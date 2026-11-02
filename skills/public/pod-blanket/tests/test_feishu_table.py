"""选款表：选中的款同步到飞书多维表格（建表、加协作者、缩略图、方向、原图、下载链接、续签、删除、重试、触发）。"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from conftest import reopen, set_config
from podflow import bg, cli, feishu, feishu_table, review, store, ui
from podflow.util import dumps, iso, utcnow
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_storage import BUCKET, _enable_oss
from test_ui import _enable_feishu, run


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bg, "_current", None)
    return calls


def _setup(home, seed_image, *, oss: bool = True):
    _enable_feishu(home)
    if oss:
        _enable_oss(home)
    set_config(home, "table.enabled", True)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    return ctx


def _approve(ctx, count: int, *, by: str = "feishu:ou_tester") -> list[str]:
    items = review.list_reviews(ctx, types=["asset_selection"])[:count]
    for item in items:
        review.decide(ctx, item["review_id"], "approved", by=by)
    return [i["subject_id"] for i in items]


def _table_spawns(spawned: list[list[str]]) -> list[list[str]]:
    return [argv[2:] for argv in spawned if argv[2:3] == ["table"]]


def test_disabled_table_makes_no_calls(ctx, fake, seed_image, spawned) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    _approve(ctx, 1)
    assert _table_spawns(spawned) == []
    assert feishu_table.sync(ctx) == {
        "skipped": True,
        "reason": "table.enabled 为 false",
    }
    assert fake.bitable_calls == [] and ui.gallery(ctx)["table_url"] is None
    assert ui.dashboard(ctx)["table_url"] is None


def test_first_sync_creates_table_and_adds_rows(
    home, fake, seed_image, spawned
) -> None:
    ctx = _setup(home, seed_image)
    store.sync(ctx)  # 原图都已传到 OSS
    approved = _approve(ctx, 2)
    # 选中后拉起后台同步；已经有一个在跑时不再重复拉起
    assert _table_spawns(spawned) == [["table", "sync"]]

    out = feishu_table.sync(ctx)
    assert (out["created"], out["added"], out["failed"]) == (True, 2, 0)
    # 建表：一个多维表格、一张带全部字段的数据表（自带的空表删掉）、画册视图、选款的人加为可管理协作者
    (app,) = fake.bitable_apps
    assert app["name"] == "POD 毛毯选款表"
    (table,) = fake.bitable_tables.values()
    assert [f["field_name"] for f in table["fields"]] == [
        f["field_name"] for f in feishu_table.FIELDS
    ]
    assert table["fields"][0]["field_name"] == "素材"
    assert table["views"] == [{"view_name": "看图", "view_type": "gallery"}]
    assert (
        "DELETE",
        f"/bitable/v1/apps/{app['app_token']}/tables/{app['default_table_id']}",
    ) in fake.bitable_calls
    assert fake.bitable_members == [
        {
            "member_type": "openid",
            "member_id": "ou_tester",
            "perm": "full_access",
            "type": "bitable",
            "need_notification": "false",
        }
    ]
    # 建好后发卡片，第一个按钮直接打开表格
    card = next(
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"] == "选款表已建好"
    )
    actions = next(e for e in card["elements"] if e["tag"] == "action")["actions"]
    assert (
        actions[0]["url"] == app["url"]
        and actions[0]["text"]["content"] == "打开选款表"
    )

    records = sorted(fake.bitable_records.values(), key=lambda r: r["素材"])
    # 「素材」是「编号_方向短语」，短语就是「方向」列去掉 V 编号的那句
    assert [r["素材"] for r in records] == [f"{a}_主题方案1" for a in sorted(approved)]
    row = records[0]
    assert row["方向"] == "V0001 主题方案1"
    assert row["改了什么"] == "主题：原样 → 主题方案1"
    assert (row["不变"], row["方位"], row["类型"]) == (
        "三只陪伴的构图",
        ["主题叙事"],
        "大变化",
    )
    # 新建的表：方位是 11 个能裂变的维度，类型是小变化 / 大变化
    table = next(iter(fake.bitable_tables.values()))
    by_name = {f["field_name"]: f for f in table["fields"]}
    assert [o["name"] for o in by_name["类型"]["property"]["options"]] == [
        "小变化",
        "大变化",
    ]
    aspects = [o["name"] for o in by_name["方位"]["property"]["options"]]
    assert len(aspects) == 11 and aspects[0] == "主题叙事" and "文字定制" in aspects
    # 绘制技法同在小变化、大变化两层，选项里只出现一次
    assert len(set(aspects)) == 11 and aspects.count("绘制技法") == 1
    assert row["模型"].removesuffix("-edit") in (
        "nano-banana-2.1",
        "gpt-2.5-flare",
        "gpt-2.5-sunburst",
    )
    assert row["出图方式"] in ("文生图", "图生图")
    assert (row["原图编号"], row["批次"], row["系列"]) == ("S01", "B001", "F01 v1")
    assert row["选中人"] == [{"id": "ou_tester"}] and row["选中时间"] > 0
    assert "备注" not in row and "修改自" not in row
    # 缩略图和原图都是传到这张表里的小图；原图缩略图只传一次，各行复用
    thumb = fake.bitable_files[row["缩略图"][0]["file_token"]]
    assert (thumb["parent_type"], thumb["parent_node"]) == (
        "bitable_image",
        app["app_token"],
    )
    assert thumb["file_name"] == f"{row['素材']}.jpg"
    assert int(thumb["size"]) < 200_000
    assert records[0]["原图"] == records[1]["原图"]
    uploads = [c for c in fake.bitable_calls if c[1] == "/drive/v1/medias/upload_all"]
    assert len(uploads) == 3
    # 原图下载链接：OSS 公网签名链接，下载成「编号_方向短语.png」（中文名走 filename*，ASCII 兜底是存储里的文件名）
    link = urlsplit(row["下载原图"]["link"])
    asset_path = ctx.db.scalar(
        "SELECT path FROM assets WHERE asset_id = ?", (row["素材"].split("_", 1)[0],)
    )
    assert link.netloc == f"{BUCKET}.oss-cn-hangzhou.aliyuncs.com"
    name = f"{row['素材']}{Path(asset_path).suffix}"
    assert parse_qs(link.query)["response-content-disposition"] == [
        f'attachment; filename="{Path(asset_path).name}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    ]
    assert row["下载原图"]["text"].startswith("下载原图（") and row["链接到期"] > 0

    # 再同步不重复；工作台已选款带「打开选款表」
    again = feishu_table.sync(ctx)
    assert (again["created"], again["added"], again["refreshed"]) == (False, 0, 0)
    assert len(fake.bitable_records) == 2 and len(fake.bitable_apps) == 1
    assert ui.gallery(ctx)["table_url"] == app["url"]
    # 工作台的固定按钮「选款表」也直接打开它
    assert ui.dashboard(ctx)["table_url"] == app["url"]
    status = feishu_table.status(ctx)
    assert (status["rows"], status["pending"], status["without_link"]) == (2, 0, 0)


def test_links_wait_for_oss_and_renew_before_expiry(
    home, fake, seed_image, spawned
) -> None:
    ctx = _setup(home, seed_image)
    (asset_id,) = _approve(ctx, 1)
    # 原图还没传到 OSS：先不放链接
    feishu_table.sync(ctx)
    ((record_id, row),) = fake.bitable_records.items()
    assert "下载原图" not in row and feishu_table.status(ctx)["without_link"] == 1
    assert not feishu_table._has_work(ctx)

    # 传上去之后巡检认得出要补链接，同步时补上
    store.sync(ctx)
    assert feishu_table._has_work(ctx)
    assert feishu_table.sync(ctx)["refreshed"] == 1
    assert fake.bitable_records[record_id]["下载原图"]["link"].startswith("https://")
    assert ("PUT", f"/bitable/v1/apps/{fake.bitable_apps[0]['app_token']}/tables/") in [
        (m, p.split("tables/")[0] + "tables/") for m, p in fake.bitable_calls
    ]

    # 剩不到 15 天就续签
    soon = iso(utcnow() + dt.timedelta(days=3))
    with ctx.db.tx() as conn:
        conn.execute("UPDATE table_rows SET link_expires_at = ?", (soon,))
    assert feishu_table._has_work(ctx)
    assert feishu_table.sync(ctx)["refreshed"] == 1
    expires = ctx.db.scalar(
        "SELECT link_expires_at FROM table_rows WHERE asset_id = ?", (asset_id,)
    )
    assert expires > iso(utcnow() + dt.timedelta(days=80))


def test_unpicked_rows_are_removed_and_failures_retry(
    home, fake, seed_image, spawned
) -> None:
    ctx = _setup(home, seed_image, oss=False)
    first, second = _approve(ctx, 2)
    assert feishu_table.sync(ctx)["added"] == 2
    # 本机存储：没有下载链接
    assert all("下载原图" not in r for r in fake.bitable_records.values())

    # 改判成不要：同步时删掉那一行
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE assets SET human_decision = 'rejected' WHERE asset_id = ?", (first,)
        )
    out = feishu_table.sync(ctx)
    assert out["removed"] == 1
    assert [r["素材"].split("_", 1)[0] for r in fake.bitable_records.values()] == [
        second
    ]

    # 写行失败：记失败次数，下次重试
    fake.bitable_fail.add(("POST", "/records"))
    (third,) = [
        i["subject_id"] for i in review.list_reviews(ctx, types=["asset_selection"])[:1]
    ]
    _approve(ctx, 1)
    out = feishu_table.sync(ctx)
    assert out["failed"] == 1 and out["errors"][0]["asset_id"] == third
    assert (
        ctx.db.scalar("SELECT attempts FROM table_rows WHERE asset_id = ?", (third,))
        == 1
    )
    assert feishu_table.status(ctx)["pending"] == 1
    fake.bitable_fail.clear()
    assert feishu_table.sync(ctx)["added"] == 1
    assert (
        ctx.db.scalar("SELECT attempts FROM table_rows WHERE asset_id = ?", (third,))
        == 0
    )

    # 失败满上限就不再自动重试，--retry 清零后接着试
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE assets SET human_decision = 'approved' WHERE asset_id = ?", (first,)
        )
    fake.bitable_fail.add(("POST", "/records"))
    for _ in range(feishu_table.MAX_ATTEMPTS):
        feishu_table.sync(ctx)
    assert feishu_table.status(ctx)["failed"][0]["asset_id"] == first
    assert not feishu_table._has_work(ctx)
    fake.bitable_fail.clear()
    assert feishu_table.sync(ctx, retry=True)["added"] == 1


def test_person_field_falls_back_when_rejected(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image, oss=False)
    _approve(ctx, 2)
    fake.bitable_reject_person = True
    assert feishu_table.sync(ctx)["added"] == 2
    assert all("选中人" not in r for r in fake.bitable_records.values())
    # 第一行失败后记住不写人员字段，第二行不再先试一次
    record_posts = [
        c for c in fake.bitable_calls if c[0] == "POST" and c[1].endswith("/records")
    ]
    assert len(record_posts) == 3 and feishu_table.base(ctx)["no_person"] is True


def test_create_failure_backs_off(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image, oss=False)
    _approve(ctx, 1)
    fake.bitable_fail.add(("POST", "/bitable/v1/apps"))
    with pytest.raises(feishu.FeishuError):
        feishu_table.sync(ctx)
    assert "fake failure" in feishu_table.status(ctx)["create_error"]
    # 建表失败（多半是还没开权限）后，一小时内巡检不再拉起
    assert not feishu_table._has_work(ctx) and feishu_table.kick(ctx) is None
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn,
            feishu_table.ERROR_META_KEY,
            dumps({"at": iso(utcnow() - dt.timedelta(hours=2)), "error": "x"}),
        )
    assert feishu_table._has_work(ctx)
    fake.bitable_fail.clear()
    assert feishu_table.sync(ctx)["created"] is True
    assert feishu_table.status(ctx)["create_error"] is None


def test_cli_table_commands(home, fake, seed_image, capsys, spawned) -> None:
    ctx = _setup(home, seed_image, oss=False)
    _approve(ctx, 1)
    assert run(capsys, "table", "status")["url"] is None
    created = run(capsys, "table", "init")
    assert created["created"] is True and created["shared_with"] == ["ou_tester"]
    assert run(capsys, "table", "init")["created"] is False
    assert run(capsys, "table", "sync")["added"] == 1
    # --reset：另建一张新表，选中的款重新同步进去
    assert run(capsys, "table", "init", "--reset")["created"] is True
    assert len(fake.bitable_apps) == 2
    assert run(capsys, "table", "sync")["added"] == 1
    task = run(capsys, "table", "sync", "--detach")
    assert task["task_id"].startswith("BG") and _table_spawns(spawned)[-1] == [
        "table",
        "sync",
    ]

    # 别的进程正在同步（持有租约）时不另建表
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn,
            feishu_table.LEASE_KEY,
            dumps({"owner": "other", "until": iso(utcnow() + dt.timedelta(minutes=5))}),
        )
    assert cli.main(["table", "init", "--reset"]) != 0
    assert "正在同步" in capsys.readouterr().out and len(fake.bitable_apps) == 2

    set_config(home, "table.enabled", False)
    assert cli.main(["table", "init"]) != 0
    assert "table.enabled 为 false" in capsys.readouterr().out


def _move_calls(fake) -> list[str]:
    return [
        path
        for _, path in fake.bitable_calls
        if path.endswith("/nodes/move_docs_to_wiki")
    ]


def test_unconfigured_table_stays_in_drive(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image)
    created = feishu_table.init(ctx)
    assert created["url"].startswith("https://example.feishu.cn/base/")
    assert _move_calls(fake) == []
    assert feishu_table.status(ctx)["wiki_error"] is None


def test_existing_table_moves_into_wiki(
    home, fake, seed_image, monkeypatch, spawned
) -> None:
    monkeypatch.setattr(feishu_table.time, "sleep", lambda *_a, **_k: None)
    fake.wiki_processing_left = 1
    ctx = _setup(home, seed_image)
    store.sync(ctx)
    _approve(ctx, 1)
    first = feishu_table.sync(ctx)
    assert first["url"].startswith("https://example.feishu.cn/base/")
    assert _move_calls(fake) == []
    seed_id = ctx.db.q1("SELECT seed_id FROM seeds")["seed_id"]
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO candidate_tables(seed_id, table_id, url, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                seed_id,
                "tblCand",
                f"{first['url']}?table=tblCand",
                iso(utcnow()),
                iso(utcnow()),
            ),
        )
    set_config(home, "table.wiki_space_id", "spc1")
    ctx = reopen(home)
    # 选中时拉起的同步还记着本进程的 pid，先收掉，巡检才会因为「还没进知识库」再拉起一次
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE bg_tasks SET status = 'done', finished_at = ?, updated_at = ? WHERE kind = 'table'",
            (iso(utcnow()), iso(utcnow())),
        )
    before = len(_table_spawns(spawned))
    assert feishu_table.wiki_pending(ctx)
    assert feishu_table.kick(ctx) is not None
    assert _table_spawns(spawned)[before:] == [["table", "sync"]]

    moved = feishu_table.sync(ctx)
    assert moved["url"].startswith("https://example.feishu.cn/wiki/wik")
    node = moved["url"].rsplit("/", 1)[-1]
    row = ctx.db.q1("SELECT url FROM candidate_tables WHERE seed_id = ?", (seed_id,))
    assert row["url"] == f"{moved['url']}?table=tblCand"
    card = next(
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"] == "选款表已放进知识库"
    )
    actions = next(e for e in card["elements"] if e["tag"] == "action")["actions"]
    assert actions[0]["url"] == moved["url"]
    assert _move_calls(fake) == ["/wiki/v2/spaces/spc1/nodes/move_docs_to_wiki"]
    again = feishu_table.sync(ctx)
    assert again["url"] == moved["url"] and _move_calls(fake) == [
        "/wiki/v2/spaces/spc1/nodes/move_docs_to_wiki"
    ]
    assert feishu_table.status(ctx)["wiki_node_token"] == node
    assert feishu_table.wiki_pending(ctx) is False


def test_new_table_opens_from_wiki(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image)
    set_config(home, "table.wiki_space_id", "spc1")
    ctx = reopen(home)
    created = feishu_table.init(ctx)
    assert created["created"] is True
    assert created["url"].startswith("https://example.feishu.cn/wiki/")
    card = next(
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"] == "选款表已建好"
    )
    actions = next(e for e in card["elements"] if e["tag"] == "action")["actions"]
    assert actions[0]["url"] == created["url"]
    assert not any(
        c["header"]["title"]["content"] == "选款表已放进知识库"
        for c in fake.feishu_cards
    )


def test_wiki_permission_error_still_syncs_rows(
    home, fake, seed_image, spawned
) -> None:
    ctx = _setup(home, seed_image)
    set_config(home, "table.wiki_space_id", "spc1")
    ctx = reopen(home)
    fake.wiki_fail_code = 131006
    store.sync(ctx)
    _approve(ctx, 1)
    out = feishu_table.sync(ctx)
    assert (out["created"], out["added"], out["failed"]) == (True, 1, 0)
    status = feishu_table.status(ctx)
    assert status["url"].startswith("https://example.feishu.cn/base/")
    assert "wiki:wiki" in status["wiki_error"]
    assert feishu_table.wiki_pending(ctx) is False
