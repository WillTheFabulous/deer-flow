"""素材存储：OSS 为准 + 本机缓存（登记、后台上传、重试与冲突、拉回校验、清理），以及 OSS V1 签名。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import reopen, set_config
from podflow import bg, cli, oss, review, store, ui
from podflow.stages import seed as seedmod
from podflow.util import dumps, iso, utcnow
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_ui import run

BUCKET = "test-bucket"


def _enable_oss(home, **overrides) -> None:
    conf = {
        "bucket": BUCKET,
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "public_endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "prefix": "pod-blanket/",
        "upload_rate_kbps": 0,
        "timeout_seconds": 10,
        **overrides,
    }
    set_config(home, "storage", {"backend": "oss", "oss": conf})
    secrets = home / "secrets.env"
    secrets.write_text(
        secrets.read_text(encoding="utf-8")
        + "OSS_ACCESS_KEY_ID=ak-test\nOSS_ACCESS_KEY_SECRET=sk-test\n",
        encoding="utf-8",
    )


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bg, "_current", None)
    return calls


@pytest.fixture
def octx(home, fake, spawned):
    _enable_oss(home)
    c = reopen(home)
    yield c
    c.db.close()


def _objects(ctx) -> dict[str, dict]:
    return {r["path"]: dict(r) for r in ctx.db.q("SELECT * FROM storage_objects")}


def _set_pending(ctx, path: str) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE storage_objects SET status = 'pending', attempts = 0 WHERE path = ?",
            (path,),
        )


def test_new_files_are_registered_and_uploaded_by_one_sync(
    octx, fake, seed_image, spawned
) -> None:
    bootstrap(octx, seed_image, limit=1)
    Worker(octx, max_minutes=5).run()
    objects = _objects(octx)
    assert objects and all(o["status"] == "pending" for o in objects.values())
    assert not fake.oss_objects
    # 第一次写入就拉起后台上传；它还在跑时不再重复拉起
    assert [argv[2:] for argv in spawned] == [["storage", "sync"]]
    assert {p.split("/")[1] for p in objects} == {"seeds", "families"}
    assert any(p.endswith(".manifest.json") for p in objects)
    assert any(p.endswith(".thumb.jpg") for p in objects)

    result = store.sync(octx)
    assert (
        result["uploaded"] == len(objects)
        and result["pending"] == 0
        and not result["errors"]
    )
    assert set(fake.oss_objects) == {f"pod-blanket/{p}" for p in objects}
    for path, row in _objects(octx).items():
        body, meta = fake.oss_objects[f"pod-blanket/{path}"]
        assert row["status"] == "uploaded" and row["uploaded_at"]
        assert body == store.resolve(octx.home, path).read_bytes()
        assert (
            meta["x-oss-meta-sha256"]
            == row["sha256"]
            == hashlib.sha256(body).hexdigest()
        )
    puts = [h for m, _, h in fake.oss_requests if m == "PUT"]
    assert all(h["x-oss-forbid-overwrite"] == "true" for h in puts)
    assert store.status(octx)["objects"]["uploaded"]["count"] == len(objects)


def test_missing_local_copy_is_pulled_back_and_verified(octx, fake, seed_image) -> None:
    bootstrap(octx, seed_image, limit=1)
    Worker(octx, max_minutes=5).run()
    store.sync(octx)
    asset = dict(octx.db.q1("SELECT * FROM assets ORDER BY asset_id LIMIT 1"))
    local = store.resolve(octx.home, asset["path"])
    original = local.read_bytes()
    local.unlink()
    assert store.ensure_local(octx, asset["path"]).read_bytes() == original

    # 选中后发原图：本机副本被清掉也能从 OSS 拉回
    rid = review.find_review(octx, "asset_selection", asset["asset_id"])
    review.decide(octx, rid, "approved", by="tester")
    local.unlink()
    files = ui.originals(octx, [asset["asset_id"]])["files"]
    assert files and open(files[0]["path"], "rb").read() == original

    key = f"pod-blanket/{asset['path']}"
    local.unlink()
    fake.oss_objects[key] = (b"tampered", fake.oss_objects[key][1])
    with pytest.raises(store.StorageError, match="sha256"):
        store.ensure_local(octx, asset["path"])
    assert not local.exists()
    # OSS 上也没有：原样返回不存在的路径，由调用方按缺图处理
    del fake.oss_objects[key]
    assert not store.ensure_local(octx, asset["path"]).exists()


def test_failed_uploads_are_retried_and_conflicts_are_not_overwritten(
    octx, fake, seed_image
) -> None:
    seedmod.add_seed(octx, seed_image, source_ref="测试参考图")
    (path,) = _objects(octx)
    key = f"pod-blanket/{path}"

    fake.oss_fail_puts = 1
    result = store.sync(octx)
    assert result["uploaded"] == 0 and result["failed"] == 1 and result["pending"] == 1
    row = _objects(octx)[path]
    assert row["attempts"] == 1 and "503" in row["last_error"]
    assert store.sync(octx)["uploaded"] == 1

    # 上次其实传成功了只是没记账：409 后核对 sha256 一致，记为已上传
    _set_pending(octx, path)
    assert store.sync(octx)["uploaded"] == 1
    assert _objects(octx)[path]["status"] == "uploaded"

    # 同名但内容不同：记失败，绝不覆盖
    fake.oss_objects[key] = (b"other", {"x-oss-meta-sha256": "deadbeef"})
    _set_pending(octx, path)
    result = store.sync(octx)
    assert result["failed"] == 1 and "内容不同" in result["errors"][0]["error"]
    assert fake.oss_objects[key][0] == b"other"

    # 失败到上限后不再自动重试，--retry 清零再来
    with octx.db.tx() as conn:
        conn.execute(
            "UPDATE storage_objects SET attempts = ? WHERE path = ?",
            (store.MAX_UPLOAD_ATTEMPTS, path),
        )
    assert (
        store.pending_count(octx) == 0
        and store.status(octx)["stuck"][0]["path"] == path
    )
    del fake.oss_objects[key]
    assert store.sync(octx, retry=True)["uploaded"] == 1


def test_only_one_uploader_at_a_time(octx, fake, seed_image, spawned) -> None:
    with octx.db.tx() as conn:
        lease = {
            "owner": "someone-else",
            "until": iso(utcnow().replace(year=utcnow().year + 1)),
        }
        octx.db.set_meta(conn, store.UPLOADER_LEASE_KEY, dumps(lease))
    seedmod.add_seed(octx, seed_image, source_ref="测试参考图")
    assert spawned == []
    assert store.kick(octx) is None
    assert store.sync(octx)["skipped"] is True and not fake.oss_objects

    with octx.db.tx() as conn:
        octx.db.set_meta(
            conn,
            store.UPLOADER_LEASE_KEY,
            dumps({"owner": "someone-else", "until": iso(utcnow())}),
        )
    assert store.sync(octx)["uploaded"] == 1
    assert octx.db.get_meta(store.UPLOADER_LEASE_KEY) is None


def test_prune_keeps_pending_and_recent_files(octx, fake, seed_image) -> None:
    seedmod.add_seed(octx, seed_image, source_ref="测试参考图")
    store.sync(octx)
    (uploaded,) = _objects(octx)
    extra = octx.home / "assets" / "seeds" / "S01" / "note.json"
    extra.write_text("{}", encoding="utf-8")
    with octx.db.tx() as conn:
        store.register(octx, conn, extra)
    old = time.time() - 40 * 86400
    for p in (store.resolve(octx.home, uploaded), extra):
        os.utime(p, (old, old))

    dry = store.prune(octx, older_than_days=30, dry_run=True)
    assert dry["removed"] == 1 and store.resolve(octx.home, uploaded).exists()
    assert store.prune(octx, older_than_days=30)["removed"] == 1
    assert not store.resolve(octx.home, uploaded).exists() and extra.exists()
    assert store.ensure_local(octx, uploaded).exists()


def test_local_backend_registers_nothing_and_refuses_prune(ctx, seed_image) -> None:
    seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    assert ctx.db.scalar("SELECT COUNT(*) FROM storage_objects") == 0
    assert store.kick(ctx) is None
    with pytest.raises(store.StorageError):
        store.prune(ctx)


def test_storage_cli_migrates_existing_files_and_doctor_probes_oss(
    home, fake, seed_image, capsys
) -> None:
    # 迁移场景：素材早就在本机，开 OSS 之前从没登记过
    c = reopen(home)
    seedmod.add_seed(c, seed_image, source_ref="测试参考图")
    c.db.close()
    _enable_oss(home)
    out = run(capsys, "storage", "sync", "--all")
    assert out["registered"] == 1 and out["uploaded"] == 1 and out["pending"] == 0
    status = run(capsys, "storage", "status")
    assert status["backend"] == "oss" and status["objects"]["uploaded"]["count"] == 1
    assert run(capsys, "storage", "sync", "--all")["registered"] == 0

    assert cli.main(["doctor"]) in (0, 1)
    checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    probe = checks["OSS 可写可读（写入、读回、删除探针对象）"]
    assert probe["ok"] is True and probe["detail"].startswith("pod-blanket/_doctor/")
    assert not [k for k in fake.oss_objects if "_doctor" in k]
    assert checks["业务库可用"]["ok"] is True


def test_requests_are_signed_like_oss_v1(octx, fake) -> None:
    client = store.client(octx)
    client.put(
        "pod-blanket/x.txt", b"hello", content_type="text/plain", meta={"sha256": "abc"}
    )
    method, key, headers = fake.oss_requests[-1]
    md5 = base64.b64encode(hashlib.md5(b"hello").digest()).decode()
    to_sign = (
        f"PUT\n{md5}\ntext/plain\n{headers['date']}\n"
        "x-oss-forbid-overwrite:true\nx-oss-meta-sha256:abc\n"
        f"/{BUCKET}/pod-blanket/x.txt"
    )
    expected = base64.b64encode(
        hmac.new(b"sk-test", to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    assert (method, key) == ("PUT", "pod-blanket/x.txt")
    assert headers["authorization"] == f"OSS ak-test:{expected}"

    url = client.sign_url("pod-blanket/x.txt", 600)
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert (
        parts.netloc == f"{BUCKET}.oss-cn-hangzhou.aliyuncs.com"
        and query["OSSAccessKeyId"] == "ak-test"
    )
    to_sign = f"GET\n\n\n{query['Expires']}\n/{BUCKET}/pod-blanket/x.txt"
    assert (
        query["Signature"]
        == base64.b64encode(
            hmac.new(b"sk-test", to_sign.encode(), hashlib.sha1).digest()
        ).decode()
    )

    # 带文件名：浏览器直接下载成这个名字，覆盖参数一起签名
    url = client.sign_url("pod-blanket/x.png", 600, filename="x.png")
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    disposition = 'attachment; filename="x.png"'
    assert query["response-content-disposition"] == disposition
    to_sign = (
        f"GET\n\n\n{query['Expires']}\n/{BUCKET}/pod-blanket/x.png"
        f"?response-content-disposition={disposition}"
    )
    assert (
        query["Signature"]
        == base64.b64encode(
            hmac.new(b"sk-test", to_sign.encode(), hashlib.sha1).digest()
        ).decode()
    )


def test_rate_limited_upload_streams_with_content_length(
    octx, fake, monkeypatch
) -> None:
    waited: list[int] = []
    monkeypatch.setattr(oss._PACER, "wait", lambda nbytes, rate: waited.append(nbytes))
    _enable_oss(octx.home, upload_rate_kbps=300)
    client = store.client(reopen(octx.home))
    data = os.urandom(200 * 1024)
    client.put("pod-blanket/big.bin", data)
    _, _, headers = fake.oss_requests[-1]
    assert sum(waited) == len(data) and len(waited) == 4
    assert (
        headers["content-length"] == str(len(data))
        and "transfer-encoding" not in headers
    )
    assert fake.oss_objects["pod-blanket/big.bin"][0] == data
