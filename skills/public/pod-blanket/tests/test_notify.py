"""巡检通知：只在有告警时发，同样的告警不重复发。"""

from __future__ import annotations

import json
import os

from conftest import reopen, set_config
from podflow import cli
from podflow.db import next_id
from podflow.util import now_iso


def run(capsys, *argv: str) -> dict:
    assert cli.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def _enable_feishu(home) -> None:
    set_config(
        home,
        "notify.feishu",
        {"enabled": True, "receive_id_type": "chat_id", "receive_id": "oc_test"},
    )
    secrets = home / "secrets.env"
    secrets.write_text(
        "LITELLM_API_KEY=test-key\nFEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\n",
        encoding="utf-8",
    )
    os.chmod(secrets, 0o600)


def test_tick_notifies_only_on_alerts_and_dedupes(home, fake, capsys) -> None:
    _enable_feishu(home)
    # 没开试运行窗口、也没有告警：正常停止不发通知（每 10 分钟一次会刷屏）
    out = run(capsys, "tick", "--no-spawn", "--notify")
    assert (
        out["decision"] == "stop"
        and out["notified"]["sent"] is False
        and not fake.feishu_messages
    )
    ctx = reopen(home)
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO provider_calls(call_id, kind, stage, scope_id, model, idempotency_key, request_meta_json,"
            " status, day, currency, reserved_cost, cost_status, created_at) VALUES(?, 'image', 'T01', 'J0001',"
            " 'gpt-image-2.5-flare', 'k-unknown', '{}', 'unknown', '2026-10-08', 'CNY', 0, 'unknown', ?)",
            (next_id(conn, "call"), now_iso()),
        )
    out = run(capsys, "tick", "--no-spawn", "--notify")
    assert out["notified"]["sent"] is True and len(fake.feishu_messages) == 1
    assert "结果未知" in fake.feishu_messages[0]
    # 同样的告警不重复发
    out = run(capsys, "tick", "--no-spawn", "--notify")
    assert out["notified"]["sent"] is False and len(fake.feishu_messages) == 1


def test_notify_disabled_is_reported(home, fake, capsys) -> None:
    out = run(capsys, "notify", "--text", "hello")
    assert out["sent"] is False and not fake.feishu_messages
