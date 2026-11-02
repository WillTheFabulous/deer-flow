"""「选中的款」的下载确认：一款一个勾、表按「排序」排（待下载 → 还不能下 → 已下载），
和批次交付包的「已下载」双向打通，巡检兜底，迁移人自己记的旧标记。"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import reopen, set_config
from podflow import candidates, downloads, feishu_table, lease, packages, scenes
from podflow.util import iso, utcnow
from test_scenes import _cli, _setup, spawned  # noqa: F401 - 夹具，下面 usefixtures 用

# 假拉起后台进程（表格同步、套图）：只要它的副作用，不看拉起了什么
pytestmark = pytest.mark.usefixtures("spawned")
BY = "feishu:ou_tester"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认时间、交付包对象名每取一次往后走一分钟：先后确认的款排序分得开，重打的包不撞名。"""
    state = {"now": utcnow()}

    def tick() -> dt.datetime:
        state["now"] += dt.timedelta(minutes=1)
        return state["now"]

    monkeypatch.setattr(packages, "utcnow", tick)
    monkeypatch.setattr(downloads, "now_iso", lambda: iso(tick()))


def _ready(
    home,
    seed_image,
    *,
    approve: int = 2,
    scenes_for: list[int] | None = None,
    confirm: bool = True,
):
    """选中 approve 款、出完套图（scenes_for 只给这几款出），建好「选中的款」；confirm 打开下载确认。"""
    if confirm:
        set_config(home, "table.download_confirm", True)
    ctx, picked = _setup(home, seed_image, approve=approve)
    if scenes_for is None:
        scenes.start(ctx, by="t", seed_id="S01")
    elif scenes_for:
        scenes.start(ctx, by="t", asset_ids=[picked[i] for i in scenes_for])
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    return ctx, picked


def _rows(ctx) -> dict[str, str]:
    return {
        r["asset_id"]: r["record_id"]
        for r in ctx.db.q("SELECT asset_id, record_id FROM table_rows")
    }


def _order(fake, record_id: str) -> int:
    return fake.bitable_records[record_id][downloads.ORDER_FIELD]


def _checked(fake, record_id: str) -> bool:
    return bool(fake.bitable_records[record_id].get(downloads.CONFIRM_FIELD))


def _tick(ctx, fake, asset_id: str) -> dict:
    """人在「选中的款」这一行勾上「下载确认」，网关转来记录变更事件（只有勾上才转）。"""
    info = feishu_table.base(ctx)
    record_id = _rows(ctx)[asset_id]
    fake.bitable_records[record_id][downloads.CONFIRM_FIELD] = True
    return candidates.control_event(
        ctx,
        file_token=info["app_token"],
        table_id=info["table_id"],
        record_ids=[record_id],
        by=BY,
    )


def _sources(ctx) -> dict[str, str]:
    return {
        r["asset_id"]: r["source"]
        for r in ctx.db.q("SELECT asset_id, source FROM selected_downloads")
    }


