"""以应用身份（tenant_access_token）调飞书开放平台：通知卡片和选款表共用。

凭证 FEISHU_APP_ID / FEISHU_APP_SECRET 放在 secrets.env。一个 Client 只取一次 token，用 with 包住一次完整的操作。
"""

from __future__ import annotations

from typing import Any

from . import http
from .context import Ctx

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    def __init__(self, message: str, *, code: Any = None, msg: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.msg = msg


class Client:
    def __init__(self, ctx: Ctx, *, timeout: float = 30) -> None:
        self._ctx = ctx
        self._timeout = timeout
        self._http: Any = None
        self._token = ""

    def __enter__(self) -> Client:
        app_id = self._ctx.secrets.get("FEISHU_APP_ID")
        app_secret = self._ctx.secrets.get("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            raise FeishuError("secrets.env 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
        self._http = http.make_client(self._timeout)
        try:
            token = self._http.post(
                f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            ).json()
            if token.get("code") != 0:
                raise FeishuError(
                    f"获取 tenant_access_token 失败：{token.get('msg')}",
                    code=token.get("code"),
                    msg=str(token.get("msg") or ""),
                )
        except BaseException:
            self._http.close()
            raise
        self._token = token["tenant_access_token"]
        return self

    def __exit__(self, *exc: Any) -> None:
        self._http.close()

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调一个接口，返回 data；业务码不是 0 时抛 FeishuError（带 code 与 msg）。"""
        resp = self._http.request(
            method,
            f"{FEISHU_BASE}{path}",
            params=params,
            json=json,
            data=data,
            files=files,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            body = resp.json()
        except ValueError:
            raise FeishuError(
                f"{method} {path} 返回 HTTP {resp.status_code}", code=resp.status_code
            ) from None
        if body.get("code") != 0:
            raise FeishuError(
                f"{method} {path} 失败：{body.get('code')} {body.get('msg')}",
                code=body.get("code"),
                msg=str(body.get("msg") or ""),
            )
        return body.get("data") or {}
