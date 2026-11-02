"""G00 + Pxx 提示词拼装。

提示词正文来自方案「提示词初稿」，一字不改地放在 prompts/*.md。拼装规则：
- system = G00 全文 + 当前阶段 Pxx 全文 + 程序追加的输出格式要求（含 result 的 JSON Schema）；
- 「输入」段里的 {{变量}} 在 system 中替换为「变量名」引用，变量的实际值以带标题的 JSON 块放进 user 消息，
  图像变量作为附图按顺序给出。提示词文件里不写任何注释，避免注释里的占位符被二次替换进 prompt。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .paths import PROMPTS_DIR, SCHEMAS_DIR
from .util import sha256_text

SECTIONS = ("输入", "角色与任务", "执行规则", "输出约定", "结束与回流")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
IMAGE_VARS = frozenset(
    {"source_images", "source_image", "candidate_image", "reference_image"}
)
SCHEMA_VERSION = "0.1"


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    title: str
    sections: dict[str, str]

    @property
    def variables(self) -> list[str]:
        return list(dict.fromkeys(VAR_RE.findall(self.sections["输入"])))


@dataclass
class ImageInput:
    label: str
    data: bytes
    mime: str = "image/jpeg"


@dataclass
class BuiltPrompt:
    system: str
    user_content: list[dict[str, Any]]
    prompt_version: str
    variables: dict[str, Any] = field(default_factory=dict)


@lru_cache(maxsize=None)
def load_prompt(prompt_id: str) -> Prompt:
    text = (PROMPTS_DIR / f"{prompt_id}.md").read_text(encoding="utf-8")
    title_match = re.match(r"#\s+(\S+)\s+(.+)", text)
    if not title_match or title_match.group(1) != prompt_id:
        raise PromptError(f"{prompt_id}.md 缺少标题行")
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines()[1:]:
        heading = re.match(r"##\s+(.+)", line)
        if heading:
            if current:
                sections[current] = "\n".join(buf).strip()
            current, buf = heading.group(1).strip(), []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    missing = [s for s in SECTIONS if not sections.get(s)]
    if missing:
        raise PromptError(f"{prompt_id}.md 缺少段落：{missing}")
    return Prompt(
        prompt_id=prompt_id, title=title_match.group(2).strip(), sections=sections
    )


def prompt_version(prompt_id: str) -> str:
    parts = [
        (PROMPTS_DIR / "G00.md").read_text(encoding="utf-8"),
        (PROMPTS_DIR / f"{prompt_id}.md").read_text(encoding="utf-8"),
    ]
    schema = SCHEMAS_DIR / f"{prompt_id}.result.schema.json"
    if schema.exists():
        parts.append(schema.read_text(encoding="utf-8"))
    return f"{prompt_id}-{sha256_text(''.join(parts))[:10]}"


def _render(prompt: Prompt) -> str:
    lines = []
    for name in SECTIONS:
        body = prompt.sections[name]
        if name == "输入":
            body = VAR_RE.sub(lambda m: f"「{m.group(1)}」", body)
        lines.append(f"## {name}\n{body}")
    return "\n\n".join(lines)


def _image_part(img: ImageInput) -> dict[str, Any]:
    import base64

    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{img.mime};base64,{base64.b64encode(img.data).decode('ascii')}"
        },
    }


def build(
    stage: str,
    *,
    job_id: str,
    variables: dict[str, Any],
    images: dict[str, list[ImageInput]] | None = None,
    result_schema: str,
) -> BuiltPrompt:
    """按 G00 + Pxx 拼出 system / user。缺任何一个声明过的非图像变量直接报错（编程错误）。"""
    g00 = load_prompt("G00")
    pxx = load_prompt(stage)
    images = images or {}
    declared = list(dict.fromkeys(g00.variables + pxx.variables))
    missing = [v for v in declared if v not in IMAGE_VARS and v not in variables]
    if missing:
        raise PromptError(f"{stage} 缺少输入变量：{missing}")
    missing_images = [v for v in pxx.variables if v in IMAGE_VARS and v not in images]
    if missing_images:
        raise PromptError(f"{stage} 缺少图像输入：{missing_images}")

    system = "\n\n".join(
        [
            f"# 公共系统约束（G00）\n\n{_render(g00)}",
            f"# 当前阶段：{stage} {pxx.title}\n\n{_render(pxx)}",
            "# 输出格式（程序会校验，不合格会被退回重答）\n\n"
            "只输出一个 JSON 对象，不要 Markdown 代码块，不要任何 JSON 以外的文字。\n"
            f'外壳字段固定为：schema_version="{SCHEMA_VERSION}"、job_id="{job_id}"、stage="{stage}"，'
            "以及 status（complete|needs_review|blocked|failed）、result、unknowns、evidence_refs、next_action。\n"
            "result 必须满足下面的 JSON Schema：\n" + result_schema,
        ]
    )

    # 附图编号按「输入」段声明顺序统一分配，正文引用与附件一一对应
    numbered: list[tuple[int, str, ImageInput]] = []
    for name in declared:
        for img in images.get(name, []) if name in IMAGE_VARS else []:
            numbered.append((len(numbered) + 1, name, img))

    blocks = [
        "以下是本次调用的输入。JSON 值即「输入」段中同名变量的内容；图像作为附图按编号给出。"
    ]
    for name in declared:
        if name in IMAGE_VARS:
            labels = "、".join(f"图{idx}" for idx, n, _ in numbered if n == name)
            blocks.append(f"### {name}\n见附图：{labels or '无'}")
        else:
            value = variables[name]
            text = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, indent=1)
            )
            blocks.append(f"### {name}\n{text}")
    content: list[dict[str, Any]] = [{"type": "text", "text": "\n\n".join(blocks)}]
    for idx, name, img in numbered:
        content.append({"type": "text", "text": f"图{idx}（{name}）：{img.label}"})
        content.append(_image_part(img))
    return BuiltPrompt(
        system=system,
        user_content=content,
        prompt_version=prompt_version(stage),
        variables=variables,
    )