def test_tick_confirms_and_sinks_the_row(home, fake, seed_image) -> None:
    ctx, (first, second) = _ready(home, seed_image)
    info = feishu_table.base(ctx)
    fields = {
        f["field_name"]: f for f in fake.bitable_tables[info["table_id"]]["fields"]
    }
    # 新表建表时就带两列：勾选框带说明，排序是数字
    assert fields[downloads.CONFIRM_FIELD]["type"] == 7
    assert "打勾" in fields[downloads.CONFIRM_FIELD]["description"]["text"]
    assert fields[downloads.ORDER_FIELD]["type"] == 2
    rows = _rows(ctx)
    # 两款都出完了套图、还没确认：都在最上面的「待下载」，没打勾
    for asset_id in (first, second):
        assert downloads.GROUP_PENDING <= _order(fake, rows[asset_id])
        assert _order(fake, rows[asset_id]) < downloads.GROUP_WAITING
        assert not _checked(fake, rows[asset_id])
    out = _tick(ctx, fake, first)
    assert out["confirmed"] == [first] and out["rejected"] == []
    # 勾上的那一行当场沉到「已下载」，另一行还在上面；勾留着
    assert _order(fake, rows[first]) >= downloads.GROUP_DONE
    assert _order(fake, rows[second]) < _order(fake, rows[first])
    assert _checked(fake, rows[first])
    mark = ctx.db.q1("SELECT * FROM selected_downloads WHERE asset_id = ?", (first,))
    assert mark["source"] == "table" and mark["marked_by"] == BY
    # 一个批次只勾了一款：交付包还不算下载
    assert not packages.by_seed(ctx, "S01")["S01"][0]["downloaded"]
    # 程序自己写的勾也会发事件：回读已经确认了，不再处理
    assert _tick(ctx, fake, first)["already"] == [first]
    assert not downloads.has_work(ctx)
    status = feishu_table.status(ctx)["downloads"]
    assert status["downloaded"] == 1 and status["to_download"] == 1


def test_tick_without_finished_scenes_is_taken_back(home, fake, seed_image) -> None:
    ctx, (first, second) = _ready(home, seed_image, scenes_for=[0])
    rows = _rows(ctx)
    # second 没出套图：在「还不能下」那一组，排在待下载后面
    assert downloads.GROUP_WAITING <= _order(fake, rows[second])
    assert _order(fake, rows[second]) < downloads.GROUP_DONE
    assert _order(fake, rows[first]) < _order(fake, rows[second])
    out = _tick(ctx, fake, second)
    assert out["confirmed"] == []
    assert out["rejected"] == [{"asset_id": second, "reason": "还没出套图"}]
    # 勾了不算：程序把勾取消
    assert not _checked(fake, rows[second])
    assert _sources(ctx) == {}
    info = feishu_table.base(ctx)
    assert candidates.control_event(
        ctx,
        file_token=info["app_token"],
        table_id=info["table_id"],
        record_ids=["recNope"],
        by=BY,
    ) == {"ignored": "改的不是选中的款里的行"}


def test_order_groups_and_rerender_moves_back_up(home, fake, seed_image) -> None:
    ctx, picked = _ready(home, seed_image, approve=3, scenes_for=[0, 1])
    a, b, c = picked
    rows = _rows(ctx)
    _tick(ctx, fake, a)
    _tick(ctx, fake, b)
    orders = {x: _order(fake, rows[x]) for x in picked}
    # 还不能下（c 没出套图）→ 已下载（a 先确认，b 后确认，刚确认的 b 在最底下）
    assert orders[c] < orders[a] < orders[b]
    assert downloads.GROUP_WAITING <= orders[c] < downloads.GROUP_DONE
    # a 重出套图：旧的确认作废，勾取消，在出的时候在中间
    scenes.start(ctx, by="t", asset_ids=[a], force=True)
    feishu_table.sync(ctx)
    assert not _checked(fake, rows[a])
    assert downloads.GROUP_WAITING <= _order(fake, rows[a]) < downloads.GROUP_DONE
    # 出完了回到最上面的「待下载」，要再确认一次
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    assert _order(fake, rows[a]) < downloads.GROUP_WAITING
    assert not downloads.items(ctx, [a])[a]["confirmed"]
    assert _checked(fake, rows[b])


def test_confirming_a_whole_batch_marks_its_package(
    home, fake, seed_image, capsys
) -> None:
    ctx, (first, second) = _ready(home, seed_image)
    _tick(ctx, fake, first)
    code, out = _cli(capsys, "table", "downloaded", "--asset", second, "--by", BY)
    assert code == 0 and out["confirmed"] == [second]
    assert [(p["seed_id"], p["batch_id"]) for p in out["packages"]] == [("S01", "B001")]
    (view,) = packages.by_seed(ctx, "S01")["S01"]
    assert view["downloaded"] and packages.complete(view)
    assert (
        ctx.db.scalar("SELECT source FROM package_downloads WHERE batch_id = 'B001'")
        == "selected"
    )
    # 对话里标的也当场写进表：打勾、沉底
    rows = _rows(ctx)
    assert _checked(fake, rows[second])
    assert _order(fake, rows[second]) >= downloads.GROUP_DONE
    # 撤销单款：不动交付包
    code, out = _cli(
        capsys, "table", "downloaded", "--asset", second, "--by", BY, "--undo"
    )
    assert out["undone"] == [second]
    assert not _checked(fake, rows[second])
    assert packages.by_seed(ctx, "S01")["S01"][0]["downloaded"]


