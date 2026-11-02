"""费用账本：未知价格 / 零预算阻断、三道上限、并发预留不超限、结果未知保守计费。"""

from __future__ import annotations

import threading
import uuid

import pytest
from conftest import reopen, set_config
from podflow import budget


def _reserve(ctx, amount: float, *, scope: str = "J0001", key: str | None = None):
    return budget.reserve(
        ctx.db,
        ctx.cfg,
        kind="image",
        stage="T01",
        scope_id=scope,
        model="gpt-image-2.5-flare",
        idempotency_key=key or f"k-{uuid.uuid4().hex}",
        estimated_cost=amount,
    )


def test_unknown_price_blocks_without_writing_a_row(home, fake) -> None:
    set_config(home, "prices/image/gpt-image-2.5-flare", {"per_image": None})
    ctx = reopen(home)
    with pytest.raises(budget.BudgetBlocked) as err:
        budget.estimate_image_cost(ctx.cfg, "gpt-image-2.5-flare-edit")
    assert err.value.reason == "price_unknown"
    assert ctx.db.scalar("SELECT COUNT(*) FROM provider_calls") == 0


def test_zero_budget_blocks(home, fake) -> None:
    set_config(home, "budget.daily_cap", 0)
    ctx = reopen(home)
    with pytest.raises(budget.BudgetBlocked) as err:
        _reserve(ctx, 0.5)
    assert err.value.reason == "budget_zero"


def test_caps_are_enforced(home, fake) -> None:
    set_config(
        home,
        "budget",
        {"currency": "CNY", "daily_cap": 2, "total_cap": 100, "per_task_cap": 1},
    )
    ctx = reopen(home)
    _reserve(ctx, 0.6, scope="J1")
    with pytest.raises(budget.BudgetBlocked) as err:
        _reserve(ctx, 0.6, scope="J1")
    assert err.value.reason == "per_task_cap"
    _reserve(ctx, 0.9, scope="J2")
    with pytest.raises(budget.BudgetBlocked) as err:
        _reserve(ctx, 0.6, scope="J3")
    assert err.value.reason == "daily_cap"


def test_duplicate_idempotency_key_is_rejected(ctx) -> None:
    _reserve(ctx, 0.1, key="same")
    with pytest.raises(budget.BudgetBlocked) as err:
        _reserve(ctx, 0.1, key="same")
    assert err.value.reason == "duplicate_request"


def test_concurrent_reservations_never_exceed_the_cap(home, fake) -> None:
    set_config(
        home,
        "budget",
        {"currency": "CNY", "daily_cap": 5, "total_cap": 100, "per_task_cap": 100},
    )
    reopen(home).db.close()
    accepted, blocked = [], []

    def worker(i: int) -> None:
        local = reopen(home)
        try:
            _reserve(local, 1.0, scope=f"J{i}", key=f"c{i}")
            accepted.append(i)
        except budget.BudgetBlocked:
            blocked.append(i)
        finally:
            local.db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(accepted) == 5 and len(blocked) == 7
    ctx = reopen(home)
    assert budget.summary(ctx.db, ctx.cfg)["today_committed"] == pytest.approx(5.0)


def test_unknown_keeps_reservation_and_failure_releases_it(ctx) -> None:
    a = _reserve(ctx, 1.0, key="a")
    b = _reserve(ctx, 2.0, key="b")
    budget.mark_submitted(ctx.db, a.call_id)
    budget.mark_unknown(ctx.db, a.call_id, error="timeout")
    budget.mark_submitted(ctx.db, b.call_id)
    budget.settle_failure(ctx.db, b.call_id, error="503", http_status=503)
    s = budget.summary(ctx.db, ctx.cfg)
    assert s["today_committed"] == pytest.approx(1.0)
    assert s["unknown_calls"] == 1


def test_llm_cost_uses_actual_usage(ctx) -> None:
    est = budget.estimate_llm_cost(ctx.cfg, "gpt-6.1-sol-high", "P07")
    assert est == pytest.approx(12000 / 1e6 * 10 + 6000 / 1e6 * 30)
    actual = budget.actual_llm_cost(
        ctx.cfg, "gpt-6.1-sol-high", {"prompt_tokens": 1000, "completion_tokens": 100}
    )
    assert actual == pytest.approx(0.013)
