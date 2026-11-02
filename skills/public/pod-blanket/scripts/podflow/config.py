"""配置与密钥加载。"""

from __future__ import annotations

import copy
import os
import stat
from pathlib import Path
from typing import Any

import yaml

from .paths import CONFIG_EXAMPLE

CONFIG_NAME = "podflow.yaml"
SECRETS_NAME = "secrets.env"


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_example() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_EXAMPLE.read_text(encoding="utf-8"))


def load_config(home: Path) -> dict[str, Any]:
    """用户配置叠在样例默认值上：新版本新增的键不会因旧配置缺失而报错。"""
    path = home / CONFIG_NAME
    if not path.exists():
        raise ConfigError(f"缺少 {path}，先运行 podctl init")
    user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = _deep_merge(load_example(), user)
    # fanout / replaced_models 这类列表或映射以用户配置为准，不与样例合并
    user_gen = user.get("image_generation") or {}
    for key in ("fanout", "replaced_models"):
        if key in user_gen:
            cfg["image_generation"][key] = user_gen[key]
    if cfg.get("publish", {}).get("auto_listing"):
        raise ConfigError(
            "publish.auto_listing 只能是 false（旧开关，不下单）：Temu 上架走 temu.*，"
            "要勾了店铺就一路自动上架开 temu.auto.enabled（2026-10-30 用户定，人可以取消「确认上架」暂停）"
        )
    return cfg


def get(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def load_secrets(home: Path) -> dict[str, str]:
    """读取 $PODFLOW_HOME/secrets.env（KEY=VALUE）。

    沙箱会从子进程环境里剔除名字含 KEY/SECRET/TOKEN 的变量，所以引擎的凭证放在这个
    600 权限的文件里；环境变量里若恰好有同名值（宿主机调试时）也接受。
    """
    secrets: dict[str, str] = {}
    path = home / SECRETS_NAME
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                secrets[key.strip()] = value
    for key in ("LITELLM_API_KEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET"):
        if key not in secrets and os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


def secrets_file_mode_ok(home: Path) -> bool:
    path = home / SECRETS_NAME
    if not path.exists():
        return False
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode & 0o077 == 0
