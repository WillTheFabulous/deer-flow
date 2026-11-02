#!/usr/bin/env python3
"""探测 LiteLLM 代理上的语言模型能不能给 DeerFlow 的智能体用。

DeerFlow 的智能体每一轮都带工具，所以「能对话」不够。每个候选模型测四项：

1. 普通对话（非流式），并记录响应里有没有 reasoning_content；
2. 流式输出（记录首个内容块的耗时）；
3. 两步工具调用：第一轮必须产生 tool_calls；第二轮带上工具结果后给出最终回答。
   第一轮返回了 reasoning_content 时，第二轮先按原样回传；再试一次不回传，
   用来判断 config.yaml 里必须用 PatchedChatDeepSeek（会回传）还是 ChatOpenAI 也行；
4. 识图：一张纯红色小图，回答里要出现「红」或 red。

用法（在 gateway 容器里跑，容器内能直接访问 host.docker.internal:4001）：

    docker exec deer-flow-gateway /app/backend/.venv/bin/python \\
        /app/project/scripts/fork/probe_litellm_models.py [--models a,b,c] [--key-file PATH] [--json]

API key 先取 --key-file 里的 LITELLM_API_KEY= 行，再取环境变量 LITELLM_API_KEY。
只发很小的请求，不写任何文件；输出里不含 key。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://host.docker.internal:4001/v1"
DEFAULT_MODELS = [
    f"gpt-5.6-{family}-{tier}"
    for family in ("terra", "sol", "luna")
    for tier in ("high", "xhigh", "max", "ultra")
] + [
    "deepseek-v4-pro",
    "deepseek-v4-pro-0813",
    "deepseek-v4.1-flash",
]

_TOOL = {
    "type": "function",
    "function": {
        "name": "get_order_status",
        "description": "查询订单的物流状态",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号"}},
            "required": ["order_id"],
        },
    },
}
_TOOL_RESULT = {"order_id": "A1024", "status": "已发货", "carrier": "顺丰"}
_ERROR_CHARS = 240


class ProbeError(RuntimeError):
    pass


def _load_key(key_file: str | None) -> str:
    if key_file:
        for line in Path(key_file).read_text(encoding="utf-8").splitlines():
            if line.startswith("LITELLM_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not key:
        raise SystemExit("找不到 LITELLM_API_KEY：用 --key-file 指定，或先设环境变量")
    return key


def _red_png_data_url() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (220, 20, 20)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class Prober:
    def __init__(self, base_url: str, key: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {key}"}
        self.timeout = timeout

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions", headers=self.headers, json=payload
            )
        if resp.status_code != 200:
            raise ProbeError(f"HTTP {resp.status_code}: {resp.text[:_ERROR_CHARS]}")
        return resp.json()

    @staticmethod
    def _message(data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices") or []
        if not choices:
            raise ProbeError(
                f"响应没有 choices: {json.dumps(data, ensure_ascii=False)[:_ERROR_CHARS]}"
            )
        return choices[0].get("message") or {}

    def basic(self, model: str) -> dict[str, Any]:
        data = self._post(
            {
                "model": model,
                "messages": [{"role": "user", "content": "只回复两个字母：OK"}],
            }
        )
        msg = self._message(data)
        content = msg.get("content") or ""
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return {
            "ok": "ok" in content.lower(),
            "content": content[:60],
            "reasoning_content": bool(msg.get("reasoning_content")),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
        }

    def stream(self, model: str) -> dict[str, Any]:
        payload = {
            "model": model,
            "stream": True,
            "messages": [{"role": "user", "content": "用一句话介绍你自己。"}],
        }
        started = time.monotonic()
        first_content: float | None = None
        text: list[str] = []
        chunks = 0
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    raise ProbeError(
                        f"HTTP {resp.status_code}: {resp.read().decode('utf-8', 'replace')[:_ERROR_CHARS]}"
                    )
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    chunks += 1
                    for choice in json.loads(body).get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            if first_content is None:
                                first_content = time.monotonic() - started
                            text.append(piece)
        return {
            "ok": bool("".join(text).strip()),
            "chunks": chunks,
            "first_content_s": None
            if first_content is None
            else round(first_content, 1),
        }

    def tools(self, model: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": "请调用 get_order_status 查询订单 A1024 的状态，再用一句话告诉我结果。",
            }
        ]
        first = self._message(
            self._post({"model": model, "messages": messages, "tools": [_TOOL]})
        )
        tool_calls = first.get("tool_calls") or []
        if not tool_calls:
            return {
                "ok": False,
                "error": f"第一轮没有 tool_calls，回复：{(first.get('content') or '')[:80]}",
            }
        reasoning = first.get("reasoning_content")
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": first.get("content") or "",
            "tool_calls": tool_calls,
        }
        results = [
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(_TOOL_RESULT, ensure_ascii=False),
            }
            for call in tool_calls
        ]

        def finish(with_reasoning: bool) -> str:
            reply = dict(assistant)
            if with_reasoning and reasoning:
                reply["reasoning_content"] = reasoning
            return (
                self._message(
                    self._post(
                        {
                            "model": model,
                            "messages": [*messages, reply, *results],
                            "tools": [_TOOL],
                        }
                    )
                ).get("content")
                or ""
            )

        final = finish(with_reasoning=True)
        out: dict[str, Any] = {
            "ok": "发货" in final,
            "tool_calls": len(tool_calls),
            "reasoning_content": bool(reasoning),
            "final": final[:60],
        }
        if reasoning:
            try:
                out["ok_without_reasoning_echo"] = "发货" in finish(
                    with_reasoning=False
                )
            except ProbeError as exc:
                out["ok_without_reasoning_echo"] = False
                out["without_echo_error"] = str(exc)[:_ERROR_CHARS]
        return out

    def vision(self, model: str) -> dict[str, Any]:
        content = [
            {"type": "text", "text": "这张图片是什么颜色？只回答颜色。"},
            {"type": "image_url", "image_url": {"url": _red_png_data_url()}},
        ]
        answer = (
            self._message(
                self._post(
                    {"model": model, "messages": [{"role": "user", "content": content}]}
                )
            ).get("content")
            or ""
        )
        return {"ok": "红" in answer or "red" in answer.lower(), "answer": answer[:40]}

    def probe(self, model: str) -> dict[str, Any]:
        result: dict[str, Any] = {"model": model}
        for name in ("basic", "stream", "tools", "vision"):
            started = time.monotonic()
            try:
                item = getattr(self, name)(model)
            except (ProbeError, httpx.HTTPError, ValueError) as exc:
                item = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:_ERROR_CHARS]}",
                }
            item["seconds"] = round(time.monotonic() - started, 1)
            result[name] = item
        return result


def _mark(item: dict[str, Any]) -> str:
    return "通过" if item.get("ok") else "失败"


def _print_table(results: list[dict[str, Any]]) -> None:
    print(
        f"{'模型':28} {'对话':6} {'流式(首字秒)':14} {'工具':6} {'识图':6} {'reasoning_content':18} {'不回传可用':10} 总耗时"
    )
    for r in results:
        stream = r["stream"]
        tools = r["tools"]
        rc = (
            "有"
            if (r["basic"].get("reasoning_content") or tools.get("reasoning_content"))
            else "无"
        )
        no_echo = (
            "-"
            if "ok_without_reasoning_echo" not in tools
            else ("是" if tools["ok_without_reasoning_echo"] else "否")
        )
        total = sum(r[k]["seconds"] for k in ("basic", "stream", "tools", "vision"))
        stream_cell = f"{_mark(stream)}({stream.get('first_content_s')})"
        print(
            f"{r['model']:28} {_mark(r['basic']):6} {stream_cell:14} {_mark(tools):6} {_mark(r['vision']):6} {rc:18} {no_echo:10} {total:.0f}s"
        )
    for r in results:
        for name in ("basic", "stream", "tools", "vision"):
            error = r[name].get("error") or r[name].get("without_echo_error")
            if error:
                print(f"  {r['model']} {name}: {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--models",
        help="逗号分隔；默认 gpt-5.6 三系列 × high/xhigh/max/ultra + 3 个 deepseek",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--key-file",
        help="含 LITELLM_API_KEY= 行的文件（如 pod-blanket 的 secrets.env）",
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="单次请求超时秒数")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    args = parser.parse_args(argv)

    models = (
        [m.strip() for m in args.models.split(",") if m.strip()]
        if args.models
        else DEFAULT_MODELS
    )
    prober = Prober(args.base_url, _load_key(args.key_file), args.timeout)
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(pool.map(prober.probe, models))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
