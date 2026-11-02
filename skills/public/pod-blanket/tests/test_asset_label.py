"""素材的对外名字「编号_方向短语」：两张表的「素材」列、缩略图附件名、下载名（中文走 RFC 5987）、补改已有行。"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from conftest import reopen
from podflow import candidates, feishu_table, maintenance, oss, review, store
from podflow.stages.common import StageError
from podflow.stages.plan import asset_label, asset_label_of
from test_candidates import _record, _setup, spawned  # noqa: F401
from test_storage import BUCKET, _enable_oss
from test_ui import run


def test_label_rules() -> None:
    plan = {"title": "三伙伴开怀大笑", "brief": [{"field_id": "D17", "to": "大笑"}]}
    assert asset_label("IMG00123", plan, None) == "IMG00123_三伙伴开怀大笑"
    # 空白换成下划线，文件名不能有的字符去掉，最长 24 字
    spaced = {**plan, "title": " 三伙伴 / 开怀:大笑? "}
    assert asset_label("IMG00123", spaced, None) == "IMG00123_三伙伴_开怀大笑"
    long = {**plan, "title": "长" * 40}
    assert asset_label("IMG00123", long, None) == "IMG00123_" + "长" * 24
    # 改版前没有简述的方向：短语是变化轴
    assert asset_label("IMG00007", {}, "D05 主题") == "IMG00007_D05_主题"
    # 找不到方向：只有编号
    assert asset_label("IMG00008", {}, None) == "IMG00008"
    assert asset_label("IMG00009", None, None) == "IMG00009"


def test_disposition_and_signature_for_chinese_names(home, fake) -> None:
    assert oss.content_disposition("IMG00123.png") == (
        'attachment; filename="IMG00123.png"'
    )
    name = "IMG00123_三伙伴开怀大笑.png"
    disposition = oss.content_disposition(name)
    # ASCII 兜底是存储里的文件名，浏览器优先用 filename* 里的原名
    assert disposition == (
        'attachment; filename="IMG00123.png"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )
    assert disposition.isascii()
    # 短语里带 ASCII 字符时，给定的兜底名（存储里的文件名）优先
    assert oss.content_disposition("IMG00001_主题方案1.png", "IMG00001.png").startswith(
        'attachment; filename="IMG00001.png"; '
    )

    _enable_oss(home)
    client = store.client(reopen(home))
    url = client.sign_url("pod-blanket/a/IMG00123.png", 600, filename=name)
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    # 链接里解码出来就是这串原值；签名也是签这串原值
    assert query["response-content-disposition"] == disposition
    to_sign = (
        f"GET\n\n\n{query['Expires']}\n/{BUCKET}/pod-blanket/a/IMG00123.png"
        f"?response-content-disposition={disposition}"
    )
    assert (
        query["Signature"]
        == base64.b64encode(
            hmac.new(b"sk-test", to_sign.encode(), hashlib.sha1).digest()
        ).decode()
    )


def _rows(fake, table_id: str) -> list[dict]:
    return [
        fake.bitable_records[r]
        for r, t in fake.bitable_record_tables.items()
        if t == table_id
    ]


def _disposition(link: dict) -> str:
    return parse_qs(urlsplit(link["link"]).query)["response-content-disposition"][0]


def test_candidate_rows_use_the_label(home, fake, seed_image, spawned) -> None:  # noqa: F811
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    table_id = candidates.table_info(ctx, "S01")["table_id"]
    for row in _rows(fake, table_id):
        asset_id = row["素材"].split("_", 1)[0]
        label = asset_label_of(ctx, asset_id)
        assert row["素材"] == label and label != asset_id
        assert row["方向"].split(" ", 1)[1] == label.split("_", 1)[1]
        thumb = fake.bitable_files[row["缩略图"][0]["file_token"]]
        assert thumb["file_name"] == f"{label}.jpg"
        assert f"filename*=UTF-8''{quote(label, safe='')}.png" in _disposition(
            row["下载原图"]
        )


def _old_style(ctx, fake) -> None:
    """把表里的行改回改版前的样子：素材只有编号、缩略图和下载名也是编号。"""
    for record_id, row in fake.bitable_records.items():
        asset_id = row["素材"].split("_", 1)[0]
        row["素材"] = asset_id
        row["缩略图"] = [{"file_token": f"old-{record_id}"}]
        row["下载原图"] = {"link": f"https://old/{asset_id}.png", "text": "旧链接"}


def test_relabel_existing_rows(home, fake, seed_image, spawned, capsys) -> None:  # noqa: F811
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    items = review.list_reviews(ctx, types=["asset_selection"])
    # 一张选中（进选款表、候选表标已选中），一张不要（候选表标没选中，不重签链接）
    picked, dropped = items[0]["subject_id"], items[1]["subject_id"]
    review.decide(ctx, items[0]["review_id"], "approved", by="feishu:ou_tester")
    review.decide(ctx, items[1]["review_id"], "rejected", by="feishu:ou_tester")
    candidates.sync(ctx)
    feishu_table.sync(ctx)
    selection = feishu_table.base(ctx)["table_id"]
    total = len(fake.bitable_records)
    _old_style(ctx, fake)

    dry = run(capsys, "maintenance", "relabel-assets", "--by", "t", "--dry-run")
    assert dry == {
        "dry_run": True,
        "selection_rows": 1,
        "candidate_rows": total - 1,
    }
    assert all("_" not in r["素材"] for r in fake.bitable_records.values())

    calls = len(fake.bitable_calls)
    out = maintenance.relabel_assets(ctx, by="t")
    assert out["failed"] == 0
    assert out["selection"] == {"rows": 1, "updated": 1, "links": 1}
    cand = out["candidates"]
    assert (cand["rows"], cand["updated"]) == (total - 1, total - 1)
    # 没选中的那一行不重签下载链接，其余都重签
    assert cand["links"] == total - 2
    uploads = [
        c for c in fake.bitable_calls[calls:] if c[1] == "/drive/v1/medias/upload_all"
    ]
    assert len(uploads) == total
    for record_id, row in fake.bitable_records.items():
        asset_id = row["素材"].split("_", 1)[0]
        label = asset_label_of(ctx, asset_id)
        assert row["素材"] == label
        assert fake.bitable_files[row["缩略图"][0]["file_token"]]["file_name"] == (
            f"{label}.jpg"
        )
        if asset_id == dropped and fake.bitable_record_tables[record_id] != selection:
            assert row["下载原图"]["link"].startswith("https://old/")
        else:
            assert f"{quote(label, safe='')}.png" in _disposition(row["下载原图"])
    # 选中的那张在两张表里都改了
    assert {
        fake.bitable_record_tables[r]
        for r, row in fake.bitable_records.items()
        if row["素材"].startswith(f"{picked}_")
    } == {selection, candidates.table_info(ctx, "S01")["table_id"]}
    assert ctx.db.scalar(
        "SELECT link_expires_at FROM candidate_rows WHERE asset_id = ?", (picked,)
    )
    assert ctx.db.q1("SELECT payload_json FROM events WHERE event = 'assets_relabeled'")
    assert _record(ctx, picked)

    # 可以重复执行：结果一样
    again = maintenance.relabel_assets(ctx, by="t")
    assert (again["failed"], again["selection"]["updated"]) == (0, 1)
    assert all(
        row["素材"] == asset_label_of(ctx, row["素材"].split("_", 1)[0])
        for row in fake.bitable_records.values()
    )


def test_relabel_needs_the_table(home, fake) -> None:
    ctx = reopen(home)
    with pytest.raises(StageError, match="table.enabled"):
        maintenance.relabel_assets(ctx, by="t")
