"""mooya 机器调用接口（`/api/service/v1`）的客户端：交一张选中的图出套图（场景图）、查进度、取原图。

两边只约定接口的输入输出（mooya 仓库 `feature-plans/service-api/README.md`）：场景图怎么出——提示词、
模板、模型、步骤、张数——全由 mooya 的图案管线和预设决定，mooya 改了这边不用跟着改。

- 地址 `mooya.base_url`（dev `http://host.docker.internal:5006`，生产走 nginx `:5173`）；
- 服务令牌放 secrets.env，名字在 `mooya.token_name`（默认 MOOYA_SERVICE_TOKEN），mooya 那边只存它的 sha256；
- 一个令牌就是 mooya 的一个客户端，绑一个租户。`mooya.token_name` 是 `legacy`（platform_internal）；
  `mooya.clients` 可以再配几个（如 `deerflow` 绑 ai_temu_deerflow，2026-10-28），新交的套图用 `mooya.submit_client`。
  mooya 按租户查任务，所以每套套图记下自己是哪个客户端交的（`scenes`），之后查进度、拉图、取消都用它；
- 取图口径 `variant`：raw = 一律不贴字标（存档用），export = 按交付工作台导出口径（字标租户贴 MIANDUDU，上架用）；
- 网络抖动、5xx、429 记为 `transient`（下次再试）；401 / 403 是令牌或租户配置问题（`service`）；其余 4xx 是这一单的问题。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import http
from .context import Ctx

API_PREFIX = "/api/service/v1"
DEFAULT_TOKEN_NAME = "MOOYA_SERVICE_TOKEN"
LEGACY_CLIENT = "legacy"
VARIANT_RAW = "raw"
VARIANT_EXPORT = "export"
# mooya 的对外状态（接口契约）：已结束的与还在跑的
TERMINAL = frozenset({"done", "partial", "failed", "cancelled"})
ACTIVE = frozenset({"queued", "running", "waiting_human", "paused"})


class MooyaError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        transient: bool = False,
        service: bool = False,
        body: Any = None,
    ):
        super().__init__(message)
        self.status = status
        # 网络抖动 / 5xx / 429：这一单没问题，等会儿再试
        self.transient = transient
        # 401 / 403：令牌或绑定租户的问题，换哪一单都一样，要人去配
        self.service = service
        # mooya 返回的 JSON（409 时带 problems：素材没好、图位对不上、标题不合规…）
        self.body = body

    @property
    def problems(self) -> list[str]:
        body = self.body if isinstance(self.body, dict) else {}
        return [str(p) for p in body.get("problems") or []] or ([str(body["error"])] if body.get("error") else [])


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return resp.text[:300]
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or data)[:300]
    return str(data)[:300]


def client_names(ctx: Ctx) -> list[str]:
    """配了哪些客户端：legacy 总在，加上 `mooya.clients` 里的。"""
    extra = ctx.setting("mooya.clients") or {}
    return [LEGACY_CLIENT, *(n for n in extra if n != LEGACY_CLIENT)]


def submit_client(ctx: Ctx) -> str:
    """新交的套图用哪个客户端（`mooya.submit_client`，缺省 legacy）。"""
    name = str(ctx.setting("mooya.submit_client") or LEGACY_CLIENT)
    if name not in client_names(ctx):
        raise MooyaError(
            f"mooya.submit_client = {name}，但 mooya.clients 里没有这个客户端", service=True
        )
    return name


def token_name(ctx: Ctx, client: str | None = None) -> str:
    if not client or client == LEGACY_CLIENT:
        return str(ctx.setting("mooya.token_name") or DEFAULT_TOKEN_NAME)
    spec = (ctx.setting("mooya.clients") or {}).get(client)
    if not isinstance(spec, dict) or not spec.get("token_name"):
        raise MooyaError(f"mooya.clients 里没有 {client}（或缺 token_name）", service=True)
    return str(spec["token_name"])


class Pool:
    """一个进程里按客户端名复用 Client（用时才建）：每套套图用交它的那个客户端。"""

    def __init__(self, ctx: Ctx) -> None:
        self._ctx = ctx
        self._clients: dict[str, Client] = {}

    def __enter__(self) -> Pool:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def get(self, client: str | None = None) -> Client:
        name = client or LEGACY_CLIENT
        if name not in self._clients:
            self._clients[name] = Client(self._ctx, client=name)
        return self._clients[name]

    def close(self) -> None:
        for api in self._clients.values():
            api.close()
        self._clients.clear()


class Client:
    def __init__(self, ctx: Ctx, client: str | None = None):
        self.name = client or LEGACY_CLIENT
        self.base_url = str(ctx.setting("mooya.base_url") or "").rstrip("/")
        token_name_ = token_name(ctx, self.name)
        self.token = ctx.secrets.get(token_name_) or ""
        if not self.base_url:
            raise MooyaError("podflow.yaml 的 mooya.base_url 没填", service=True)
        if not self.token:
            raise MooyaError(f"secrets.env 里缺 {token_name_}", service=True)
        self._http = http.make_client(float(ctx.setting("mooya.timeout_seconds") or 60))

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.request(
                method,
                self.base_url + API_PREFIX + path,
                headers={"Authorization": f"Bearer {self.token}"},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise MooyaError(
                f"连不上 mooya：{type(exc).__name__}: {exc}", transient=True
            ) from exc
        if resp.status_code < 400:
            return resp
        code = resp.status_code
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        raise MooyaError(
            f"mooya 返回 HTTP {code}：{_error_text(resp)}",
            status=code,
            transient=code >= 500 or code == 429,
            service=code in (401, 403),
            body=body,
        )

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health").json()

    def submit(
        self,
        *,
        data: bytes,
        filename: str,
        preset: str,
        client_ref: str,
        idempotency_key: str,
        label: str = "",
    ) -> dict[str, Any]:
        """交一张图出套图。同一个幂等键 mooya 只建一次，重提拿回同一个任务。返回 {job, created}。"""
        meta = {
            "preset": preset,
            "client_ref": client_ref,
            "idempotency_key": idempotency_key,
            "label": label,
        }
        resp = self._request(
            "POST",
            "/showcase/jobs",
            data={"meta": json.dumps(meta, ensure_ascii=False)},
            files={"image": (filename, data, "application/octet-stream")},
        )
        return resp.json()

    def status(self, job_id: int) -> dict[str, Any]:
        return self._request("GET", f"/showcase/jobs/{int(job_id)}").json()["job"]

    def image(
        self, job_id: int, image_id: str, *, variant: str | None = None
    ) -> tuple[bytes, str]:
        """一张原图的字节与版本号（mooya 里重生过这张图，版本号就变）。variant：raw / export，见模块说明。"""
        resp = self._request(
            "GET",
            f"/showcase/jobs/{int(job_id)}/images/{image_id}",
            params={"variant": variant} if variant else None,
        )
        return resp.content, resp.headers.get("x-image-rev", "")

    def cancel(self, job_id: int) -> dict[str, Any]:
        return self._request("POST", f"/showcase/jobs/{int(job_id)}/cancel").json()[
            "job"
        ]

    def action(self, job_id: int, action: str) -> dict[str, Any]:
        """对一个任务做一个动作（2026-10-30 mooya 加的端点）：regenerate_missing = 缺图的槽位补出来（已有的图不动），
        resume = 熔断暂停的接着跑。返回 {job, action, started}。mooya 还没上这个端点时是 404 / 405（`unsupported`）。"""
        return self._request(
            "POST", f"/showcase/jobs/{int(job_id)}/actions", json={"action": action}
        ).json()

    def reserve_code(self, prefix: str, ref: str) -> dict[str, Any]:
        """在这个客户端的租户里领一个交付包名（货号，如 Deer0001）：同一个 ref 拿回同一个号。
        返回 {code, prefix, number, ref, created}。"""
        return self._request(
            "POST", "/codes", json={"prefix": prefix, "ref": ref}
        ).json()

    def peek_code(self, prefix: str) -> dict[str, Any]:
        return self._request("GET", "/codes", params={"prefix": prefix}).json()

    # ---- Temu 上架与四家店底账（2026-10-30，mooya feature-plans/temu-listing-unify，接口见 mooya api/service_temu.py）

    def temu_stores(self) -> dict[str, Any]:
        """这个客户端的租户在 mooya 配的 Temu 店铺：{tenant, stores: [{key, label, name, column, prefix, enabled, ui, temuOpen…}]}。"""
        return self._request("GET", "/temu/stores").json()

    def temu_create_listing(self, body: dict[str, Any]) -> dict[str, Any]:
        """建上架单（同一 ref 幂等）：返回 {listing, created}。没过预检抛 MooyaError(status=409)，problems 里是原因。"""
        return self._request("POST", "/temu/listings", json=body).json()

    def temu_listings(self, *, ids: list[int] | None = None, refs: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if ids:
            params["ids"] = ",".join(str(int(i)) for i in ids)
        elif refs:
            params["refs"] = ",".join(refs)
        return self._request("GET", "/temu/listings", params=params).json()["listings"]

    def temu_listing(self, listing_id: int) -> dict[str, Any]:
        return self._request("GET", f"/temu/listings/{int(listing_id)}").json()["listing"]

    def temu_action(self, listing_id: int, action: str, **body: Any) -> dict[str, Any]:
        """hold / release / cancel / retry / title（title=…, is_custom=…）；做不了抛 MooyaError(status=409)。"""
        return self._request(
            "POST", f"/temu/listings/{int(listing_id)}/actions", json={"action": action, **body}
        ).json()["listing"]

    def temu_import(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._request("POST", "/temu/listings/import", json={"items": items}).json()["results"]

    def temu_events(self, after: int, limit: int = 500) -> dict[str, Any]:
        return self._request("GET", "/temu/events", params={"after": int(after), "limit": int(limit)}).json()

    def temu_listed(
        self, *, changed_since: str | None = None, after_id: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        """底账增量：按 (updated_at, id) 翻页，返回 {items, next: {changed_since, after_id} | None}。"""
        params: dict[str, Any] = {"limit": int(limit), "after_id": int(after_id)}
        if changed_since:
            params["changed_since"] = changed_since
        return self._request("GET", "/temu/listed", params=params).json()

    def temu_listed_image(self, product_id: int) -> bytes:
        return self._request("GET", f"/temu/listed/{int(product_id)}/image").content

    def temu_listed_human(self, code: str, fields: dict[str, Any], **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/temu/listed/{code}/human", json={"fields": fields, **body}).json()

    def temu_listed_title(self, code: str, title: str, **body: Any) -> dict[str, Any]:
        """已上架的款改标题：不合规 409（body 里 state = invalid、problems）。"""
        return self._request("POST", f"/temu/listed/{code}/title", json={"title": title, **body}).json()


ACTION_REGENERATE_MISSING = "regenerate_missing"
ACTION_RESUME = "resume"


def unsupported(exc: MooyaError) -> bool:
    """mooya 没有这个端点（还没上 2026-10-30 的版本）：404 / 405。"""
    return exc.status in (404, 405)


def baseline_info(job: dict[str, Any]) -> dict[str, Any] | None:
    """mooya 任务里白底图质检的结果（2026-10-30 起有）：{passed, score, fallback}；老版本没有为 None。"""
    value = job.get("baseline")
    return value if isinstance(value, dict) else None