def test_package_mark_ticks_its_items_and_undo_takes_only_those_back(
    home, fake, seed_image
) -> None:
    ctx, (first, second) = _ready(home, seed_image)
    downloads.mark_assets(ctx, [first], by=BY)
    packages.mark_downloaded(ctx, "S01", "B001", by=BY, source="card")
    assert _sources(ctx) == {first: "chat", second: "package"}
    feishu_table.sync(ctx)
    rows = _rows(ctx)
    assert _checked(fake, rows[second])
    assert _order(fake, rows[second]) >= downloads.GROUP_DONE
    # 撤销交付包：只撤由它带出来的确认，人自己标的留着
    out = packages.unmark_downloaded(ctx, "S01", "B001", by=BY)
    assert out["selected_undone"] == [second]
    feishu_table.sync(ctx)
    assert not _checked(fake, rows[second]) and _checked(fake, rows[first])
    assert _order(fake, rows[second]) < downloads.GROUP_WAITING


def test_poll_picks_up_missed_ticks_and_unticks(home, fake, seed_image) -> None:
    ctx, (first, second) = _ready(home, seed_image)
    rows = _rows(ctx)
    # 巡检到点时也拉起回读
    assert candidates.kick_controls(ctx)
    # 事件没送到：巡检回读整列补上（列表接口返回文本形式，取消过的勾是 false）
    fake.bitable_records[rows[first]][downloads.CONFIRM_FIELD] = "true"
    fake.bitable_records[rows[second]][downloads.CONFIRM_FIELD] = "false"
    out = downloads.poll(ctx)
    assert out["read"] == 2 and out["confirmed"] == [first]
    assert _order(fake, rows[first]) >= downloads.GROUP_DONE
    # 人把勾取消了（取消不会推过来）：下一次巡检撤销，这一行回到上面
    fake.bitable_records[rows[first]][downloads.CONFIRM_FIELD] = False
    out = downloads.poll(ctx)
    assert out["undone"] == [first]
    assert _order(fake, rows[first]) < downloads.GROUP_WAITING
    # 交付包带出来、还没写进表的勾不算人取消
    packages.mark_downloaded(ctx, "S01", "B001", by=BY, source="card")
    assert downloads.poll(ctx)["undone"] == []
    feishu_table.sync(ctx)
    assert _checked(fake, rows[first]) and _checked(fake, rows[second])


def test_poll_does_not_take_its_own_old_tick_for_a_new_one(
    home, fake, seed_image
) -> None:
    ctx, (first, _second) = _ready(home, seed_image)
    rows = _rows(ctx)
    _tick(ctx, fake, first)
    # 在 mooya 里重生过一张图：还是这一套、内容变了，旧的确认作废；表格同步还没来得及取消勾
    set_id = scenes.latest_sets(ctx, [first])[first]["set_id"]
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE scene_images SET rev = 'r2' WHERE set_id = ? AND image_id = ("
            "SELECT MIN(image_id) FROM scene_images WHERE set_id = ?)",
            (set_id, set_id),
        )
    assert not downloads.items(ctx, [first])[first]["confirmed"]
    assert _checked(fake, rows[first])
    out = downloads.poll(ctx)
    # 这个勾是程序上次写的，不是人勾了新的一版：不确认，取消勾，回到最上面的「待下载」
    assert out["confirmed"] == [] and out["cleared"] == [first]
    assert not _checked(fake, rows[first])
    assert _order(fake, rows[first]) < downloads.GROUP_WAITING
    # 人再勾上，确认的是新的一版
    assert _tick(ctx, fake, first)["confirmed"] == [first]
    assert downloads.items(ctx, [first])[first]["confirmed"]


