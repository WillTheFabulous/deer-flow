"""阶段输出的 JSON Schema 校验（公共外壳 + 各阶段 result）。"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from .paths import SCHEMAS_DIR


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def result_schema_name(stage: str) -> str:
    return f"{stage}.result"


def _errors(name: str, obj: Any, prefix: str) -> list[str]:
    errors = sorted(
        _validator(name).iter_errors(obj), key=lambda e: list(e.absolute_path)
    )
    out = []
    for err in errors[:12]:
        path = "/".join(str(p) for p in err.absolute_path)
        out.append(f"{prefix}{('/' + path) if path else ''}: {err.message[:300]}")
    return out


def validate_envelope(obj: Any) -> list[str]:
    return _errors("envelope", obj, "$")


def validate_result(stage: str, result: Any) -> list[str]:
    return _errors(result_schema_name(stage), result, "$.result")


def schema_text(stage: str) -> str:
    return json.dumps(
        load_schema(result_schema_name(stage)), ensure_ascii=False, indent=1
    )
