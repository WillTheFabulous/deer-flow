"""连不上库、角色连接数满时的容错：Postgres 建连接按退避重试；裂变策划里丢掉的组并发跑完后再试一次。"""

from __future__ import annotations

import sys
import types

import pytest

from conftest import reopen
from podflow import db as dbmod, llm
from podflow.stages import plan as planmod


class _OperationalError(Exception):
    pass


def _fake_psycopg(monkeypatch: pytest.MonkeyPatch, fail_times: int) -> list[str]:
    calls: list[str] = []

    def connect(url, **kwargs):
        calls.append(url)
        if len(calls) <= fail_times:
            raise _OperationalError(
                'FATAL:  too many connections for role "pod_blanket"'
            )
        return types.SimpleNamespace(closed=False, execute=lambda *a, **k: None)

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        types.SimpleNamespace(connect=connect, OperationalError=_OperationalError),
    )
    monkeypatch.setattr(dbmod.time, "sleep", lambda _s: None)
    return calls


def test_pg_connect_retries_until_a_slot_frees(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_psycopg(monkeypatch, fail_times=2)
    conn = dbmod.PgDB("postgresql://u@h/db").conn()
    assert len(calls) == 3 and conn.raw.closed is False


def test_pg_connect_gives_up_after_the_last_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_psycopg(monkeypatch, fail_times=99)
    with pytest.raises(_OperationalError):
        dbmod.PgDB("postgresql://u@h/db").conn()
    assert len(calls) == len(dbmod.CONNECT_RETRY_DELAYS) + 1


def test_plan_groups_lost_to_transient_errors_are_retried(
    home, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = reopen(home)
    seen: dict[str, int] = {}

    def run_stage(ctx, stage, *, scope_id, job_id, variables):
        key = scope_id.rsplit(":", 1)[-1]
        seen[key] = seen.get(key, 0) + 1
        if key == "g2" and seen[key] == 1:
            raise RuntimeError(
                'connection failed: too many connections for role "pod_blanket"'
            )
        if key == "g3":
            raise llm.StageOutputInvalid("P05", ["无法解析为 JSON 对象"])
        return {"key": key}

    monkeypatch.setattr(planmod.llm, "run_stage", run_stage)
    groups = [{"key": "g1"}, {"key": "g2"}, {"key": "g3"}]
    outputs, failures = planmod._run_groups(
        ctx, "B999", groups, "策划", lambda g: {}, {}
    )
    # 连接出错的那组再试一次就成了；输出校验没过的不再试（run_stage 里已经重答过）
    assert sorted(outputs) == ["g1", "g2"] and list(failures) == ["g3"]
    assert seen == {"g1": 1, "g2": 2, "g3": 1}
    event = ctx.db.q1(
        "SELECT payload_json FROM events WHERE scope = ? AND event = ?",
        ("B999", "plan_groups_retried"),
    )
    assert '"recovered": ["g2"]' in event["payload_json"].replace("'", '"')