def test_writers_take_turns_on_the_rows(
    home, fake, seed_image, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, (first, _second) = _ready(home, seed_image)
    rows = _rows(ctx)
    # 另一个进程正在写「选中的款」的排序，一直不放
    other = reopen(home)
    assert lease.take(other, downloads.WRITE_LEASE, downloads.WRITE_SECONDS)
    monkeypatch.setattr(downloads, "WAIT_SECONDS", 0)
    # 事件照样记下确认，这一行先不写
    out = _tick(ctx, fake, first)
    assert out["confirmed"] == [first] and out["written"] == 0
    assert _order(fake, rows[first]) < downloads.GROUP_WAITING
    # 表格同步也不写、不算失败
    out = feishu_table.sync(ctx)
    assert out["downloads"] == {"skipped": "另一个进程正在写「选中的款」的排序"}
    assert downloads.status(ctx)["attempts"] == 0
    assert _order(fake, rows[first]) < downloads.GROUP_WAITING
    # 那边写完了：下一次同步把这一行沉下去
    lease.drop(other, downloads.WRITE_LEASE)
    assert downloads.has_work(ctx)
    feishu_table.sync(ctx)
    assert _order(fake, rows[first]) >= downloads.GROUP_DONE
    assert not lease.active(ctx, downloads.WRITE_LEASE)


def test_rewriting_a_row_does_not_clear_a_fresh_tick(home, fake, seed_image) -> None:
    ctx, (first, _second) = _ready(home, seed_image)
    rows = _rows(ctx)
    # 人刚勾上、事件还没处理；这一行恰好要重写排序（记录里没有上次写过的值）
    fake.bitable_records[rows[first]][downloads.CONFIRM_FIELD] = True
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM selected_rows WHERE asset_id = ?", (first,))
    feishu_table.sync(ctx)
    assert _checked(fake, rows[first])
    assert downloads.poll(ctx)["confirmed"] == [first]


def test_import_old_marks(home, fake, seed_image, capsys) -> None:
    ctx, picked = _ready(home, seed_image, approve=3, scenes_for=[0, 1])
    a, b, c = picked
    info = feishu_table.base(ctx)
    # 人自己加的「下载情况」多选列，三行都标了「已下载」（c 其实还没出套图）
    fake.bitable_tables[info["table_id"]]["fields"].append(
        {
            "field_name": "下载情况",
            "type": 4,
            "property": {"options": [{"name": "已下载"}]},
        }
    )
    rows = _rows(ctx)
    for asset_id in picked:
        fake.bitable_records[rows[asset_id]]["下载情况"] = ["已下载"]
    argv = ("maintenance", "import-downloads", "--column", "下载情况")
    argv += ("--option", "已下载", "--by", "ops")
    code, out = _cli(capsys, *argv, "--dry-run")
    assert code == 0 and out["dry_run"]
    assert [x["asset_id"] for x in out["confirm"]] == [a, b]
    assert [(x["asset_id"], x["reason"]) for x in out["skipped"]] == [(c, "还没出套图")]
    # a、b 是 B001 交付包里的全部款：确认后这个批次的交付包会自动标已下载
    assert out["would_mark"] == [{"seed_id": "S01", "batch_id": "B001"}]
    assert _sources(ctx) == {}
    code, out = _cli(capsys, *argv)
    assert out["confirmed"] == 2
    assert [p["batch_id"] for p in out["packages"]] == ["B001"]
    assert _sources(ctx) == {a: "migrate", b: "migrate"}
    feishu_table.sync(ctx)
    assert _checked(fake, rows[a]) and not _checked(fake, rows[c])
    # 可以重复执行
    code, out = _cli(capsys, *argv)
    assert out["confirmed"] == 0 and out["confirm"] == []


def test_import_backfills_packages_marked_before(home, fake, seed_image) -> None:
    ctx, (first, second) = _ready(home, seed_image, confirm=False)
    # 下载确认上线前标过的交付包：那时候款上没有确认
    packages.mark_downloaded(ctx, "S01", "B001", by=BY, source="card")
    assert _sources(ctx) == {}
    set_config(home, "table.download_confirm", True)
    ctx = reopen(home)
    feishu_table.sync(ctx)
    out = downloads.import_marks(
        ctx, column="下载情况", option="已下载", by="ops", dry_run=True
    )
    assert out["confirm"] == []
    assert sorted(x["asset_id"] for x in out["from_packages"]) == [first, second]
    downloads.import_marks(ctx, column="下载情况", option="已下载", by="ops")
    assert _sources(ctx) == {first: "package", second: "package"}


def test_existing_base_gets_columns_layout_and_sort(home, fake, seed_image) -> None:
    set_config(home, "listing.enabled", True)
    set_config(home, "table.views", True)
    ctx, _picked = _ready(home, seed_image, confirm=False)
    info = feishu_table.base(ctx)
    table_id = info["table_id"]
    names = [f["field_name"] for f in fake.bitable_tables[table_id]["fields"]]
    assert downloads.CONFIRM_FIELD not in names and not info.get("views_sorted")
    # 关着下载确认时，「选中的款」里的勾不处理
    assert candidates.control_event(
        ctx,
        file_token=info["app_token"],
        table_id=table_id,
        record_ids=list(_rows(ctx).values()),
        by=BY,
    ) == {"ignored": "下载确认关着（table.download_confirm）"}
    set_config(home, "table.download_confirm", True)
    ctx = reopen(home)
    assert feishu_table._has_work(ctx)
    out = feishu_table.sync(ctx)
    assert out["download_columns"]["fields_added"] == [
        downloads.CONFIRM_FIELD,
        downloads.ORDER_FIELD,
    ]
    info = feishu_table.base(ctx)
    # 四个表格视图：「下载确认」排在下载套图后面、「排序」藏起来；五个视图都按「排序」排
    for name in ("全部", "待上架", "上架中", "已上架"):
        visible = fake.visible_names(table_id, name)
        assert visible[:5] == ["素材", "缩略图", "下载原图", "下载套图", "下载确认"]
        assert downloads.ORDER_FIELD not in visible
    for view in fake.bitable_views[table_id]:
        assert fake.bitable_view_sorts[(table_id, view["view_id"])] == [
            {"field": downloads.ORDER_FIELD, "desc": False}
        ]
    assert info["views_sorted"] and info["views_layout"] == feishu_table.LAYOUT_VERSION
    for record_id in _rows(ctx).values():
        assert _order(fake, record_id) < downloads.GROUP_WAITING
    assert not feishu_table._has_work(ctx)


def test_set_fingerprint_matches_the_package_fingerprint(
    home, fake, seed_image
) -> None:
    ctx, (first, _second) = _ready(home, seed_image)
    set_id = scenes.latest_sets(ctx, [first])[first]["set_id"]
    one = packages.set_fingerprints(ctx, [(first, set_id)])[first]
    assert one == packages._fingerprint(
        ctx, [{"asset_id": first, "set": {"set_id": set_id}}]
    )


def test_order_values_keep_the_groups_apart() -> None:
    early, late = "2026-03-01T00:00:00+00:00", "2026-10-27T12:00:00+00:00"
    values = [
        downloads.order_value("pending", late),
        downloads.order_value("pending", early),
        downloads.order_value("waiting", late),
        downloads.order_value("waiting", early),
        downloads.order_value("done", early),
        downloads.order_value("done", late),
    ]
    assert values == sorted(values) and len(set(values)) == len(values)
    assert values[-1] < 2**31
