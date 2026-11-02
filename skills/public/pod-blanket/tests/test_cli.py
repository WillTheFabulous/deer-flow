"""CLI：输出 JSON、错误码、审批类命令必须写明决定人、预算只读。"""

from __future__ import annotations

import json

import pytest
from conftest import use_small
from podflow import cli


def run(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_status_and_budget_are_read_only(home, fake, capsys) -> None:
    code, snap = run(capsys, "status")
    assert code == 0 and snap["budget"]["daily_cap"] == 100
    code, info = run(capsys, "budget")
    assert code == 0 and "人设不得改动" in info["note"]
    with pytest.raises(SystemExit):
        cli.main(["budget", "set", "--daily-cap", "999"])


def test_seed_flow_via_cli(home, fake, seed_image, capsys) -> None:
    code, added = run(capsys, "seed", "add", str(seed_image), "--source-ref", "测试")
    assert code == 0 and added["seed_id"] == "S01"
    # 拆解完接着策划候选方向；系列规则并进「裂变方向」一起审
    code, result = run(capsys, "seed", "analyze", "S01")
    assert code == 0 and result["status"] == "family_pending"
    assert result["plan"]["batch_id"] == "B001" and result["plan"]["accepted"]
    code, listed = run(capsys, "review", "list")
    assert [r["subject_type"] for r in listed["reviews"]] == ["plan_selection"]
    with pytest.raises(SystemExit):
        cli.main(["directions", "confirm", "B001", "--pick", "1"])  # 缺 --by
    capsys.readouterr()
    code, shown = run(capsys, "directions", "show", "B001")
    assert code == 0 and shown["candidates"][0]["title"]
    code, err = run(capsys, "directions", "confirm", "B001", "--by", "tester")
    assert code == 2 and "还没选方向" in err["detail"]
    code, confirmed = run(
        capsys, "directions", "confirm", "B001", "--pick", "1", "--by", "tester"
    )
    assert code == 0 and confirmed["family_approved"] and len(confirmed["jobs"]) == 1
    code, fam = run(capsys, "family", "show", "F01")
    assert fam["status"] == "approved" and fam["approved_by"] == "tester"
    # 授权未知不再开「素材授权」待审；方向确认后也没有别的待审
    code, listed = run(capsys, "review", "list")
    assert code == 0 and listed["reviews"] == []


def test_plan_kinds_and_directions_cancel_via_cli(
    home, fake, seed_image, capsys
) -> None:
    run(capsys, "seed", "add", str(seed_image))
    run(capsys, "seed", "analyze", "S01")
    run(capsys, "directions", "confirm", "B001", "--pick", "all", "--by", "tester")
    code, out = run(
        capsys,
        "directions",
        "cancel",
        "B001",
        "--pick",
        "1",
        "--kind",
        "big",
        "--by",
        "tester",
    )
    assert code == 0 and out["cancelled"] == ["V0001"] and out["remaining_jobs"] == 2
    code, err = run(capsys, "plan", "--family", "F01", "--kinds", "small,tiny")
    assert code == 2 and "tiny" in err["detail"]
    with pytest.raises(SystemExit):
        cli.main(["directions", "cancel", "B001"])  # 缺 --by


def test_family_revise_kinds_plans_only_those(home, fake, seed_image, capsys) -> None:
    """修订后自动策划只出指定类型；旧版本还没挑的那批作废。"""
    use_small(fake)
    run(capsys, "seed", "add", str(seed_image))
    code, analyzed = run(capsys, "seed", "analyze", "S01")
    assert code == 0 and analyzed["plan"]["batch_id"] == "B001"
    code, err = run(
        capsys, "family", "revise", "F01", "--note", "放开主体", "--kinds", "tiny"
    )
    assert code == 2 and "tiny" in err["detail"]
    code, out = run(
        capsys,
        "family",
        "revise",
        "F01",
        "--note",
        "放开主体",
        "--kinds",
        "small,combo",
    )
    assert code == 0 and out["family_version"] == 2
    plan = out["plan"]
    assert {t["kind"] for t in plan["accepted"]} == {"small", "combo"}
    assert plan["superseded_batches"] == ["B001"]


def test_dismiss_selection_via_cli(home, fake, seed_image, capsys) -> None:
    run(capsys, "seed", "add", str(seed_image))
    run(capsys, "seed", "analyze", "S01")
    code, out = run(
        capsys,
        "maintenance",
        "dismiss-selection",
        "--batch",
        "B001",
        "--by",
        "tester",
        "--dry-run",
    )
    assert code == 0 and out["dry_run"] and out["reviews"] == []
    with pytest.raises(SystemExit):
        cli.main(["maintenance", "dismiss-selection", "--by", "tester"])  # 缺 --batch


def test_errors_are_json(home, fake, capsys) -> None:
    code, err = run(capsys, "seed", "show", "S99")
    assert code == 2 and err["error"] == "StageError"
    code, err = run(capsys, "plan", "--family", "F01")
    assert code == 2


def test_doctor_reports_ok(home, fake, capsys) -> None:
    code, report = run(capsys, "doctor", "--probe")
    assert code == 0
    failed = [c for c in report["checks"] if not c["ok"]]
    assert report["ok"], failed
