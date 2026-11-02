"""Fork 飞书 POD 工作台测试（app/channels/fork/pod_bridge.py、pod_cards.py）。

覆盖：
- PodBridge：定位 podctl、解析 JSON、业务错误 / 超时 / 脚本缺失转 PodctlError
- 卡片构建：工作台按钮、下一步 / 进度、待审项缩略图布局与决策按钮、选款的修改表单、翻页与类型筛选、最近动态、已选款
- 按钮回调：先回 toast、主循环里调 podctl 后 patch 原卡片、缩略图 image_key 缓存、单聊限制
- 后台动作：裂变 / 出图转后台、连续运行开关、发原图文件，参数校验
- 裂变方向：挑选卡的勾选清单表单（form_value 一次提交、旧卡片兼容）、任务明细卡（原方向记录卡）与带要求的再策划
- 任务列表：一张参考图一段（缩略图、进度、按钮）、已完成任务的「继续裂变」表单、翻页，策划提交后回到任务列表
- 进入 / 返回 POD 专属对话：切换已有会话或新建、记录并清除返回指针
- 定时巡检：进程内只起一个、出错退避、跟随渠道 start / stop
- ChannelStore 的 pod_return_thread 不进会话快照；已绑定账号新建线程时会话快照记对人设
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.fork import pod_bridge, pod_cards
from app.channels.fork.feishu import ChatIdentity, ForkFeishuChannel
from app.channels.fork.manager import CARD_ACTION_SOURCE, ForkChannelManager
from app.channels.fork.pod_bridge import PodBridge, PodctlError
from app.channels.fork.pod_cards import (
    BACKGROUND_NOTICE,
    POD_PERSONA,
    PodCardsMixin,
    build_pod_dashboard_card,
    build_pod_directions_card,
    build_pod_error_card,
    build_pod_gallery_card,
    build_pod_history_card,
    build_pod_reviews_card,
    build_pod_tasks_card,
    build_pod_timeline_card,
)
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus
from app.channels.store import ChannelStore


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _buttons(card: dict) -> list[dict]:
    found: list[dict] = []

    def walk(elements):
        for element in elements:
            if element.get("tag") == "action":
                found.extend(element["actions"])
            elif element.get("tag") == "button":
                found.append(element)
            elif element.get("tag") == "form":
                walk(element["elements"])
            for column in element.get("columns") or []:
                walk(column["elements"])

    walk(card["elements"])
    return found


def _labels(card: dict) -> list[str]:
    return [b["text"]["content"] for b in _buttons(card)]


DASHBOARD = {
    "title": "POD 毛毯工作台",
    "level": "normal",
    "lines": ["**状态**：运行中", "**待审**：选款 2"],
    "alerts": [],
    "notes": ["预算控制已关闭"],
    "paused": False,
    "open_reviews": 2,
    "review_types": [{"type": "asset_selection", "label": "选款", "count": 2}],
}


def _item(review_id: str, images: list[str], *, actions=None, hint=None) -> dict:
    return {
        "review_id": review_id,
        "type": "asset_dup",
        "type_label": "疑似重复",
        "subject_id": "IMG1",
        "title": f"{review_id} · 疑似重复 · IMG1",
        "lines": ["V0001 · flare"],
        "images": [{"path": p, "label": f"图 {i}"} for i, p in enumerate(images)],
        "actions": actions if actions is not None else [{"decision": "approved", "label": "不是重复", "style": "primary"}, {"decision": "rejected", "label": "是重复", "style": "danger"}],
        "hint": hint,
    }


def _page(items: list[dict], *, total: int | None = None, offset: int = 0, types: list[str] | None = None) -> dict:
    return {
        "title": "POD 待审",
        "total": len(items) if total is None else total,
        "offset": offset,
        "limit": 3,
        "types": types or [],
        "type_counts": [{"type": "asset_dup", "label": "疑似重复", "count": 4}, {"type": "asset_selection", "label": "选款", "count": 2}],
        "items": items,
    }


# ---------------------------------------------------------------------------
# PodBridge
# ---------------------------------------------------------------------------


def _fake_skill(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "skill" / "scripts" / "podctl.py"
    script.parent.mkdir(parents=True)
    script.write_text(body, encoding="utf-8")
    return script.parent.parent


class TestPodBridge:
    def test_runs_podctl_and_parses_json(self, tmp_path, monkeypatch):
        root = _fake_skill(tmp_path, "import json, sys\nprint(json.dumps({'argv': sys.argv[1:], 'ok': True}))\n")
        monkeypatch.setenv("PODFLOW_SKILL_ROOT", str(root))
        assert pod_bridge.podctl_path() == root / "scripts" / "podctl.py"
        assert _run(PodBridge().run("ui", "dashboard")) == {"argv": ["ui", "dashboard"], "ok": True}

    def test_engine_error_becomes_podctl_error(self, tmp_path, monkeypatch):
        body = "import json, sys\nprint(json.dumps({'error': 'review_error', 'detail': 'RV0001 已是 decided'}))\nsys.exit(2)\n"
        monkeypatch.setenv("PODFLOW_SKILL_ROOT", str(_fake_skill(tmp_path, body)))
        with pytest.raises(PodctlError, match="RV0001 已是 decided"):
            _run(PodBridge().run("review", "decide", "RV0001"))

    def test_crash_reports_last_stderr_line(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PODFLOW_SKILL_ROOT", str(_fake_skill(tmp_path, "raise SystemExit('boom: db locked')\n")))
        with pytest.raises(PodctlError, match="boom: db locked"):
            _run(PodBridge().run("status"))

    def test_timeout_and_missing_script(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PODFLOW_SKILL_ROOT", str(_fake_skill(tmp_path, "import time\ntime.sleep(5)\n")))
        with pytest.raises(PodctlError, match="超时"):
            _run(PodBridge(timeout=0.3).run("tick"))
        monkeypatch.setenv("PODFLOW_SKILL_ROOT", str(tmp_path / "nowhere"))
        with pytest.raises(PodctlError, match="找不到"):
            _run(PodBridge().run("status"))

    def test_default_path_follows_project_root(self, monkeypatch):
        monkeypatch.delenv("PODFLOW_SKILL_ROOT", raising=False)
        monkeypatch.setenv("DEER_FLOW_PROJECT_ROOT", "/app")
        assert pod_bridge.podctl_path() == Path("/app/skills/public/pod-blanket/scripts/podctl.py")
        assert sys.executable  # 子进程沿用 gateway 的解释器（venv 里装着引擎依赖）


# ---------------------------------------------------------------------------
# 卡片构建
# ---------------------------------------------------------------------------


class TestPodCardBuilders:
    def test_dashboard_buttons_follow_state(self):
        card = build_pod_dashboard_card(DASHBOARD, notice="**巡检结论**：窗口未开")
        assert card["config"]["update_multi"] is True and card["header"]["template"] == "blue"
        assert _labels(card) == ["待审（2）", "任务列表", "刷新", "巡检一次", "暂停", "最近动态", "已选款", "日报", "开启连续运行", "进入 POD 对话", "返回原会话"]
        assert next(b for b in _buttons(card) if b["text"]["content"] == "任务列表")["value"] == {"action": "pod_tasks"}
        assert card["elements"][0]["content"] == "**巡检结论**：窗口未开"
        paused = build_pod_dashboard_card({**DASHBOARD, "paused": True, "level": "paused", "alerts": ["积压"], "trial_active": True})
        assert "恢复" in _labels(paused) and paused["header"]["template"] == "orange"
        assert any("**异常**" in e.get("content", "") for e in paused["elements"])
        stop = next(b for b in _buttons(paused) if b.get("value", {}).get("action") == "pod_trial_stop")
        assert stop["text"]["content"] == "停止连续运行"

        # 选款表建好后：「已选款」后面多一个「选款表」，点了直接打开表格（链接按钮，不走回调），正好 12 个、每行 4 个
        url = "https://example.feishu.cn/base/bascn1"
        linked = build_pod_dashboard_card({**DASHBOARD, "table_url": url})
        assert _labels(linked) == ["待审（2）", "任务列表", "刷新", "巡检一次", "暂停", "最近动态", "已选款", "选款表", "日报", "开启连续运行", "进入 POD 对话", "返回原会话"]
        table = next(b for b in _buttons(linked) if b["text"]["content"] == "选款表")
        assert table == {"tag": "button", "text": {"tag": "plain_text", "content": "选款表"}, "type": "default", "url": url}
        rows = [e["actions"] for e in linked["elements"] if e["tag"] == "action"]
        assert [len(r) for r in rows] == [4, 4, 4] and rows[1][-1] is not None and rows[1][-1]["text"]["content"] == "选款表"
        # 不是 https 的链接不画
        bogus = build_pod_dashboard_card({**DASHBOARD, "table_url": "javascript:alert(1)"})
        assert "选款表" not in _labels(bogus) and not [b for b in _buttons(bogus) if "url" in b]

    def test_dashboard_renders_progress_and_next_step_buttons(self):
        view = {
            **DASHBOARD,
            "progress": ["F01 裂变中（已用 2 分钟）", "B001（F01，12 个任务）：已完成 3，待出图 9"],
            "next_steps": [
                {"text": "系列 F02 已批准，可以裂变下一批", "actions": [{"action": "pod_plan", "label": "裂变 3 个（试跑）", "style": "primary", "family_id": "F02", "limit": 3}]},
                {"text": "暂时没有需要你处理的事", "actions": []},
            ],
        }
        card = build_pod_dashboard_card(view)
        contents = [e.get("content", "") for e in card["elements"]]
        assert contents[0] == "**进度**\n- F01 裂变中（已用 2 分钟）\n- B001（F01，12 个任务）：已完成 3，待出图 9"
        assert contents[1:4] == ["**下一步**", "1. 系列 F02 已批准，可以裂变下一批", ""]
        plan = _buttons(card)[0]
        # 渲染用的 label / style 不回传，其余参数原样作为回调值
        assert plan["value"] == {"action": "pod_plan", "family_id": "F02", "limit": 3} and plan["type"] == "primary"
        assert "2. 暂时没有需要你处理的事" in contents

    def test_timeline_and_gallery_cards(self):
        timeline = build_pod_timeline_card({"title": "POD 最近动态", "lines": ["`10-08 13:48` 出图一轮结束"]})
        assert _labels(timeline) == ["刷新", "返回工作台"] and "出图一轮结束" in timeline["elements"][0]["content"]

        page = {
            "title": "POD 已选款",
            "total": 4,
            "offset": 3,
            "limit": 3,
            "batch_id": "B001",
            "items": [{"asset_id": "IMG00007", "title": "IMG00007 · V0002", "lines": ["B001 · 配色"], "images": [{"path": "/t.jpg", "label": "IMG00007"}]}],
        }
        card = build_pod_gallery_card(page, {"/t.jpg": "img_t"}, notice="已发送原图：IMG00007")
        assert card["header"]["title"]["content"] == "POD 已选款（4）" and card["elements"][0]["content"] == "已发送原图：IMG00007"
        assert [e for e in card["elements"] if e["tag"] == "column_set"][0]["columns"][0]["elements"][0]["img_key"] == "img_t"
        send = next(b for b in _buttons(card) if b["value"]["action"] == "pod_send_originals")
        assert send["value"] == {"action": "pod_send_originals", "asset_id": "IMG00007", "offset": 3, "batch": "B001"}
        assert "上一页" in _labels(card) and "下一页" not in _labels(card)
        assert not [b for b in _buttons(card) if "url" in b]
        empty = build_pod_gallery_card({"items": [], "total": 0}, {})
        assert "还没有选中的款" in json.dumps(empty, ensure_ascii=False)

        # 选款表建好后：第一个按钮直接打开表格（链接按钮不走回调）；不是 https 的链接不渲染
        linked = build_pod_gallery_card({**page, "table_url": "https://example.feishu.cn/base/bascn1"}, {"/t.jpg": "img_t"})
        first = next(e for e in linked["elements"] if e["tag"] == "action" and any("url" in b for b in e["actions"]))["actions"][0]
        assert first == {"tag": "button", "text": {"tag": "plain_text", "content": "打开选款表"}, "type": "primary", "url": "https://example.feishu.cn/base/bascn1"}
        bogus = build_pod_gallery_card({**page, "table_url": "javascript:alert(1)"}, {"/t.jpg": "img_t"})
        assert not [b for b in _buttons(bogus) if "url" in b]

    def test_review_items_render_thumbnails_and_decisions(self):
        page = _page([_item("RV0001", ["/a.jpg"], hint="要改图：进入 POD 对话"), _item("RV0002", ["/b.jpg", "/c.jpg"])], total=6, offset=3, types=["asset_dup"])
        card = build_pod_reviews_card(page, {"/a.jpg": "img_a", "/b.jpg": "img_b", "/c.jpg": "img_c"})
        sets = [e for e in card["elements"] if e["tag"] == "column_set"]
        # 单图：图左文右；两张图：并排
        assert sets[0]["columns"][0]["elements"][0]["img_key"] == "img_a" and sets[0]["columns"][1]["elements"][0]["tag"] == "markdown"
        assert [c["elements"][0]["img_key"] for c in sets[1]["columns"]] == ["img_b", "img_c"]
        decide = [b["value"] for b in _buttons(card) if b["value"]["action"] == "pod_decide"]
        assert decide[0] == {"action": "pod_decide", "review_id": "RV0001", "decision": "approved", "offset": 3, "type": "asset_dup"}
        labels = _labels(card)
        assert "✓ 疑似重复 4" in labels and "全部 6" in labels and "上一页" in labels and "下一页" not in labels
        assert any(e["tag"] == "note" and "第 4–6 项，共 6 项" in e["elements"][0]["content"] for e in card["elements"])

    def test_selection_item_shows_original_first_then_direction(self):
        item = {
            **_item("RV0021", ["/seed.jpg", "/cand.jpg"]),
            "title": "RV0021 · 选款 · IMG00042",
            "lines": ["方向：V0012 极地冷色三伙伴（组合 · 改 2 处）· B003", "· 主体：狐狸 → 北极狐", "不变：三只陪伴", "出图：nano-banana-pro · 图生图"],
        }
        item["images"] = [{"path": "/seed.jpg", "label": "原图 S01"}, {"path": "/cand.jpg", "label": "候选 IMG00042"}]
        card = build_pod_reviews_card(_page([item], types=["asset_selection"]), {"/seed.jpg": "img_seed", "/cand.jpg": "img_cand"})
        # 标题 → 图片并排（原图在左）→ 方向、模型等说明 → 决策按钮
        hr = next(i for i, e in enumerate(card["elements"]) if e["tag"] == "hr")
        title, row, text, actions = card["elements"][hr + 1 : hr + 5]
        assert title == {"tag": "markdown", "content": "**RV0021 · 选款 · IMG00042**"}
        assert [c["elements"][0]["img_key"] for c in row["columns"]] == ["img_seed", "img_cand"]
        assert [c["elements"][1]["content"] for c in row["columns"]] == ["原图 S01", "候选 IMG00042"]
        assert text["content"].startswith("方向：V0012 极地冷色三伙伴") and text["content"].endswith("出图：nano-banana-pro · 图生图")
        assert actions["tag"] == "action"
        # 原图缩略图没传上去时退回单图布局（图左文右）
        single = build_pod_reviews_card(_page([item], types=["asset_selection"]), {"/cand.jpg": "img_cand"})
        sets = [e for e in single["elements"] if e["tag"] == "column_set"]
        assert sets[0]["columns"][0]["elements"][0]["img_key"] == "img_cand" and "方向：V0012" in sets[0]["columns"][1]["elements"][0]["content"]

    def test_selection_fix_form_sits_at_card_root(self):
        item = _item("RV0007", ["/a.jpg"])
        item["form"] = {
            "name": "fix_RV0007",
            "input": "note_RV0007",
            "placeholder": "要改什么？",
            "default": "按质检疑点修正：右下爪子",
            "max_length": 300,
            "submit_label": "修改",
            "decision": "needs_changes",
        }
        card = build_pod_reviews_card(_page([item], types=["asset_selection"]), {"/a.jpg": "img_a"})
        # 卡片 JSON 1.0 的表单容器只能放在根节点
        forms = [e for e in card["elements"] if e["tag"] == "form"]
        assert len(forms) == 1 and forms[0]["name"] == "fix_RV0007" and forms[0]["fallback"]["tag"] == "fallback_text"
        field, submit = forms[0]["elements"]
        assert (field["tag"], field["name"], field["required"], field["max_length"]) == ("input", "note_RV0007", True, 300)
        assert field["default_value"] == "按质检疑点修正：右下爪子"
        assert (submit["action_type"], submit["name"], submit["text"]["content"]) == ("form_submit", "fix_RV0007_submit", "修改")
        assert submit["value"] == {"action": "pod_decide", "review_id": "RV0007", "decision": "needs_changes", "field": "note_RV0007", "offset": 0, "type": "asset_selection"}
        plain = build_pod_reviews_card(_page([_item("RV0001", [])]), {})
        assert not [e for e in plain["elements"] if e["tag"] == "form"]

    def test_missing_image_key_and_empty_page(self):
        card = build_pod_reviews_card(_page([_item("RV0001", ["/gone.jpg"], actions=[])]), {})
        assert not [e for e in card["elements"] if e["tag"] == "column_set"]
        assert "pod_decide" not in json.dumps(card)
        empty = build_pod_reviews_card(_page([], total=0), {})
        assert "这里没有待审项了" in json.dumps(empty, ensure_ascii=False) and "返回工作台" in _labels(empty)

    def test_error_card_offers_reopen(self):
        card = build_pod_error_card("podctl 超时")
        assert card["header"]["template"] == "red" and _labels(card) == ["重新打开工作台"]


# ---------------------------------------------------------------------------
# 混入：按钮回调与会话切换（不依赖飞书 SDK 的最小宿主）
# ---------------------------------------------------------------------------


class _FakeBridge:
    def __init__(self, responses: dict[tuple[str, ...], object]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *args: str) -> dict:
        self.calls.append(args)
        for size in (2, 1):
            if args[:size] in self.responses:
                response = self.responses[args[:size]]
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected podctl call {args}")


class _Host(PodCardsMixin):
    name = "feishu"

    def __init__(self, store: ChannelStore, bridge: _FakeBridge):
        self._store = store
        self._pod_bridge = bridge
        self.scheduled: list = []
        self.published: list[tuple] = []
        self.patched: list[tuple[str, dict]] = []
        self.uploads: list[str] = []
        self.files: list[tuple] = []
        self.send_ok = True

    def _channel_store(self):
        return self._store

    def _schedule(self, coroutine, *, name: str, msg_id: str) -> None:
        self.scheduled.append(coroutine)

    def _publish_synthetic_command(self, chat_id, open_id, text, *, source):
        self.published.append((chat_id, text, source))

    async def _upload_image(self, path) -> str:
        self.uploads.append(path)
        return f"key-{Path(path).name}"

    async def _patch_card(self, message_id: str, card: dict) -> None:
        self.patched.append((message_id, card))

    async def send_file(self, msg, attachment) -> bool:
        self.files.append((msg, attachment))
        return self.send_ok

    @staticmethod
    def _build_card_content(text: str) -> str:
        return json.dumps({"text": text}, ensure_ascii=False)

    def drain(self) -> None:
        for coroutine in self.scheduled:
            _run(coroutine)
        self.scheduled.clear()


_IDENTITY = ChatIdentity(bound=True, binding_required=True, storage_user_id="admin-1", current_thread_id="t-main")


def _ctx(store, value: dict, *, identity: ChatIdentity = _IDENTITY, chat_id: str = "chat-1", message_id: str | None = "om-card", form_value: dict | None = None):
    return SimpleNamespace(store=store, chat_id=chat_id, open_id="ou_x", identity=identity, value=value, message_id=message_id, form_value=form_value)


@pytest.fixture
def store(tmp_path: Path) -> ChannelStore:
    s = ChannelStore(path=tmp_path / "store.json")
    s.set_user_chat("feishu", "ou_x", "chat-1")
    return s


class TestPodCardActions:
    def test_reviews_action_toasts_then_patches_with_cached_thumbnails(self, store, tmp_path):
        thumb = tmp_path / "IMG1.thumb.jpg"
        thumb.write_bytes(b"jpg")
        bridge = _FakeBridge({("ui", "reviews"): _page([_item("RV0001", [str(thumb)])])})
        host = _Host(store, bridge)

        payload = host._card_action_pod_reviews(_ctx(store, {"action": "pod_reviews", "offset": 3, "type": "asset_dup"}))
        assert payload["toast"]["type"] == "info" and not host.patched
        host.drain()
        assert bridge.calls[0] == ("ui", "reviews", "--offset", "3", "--limit", "3", "--type", "asset_dup")
        message_id, card = host.patched[0]
        assert message_id == "om-card" and "key-IMG1.thumb.jpg" in json.dumps(card)

        host._card_action_pod_reviews(_ctx(store, {"action": "pod_reviews"}))
        host.drain()
        assert host.uploads == [str(thumb)]  # 同一张缩略图只上传一次

    def test_decide_forwards_decision_and_reports_engine_refusal(self, store):
        bridge = _FakeBridge({("review", "decide"): {"review_id": "RV0001"}, ("ui", "reviews"): _page([])})
        host = _Host(store, bridge)
        host._card_action_pod_decide(_ctx(store, {"review_id": "RV0001", "decision": "approved", "offset": 0}))
        host.drain()
        assert bridge.calls[0] == ("review", "decide", "RV0001", "--decision", "approved", "--by", "feishu:ou_x")
        assert "已处理 **RV0001**" in json.dumps(host.patched[-1][1], ensure_ascii=False)

        bridge.responses[("review", "decide")] = PodctlError("RV0001 已是 decided")
        host._card_action_pod_decide(_ctx(store, {"review_id": "RV0001", "decision": "approved"}))
        host.drain()
        assert "没有处理：RV0001 已是 decided" in json.dumps(host.patched[-1][1], ensure_ascii=False)

    def test_decide_notice_names_the_subject(self, store):
        # 引擎给了审核对象的名字：提示里写「RV0021（IMG00042_三伙伴开怀大笑）」，看得出处理的是哪张图
        decided = {"review_id": "RV0021", "subject_id": "IMG00042", "label": "IMG00042_三伙伴开怀大笑"}
        bridge = _FakeBridge({("review", "decide"): decided, ("ui", "reviews"): _page([])})
        host = _Host(store, bridge)
        host._card_action_pod_decide(_ctx(store, {"review_id": "RV0021", "decision": "approved", "offset": 0}))
        host.drain()
        assert "已处理 **RV0021**（IMG00042_三伙伴开怀大笑）" in json.dumps(host.patched[-1][1], ensure_ascii=False)

        bridge.responses[("review", "decide")] = {**decided, "effect": {"fix_queued": True, "reason": "流水线已暂停"}}
        host._card_action_pod_decide(_ctx(store, {"review_id": "RV0021", "decision": "needs_changes", "field": "note_RV0021"}, form_value={"note_RV0021": "背景换成雾蓝"}))
        host.drain()
        assert "**RV0021**（IMG00042_三伙伴开怀大笑） 的修改已排队：流水线已暂停，恢复后接着改" in json.dumps(host.patched[-1][1], ensure_ascii=False)

    def test_fix_form_note_goes_to_engine(self, store):
        bridge = _FakeBridge({("review", "decide"): {"review_id": "RV0007", "effect": {"fix_started": "BG0009"}}, ("ui", "reviews"): _page([])})
        host = _Host(store, bridge)
        value = {"review_id": "RV0007", "decision": "needs_changes", "field": "note_RV0007", "offset": 0, "type": "asset_selection"}
        # 没填意见、字段名不对：只提示，不调引擎
        assert host._card_action_pod_decide(_ctx(store, value, form_value={"note_RV0007": "  "}))["toast"]["type"] == "warning"
        assert host._card_action_pod_decide(_ctx(store, {**value, "field": "note_x; rm"}, form_value={"note_x; rm": "改"}))["toast"]["type"] == "warning"
        assert host._card_action_pod_decide(_ctx(store, value))["toast"]["type"] == "warning"
        assert host.scheduled == []

        host._card_action_pod_decide(_ctx(store, value, form_value={"note_RV0007": " 兔子耳朵改短 "}))
        host.drain()
        assert bridge.calls[0] == ("review", "decide", "RV0007", "--decision", "needs_changes", "--by", "feishu:ou_x", "--note", "兔子耳朵改短")
        assert "已开始修改" in json.dumps(host.patched[-1][1], ensure_ascii=False)

    def test_invalid_values_and_non_p2p_are_rejected_without_work(self, store):
        host = _Host(store, _FakeBridge({}))
        assert host._card_action_pod_decide(_ctx(store, {"review_id": "RV1; rm", "decision": "approved"}))["toast"]["type"] == "error"
        assert host._card_action_pod_decide(_ctx(store, {"review_id": "RV0001", "decision": "--by=x"}))["toast"]["type"] == "error"
        group = host._card_action_pod_dashboard(_ctx(store, {}, chat_id="group-1"))
        assert group["toast"]["type"] == "warning" and "单聊" in group["toast"]["content"]
        assert host._card_action_pod_dashboard(_ctx(store, {}, message_id=None))["toast"]["type"] == "error"
        assert host.scheduled == []

    def test_engine_failure_patches_error_card(self, store):
        host = _Host(store, _FakeBridge({("ui", "dashboard"): PodctlError("podctl ui 超时（30 秒）")}))
        host._card_action_pod_dashboard(_ctx(store, {}))
        host.drain()
        card = host.patched[0][1]
        assert card["header"]["template"] == "red" and "超时" in json.dumps(card, ensure_ascii=False)

    def test_tick_pause_resume_daily_call_engine(self, store):
        bridge = _FakeBridge(
            {
                ("tick",): {"decision": "stop", "reason": "试运行窗口未开"},
                ("pause",): {"paused": True},
                ("resume",): {"paused": False},
                ("ui", "dashboard"): DASHBOARD,
                ("ui", "daily"): {"title": "POD 日报｜2026-10-10", "lines": ["今日没有模型调用"]},
            }
        )
        host = _Host(store, bridge)
        for action in ("tick", "pause", "resume", "daily"):
            getattr(host, f"_card_action_pod_{action}")(_ctx(store, {}))
        host.drain()
        assert ("pause", "--reason", "飞书工作台暂停") in bridge.calls and ("resume", "--by", "feishu:ou_x") in bridge.calls
        texts = [json.dumps(card, ensure_ascii=False) for _, card in host.patched]
        assert "试运行窗口未开" in texts[0] and "已暂停" in texts[1] and "已恢复" in texts[2] and "POD 日报" in texts[3]


def _texts(host: _Host) -> list[str]:
    return [json.dumps(card, ensure_ascii=False) for _, card in host.patched]


class TestPodBackgroundActions:
    def test_plan_and_worker_run_detached_with_notify(self, store):
        bridge = _FakeBridge({("plan",): {"detached": True, "task_id": "BG0003"}, ("worker",): {"detached": True, "task_id": "BG0004"}, ("ui", "dashboard"): DASHBOARD})
        host = _Host(store, bridge)
        payload = host._card_action_pod_plan(_ctx(store, {"action": "pod_plan", "family_id": "F01"}))
        assert payload["toast"]["content"] == "正在提交 F01 策划…"
        host._card_action_pod_worker(_ctx(store, {"action": "pod_worker"}))
        host.drain()
        assert ("plan", "--family", "F01", "--detach", "--notify") in bridge.calls
        assert ("worker", "--detach", "--source", "feishu", "--notify") in bridge.calls
        texts = _texts(host)
        assert f"{BACKGROUND_NOTICE}（BG0003）" in texts[0] and f"{BACKGROUND_NOTICE}（BG0004）" in texts[1]

    def test_engine_refusal_explains_why_nothing_started(self, store):
        bridge = _FakeBridge({("plan",): PodctlError("StageError：F01 正在裂变（BG0003），完成后会通知"), ("ui", "dashboard"): DASHBOARD})
        host = _Host(store, bridge)
        host._card_action_pod_plan(_ctx(store, {"family_id": "F01"}))
        host.drain()
        assert bridge.calls[0] == ("plan", "--family", "F01", "--detach", "--notify") and "**没有开始**：StageError：F01 正在裂变" in _texts(host)[0]

    def test_old_cards_limit_is_ignored(self, store):
        """方向数由引擎按可变字段定：旧卡片按钮里的 limit（包括脏值）一律不传给 plan。"""
        bridge = _FakeBridge({("plan",): {"detached": True, "task_id": "BG0003"}, ("ui", "dashboard"): DASHBOARD})
        host = _Host(store, bridge)
        for limit in (8, "12", 999, True, "3 --x"):
            host._card_action_pod_plan(_ctx(store, {"family_id": "F01", "limit": limit}))
        host.drain()
        assert [c for c in bridge.calls if c[0] == "plan"] == [("plan", "--family", "F01", "--detach", "--notify")] * 5

    @pytest.mark.parametrize(
        "value",
        [
            {"family_id": "F01; rm -rf"},
            {"family_id": "--all"},
            {"family_id": 1},
            {},
        ],
    )
    def test_plan_rejects_bad_parameters(self, store, value):
        host = _Host(store, _FakeBridge({}))
        assert host._card_action_pod_plan(_ctx(store, value))["toast"]["type"] == "error"
        assert host.scheduled == []

    def test_trial_window_start_and_stop_record_who(self, store):
        bridge = _FakeBridge({("trial", "start"): {"active": True}, ("trial", "stop"): {"active": False}, ("ui", "dashboard"): DASHBOARD})
        host = _Host(store, bridge)
        host._card_action_pod_trial_start(_ctx(store, {}))
        host._card_action_pod_trial_stop(_ctx(store, {}))
        host.drain()
        assert bridge.calls[0] == ("trial", "start", "--hours", "24", "--by", "feishu:ou_x")
        assert bridge.calls[2] == ("trial", "stop", "--by", "feishu:ou_x")
        texts = _texts(host)
        assert "已开启 24 小时连续运行" in texts[0] and "已停止连续运行" in texts[1]

    def test_approved_proposal_mentions_background_revision(self, store):
        decided = {"review_id": "RV0014", "effect": {"revise_started": "BG0005", "family_id": "F01"}}
        host = _Host(store, _FakeBridge({("review", "decide"): decided, ("ui", "reviews"): _page([])}))
        host._card_action_pod_decide(_ctx(store, {"review_id": "RV0014", "decision": "approved", "type": "proposal"}))
        host.drain()
        assert "已在后台按提案修订系列 F01" in _texts(host)[0]


def _direction_item(variant_id: str, index: int, title: str, *, editable: bool, picked: bool) -> dict:
    return {
        "variant_id": variant_id,
        "index": index,
        "title": title,
        "tag": "组合 · 改 3 处",
        "lines": ["主体：狐狸、兔子、刺猬 → 北极熊、北极狐、企鹅", "陪衬：草丘 → 冰雪台", "配色：暖白底 → 冷灰底"],
        "keep": "三只陪伴、水彩画法",
        "picked": picked,
        "state": "" if editable else ("已选（待推进）" if picked else "没选"),
        "checker": {"name": f"pick_{variant_id}", "checked": picked} if editable else None,
    }


def _directions_view(*, status: str = "proposed", picked: bool = False, thumb: str | None = None) -> dict:
    editable = status == "proposed"
    items = [
        _direction_item("V0012", 1, "极地冷色三伙伴", editable=editable, picked=picked),
        _direction_item("V0014", 2, "海洋三伙伴", editable=editable, picked=False),
    ]
    return {
        "title": "挑选裂变方向｜B003",
        "batch_id": "B003",
        "family_id": "F02",
        "status": status,
        "editable": editable,
        "lines": ["参考图 S02 · 系列 F02 v1（待认可：确认方向时一并认可）", "识别出的主体：长颈鹿、小象、小熊（认错了就进入 POD 对话说）"],
        "status_line": "已选 1 / 8 个方向" if picked else "已选 0 / 8 个方向",
        "images": [{"path": thumb, "label": "参考图 S02"}] if thumb else [],
        "items": items,
        "picked": 1 if picked else 0,
        "total": 8,
        "form": {"name": "pick_B003", "submit": {"action": "pod_plan_confirm", "label": "按勾选的方向出图", "style": "primary", "batch": "B003"}} if editable else None,
        "pick_actions": [
            {"action": "pod_plan_pick_all", "label": "全选", "style": "default", "batch": "B003", "on": 1},
            {"action": "pod_plan_pick_all", "label": "全不选", "style": "default", "batch": "B003", "on": 0},
        ]
        if editable
        else [],
        "actions": ([{"action": "pod_plan_reject", "label": "都不满意，换一批", "style": "default", "batch": "B003"}] if editable else []) + [{"action": "pod_history", "label": "方向记录", "style": "default", "family": "F02"}],
        "hint": "勾选后点「按勾选的方向出图」才会提交。" if editable else None,
    }


class TestPodDirections:
    def test_card_lists_directions_as_one_checker_form(self, tmp_path):
        thumb = tmp_path / "seed.jpg"
        thumb.write_bytes(b"jpg")
        card = build_pod_directions_card(_directions_view(thumb=str(thumb), picked=True), {str(thumb): "key-seed"}, notice="已确认 1 个方向")
        text = json.dumps(card, ensure_ascii=False)
        assert card["header"]["title"]["content"] == "挑选裂变方向｜B003" and card["header"]["template"] == "blue"
        assert "key-seed" in text and "已确认 1 个方向" in text
        # 卡片 JSON 1.0 的表单容器只能放在根节点：每个方向一个勾选框，之间分割线，最后是提交按钮
        forms = [e for e in card["elements"] if e["tag"] == "form"]
        assert len(forms) == 1 and forms[0]["name"] == "pick_B003" and forms[0]["fallback"]["tag"] == "fallback_text"
        first, rule, second, submit = forms[0]["elements"]
        assert rule == {"tag": "hr"}
        assert [(c["tag"], c["name"], c["checked"]) for c in (first, second)] == [("checker", "pick_V0012", True), ("checker", "pick_V0014", False)]
        assert first["text"] == {
            "tag": "lark_md",
            "content": "**1. 极地冷色三伙伴**　组合 · 改 3 处\n· 主体：狐狸、兔子、刺猬 → 北极熊、北极狐、企鹅\n· 陪衬：草丘 → 冰雪台\n· 配色：暖白底 → 冷灰底\n不变：三只陪伴、水彩画法",
        }
        # 勾选不配回调（只在本地生效），勾上也不划线、不变淡
        assert first["overall_checkable"] is True and "behaviors" not in first
        assert first["checked_style"] == {"show_strikethrough": False, "opacity": 1}
        assert (submit["action_type"], submit["name"], submit["text"]["content"], submit["type"]) == ("form_submit", "pick_B003_submit", "按勾选的方向出图", "primary")
        assert submit["value"] == {"action": "pod_plan_confirm", "batch": "B003"}
        # 全选 / 全不选在表单上面，换一批 / 方向记录 / 返回工作台在表单下面：回调按钮都在表单外面
        outside = [b["value"] for e in card["elements"] if e["tag"] == "action" for b in e["actions"]]
        assert outside == [
            {"action": "pod_plan_pick_all", "batch": "B003", "on": 1},
            {"action": "pod_plan_pick_all", "batch": "B003", "on": 0},
            {"action": "pod_plan_reject", "batch": "B003"},
            {"action": "pod_history", "family": "F02"},
            {"action": "pod_dashboard"},
        ]
        tags = [e["tag"] for e in card["elements"]]
        pick_row = next(i for i, e in enumerate(card["elements"]) if e["tag"] == "action")
        assert pick_row < tags.index("form") and [b["text"]["content"] for b in card["elements"][pick_row]["actions"]] == ["全选", "全不选"]

        confirmed = build_pod_directions_card(_directions_view(status="planned", picked=True), {})
        confirmed_text = json.dumps(confirmed, ensure_ascii=False)
        # 确认过的批次没有勾选框，也没有全选 / 全不选
        assert confirmed["header"]["template"] == "green" and _labels(confirmed) == ["方向记录", "返回工作台"]
        assert not [e for e in confirmed["elements"] if e["tag"] in ("form", "checker")]
        assert "**1. 极地冷色三伙伴**　组合 · 改 3 处　已选（待推进）" in confirmed_text and "**2. 海洋三伙伴**　组合 · 改 3 处　没选" in confirmed_text
        assert build_pod_directions_card(_directions_view(status="superseded"), {})["header"]["template"] == "grey"

    def test_sections_head_small_and_big_changes_and_card_stays_small(self):
        """引擎给了分段（小变化 / 大变化）：每段先一行段标题，段内不加分割线；一轮 40 个方向的卡片也不超过飞书上限。"""
        small = "小变化（30）：只改一处，其余照原图，每个出 6 张图（3 个模型 × 图生图 / 图生图 Edit）"
        big = "大变化（10）：每个出 9 张图（3 个模型 × 文生图 / 图生图 / 图生图 Edit）"
        items = []
        for i in range(1, 41):
            item = _direction_item(f"V{i:04d}", i, f"方向标题{i}改到十二字以内", editable=True, picked=False)
            item.update(
                section=small if i <= 30 else big,
                tag="配色结构" if i <= 30 else "主题叙事",
                lines=["底色：暖白奶油色的底 → 雾蓝色带一点灰"] if i <= 30 else ["主题：森林动物伙伴 → 海洋动物伙伴"],
                keep="" if i <= 30 else "三只陪伴的构图与水彩画法",
            )
            items.append(item)
        view = {**_directions_view(), "items": items, "total": 40, "status_line": "已选 0 / 40 个方向"}
        card = build_pod_directions_card(view, {})
        form = next(e for e in card["elements"] if e["tag"] == "form")
        tags = [e["tag"] for e in form["elements"]]
        assert tags == ["markdown", *["checker"] * 30, "hr", "markdown", *["checker"] * 10, "button"]
        assert form["elements"][0]["content"] == f"**{small}**" and form["elements"][32]["content"] == f"**{big}**"
        assert form["elements"][1]["text"]["content"] == "**1. 方向标题1改到十二字以内**　配色结构\n· 底色：暖白奶油色的底 → 雾蓝色带一点灰"
        assert len(json.dumps(card, ensure_ascii=False).encode("utf-8")) < 28 * 1024

    def test_review_item_links_open_the_picker(self):
        item = _item("RV0020", [], actions=[])
        item["links"] = [{"action": "pod_plan_view", "label": "去挑选", "style": "primary", "batch": "B003"}]
        card = build_pod_reviews_card(_page([item]), {})
        button = next(b for b in _buttons(card) if b["text"]["content"] == "去挑选")
        assert button["value"] == {"action": "pod_plan_view", "batch": "B003"} and button["type"] == "primary"

    def test_view_and_pick_call_engine_and_patch_the_picker(self, store):
        bridge = _FakeBridge({("ui", "directions"): _directions_view(), ("directions", "pick"): {"picked": ["V0012"]}})
        host = _Host(store, bridge)
        assert host._card_action_pod_plan_view(_ctx(store, {"batch": "B003"}))["toast"]["content"] == "正在打开挑选卡…"
        host._card_action_pod_plan_pick(_ctx(store, {"batch": "B003", "variant": "V0012", "on": 1}))
        host._card_action_pod_plan_pick(_ctx(store, {"batch": "B003", "variant": "V0012", "on": 0}))
        host.drain()
        assert bridge.calls == [
            ("ui", "directions", "--batch", "B003"),
            ("directions", "pick", "B003", "V0012"),
            ("ui", "directions", "--batch", "B003"),
            ("directions", "pick", "B003", "V0012", "--off"),
            ("ui", "directions", "--batch", "B003"),
        ]
        assert all(card["header"]["title"]["content"] == "挑选裂变方向｜B003" for _, card in host.patched)

        bridge.responses[("directions", "pick")] = PodctlError("StageError：B003 确认过，不能再挑")
        host._card_action_pod_plan_pick(_ctx(store, {"batch": "B003", "variant": "V0012", "on": "1"}))
        host.drain()
        assert "**没有改成**：StageError：B003 确认过" in _texts(host)[-1]

    def test_pick_all_and_none_call_engine_and_patch_the_picker(self, store):
        bridge = _FakeBridge({("ui", "directions"): _directions_view(picked=True), ("directions", "pick"): {"picked": ["V0012", "V0014"]}})
        host = _Host(store, bridge)
        assert host._card_action_pod_plan_pick_all(_ctx(store, {"batch": "B003", "on": 1}))["toast"]["content"] == "正在全选…"
        host.drain()
        bridge.responses[("directions", "pick")] = {"picked": []}
        assert host._card_action_pod_plan_pick_all(_ctx(store, {"batch": "B003", "on": "0"}))["toast"]["content"] == "正在全部取消…"
        host.drain()
        assert bridge.calls == [
            ("directions", "pick", "B003", "all"),
            ("ui", "directions", "--batch", "B003"),
            ("directions", "pick", "B003", "all", "--off"),
            ("ui", "directions", "--batch", "B003"),
        ]
        texts = _texts(host)
        assert "已全选 2 个方向：可以再取消几个，最后点「按勾选的方向出图」" in texts[-2] and "已全部取消勾选" in texts[-1]
        assert all(card["header"]["title"]["content"] == "挑选裂变方向｜B003" for _, card in host.patched)

        bridge.responses[("directions", "pick")] = PodctlError("StageError：B003 当前状态 planned，不能再挑")
        host._card_action_pod_plan_pick_all(_ctx(store, {"batch": "B003", "on": 1}))
        host.drain()
        assert "**没有改成**：StageError：B003 当前状态 planned" in _texts(host)[-1]

    @pytest.mark.parametrize(
        "value",
        [{"batch": "B003; rm", "on": 1}, {"batch": "--all", "on": 1}, {"batch": "B003", "on": True}, {"batch": "B003", "on": 2}, {"batch": "B003"}],
    )
    def test_pick_all_rejects_bad_parameters(self, store, value):
        host = _Host(store, _FakeBridge({}))
        assert host._card_action_pod_plan_pick_all(_ctx(store, value))["toast"]["type"] == "error"
        assert host.scheduled == []

    @pytest.mark.parametrize(
        "value",
        [
            {"batch": "B003; rm", "variant": "V0012", "on": 1},
            {"batch": "B003", "variant": "--all", "on": 1},
            {"batch": "B003", "variant": "V0012", "on": True},
            {"batch": "B003", "variant": "V0012", "on": 2},
            {"batch": "B003", "variant": "V0012"},
        ],
    )
    def test_pick_rejects_bad_parameters(self, store, value):
        host = _Host(store, _FakeBridge({}))
        assert host._card_action_pod_plan_pick(_ctx(store, value))["toast"]["type"] == "error"
        for action in ("view", "confirm", "reject"):
            assert getattr(host, f"_card_action_pod_plan_{action}")(_ctx(store, {"batch": "--x"}))["toast"]["type"] == "error"
        assert host.scheduled == []

    def test_confirm_starts_generation_and_explains_result(self, store):
        confirmed = {"batch_id": "B003", "family_id": "F02", "family_approved": True, "picked": ["V0012", "V0014"], "jobs": ["J0007", "J0008"], "directions_started": "BG0031"}
        bridge = _FakeBridge({("directions", "confirm"): confirmed, ("ui", "directions"): _directions_view(status="planned")})
        host = _Host(store, bridge)
        host._card_action_pod_plan_confirm(_ctx(store, {"batch": "B003"}))
        host.drain()
        assert bridge.calls[0] == ("directions", "confirm", "B003", "--by", "feishu:ou_x", "--start")
        text = _texts(host)[-1]
        assert "已确认 2 个方向，后台开始出图" in text and "（BG0031）" in text and "系列 F02 已一并认可" in text

        bridge.responses[("directions", "confirm")] = {"picked": ["V0012"], "directions_queued": ["J0009"]}
        host._card_action_pod_plan_confirm(_ctx(store, {"batch": "B003"}))
        host.drain()
        assert "已确认 1 个方向：已排进正在出图的队列，有空的工位就接着出" in _texts(host)[-1]

        bridge.responses[("directions", "confirm")] = PodctlError("StageError：还没选方向")
        host._card_action_pod_plan_confirm(_ctx(store, {"batch": "B003"}))
        host.drain()
        assert "**没有开始**：StageError：还没选方向" in _texts(host)[-1]

    def test_checker_form_submits_all_picks_at_once(self, store):
        bridge = _FakeBridge({("directions", "confirm"): {"picked": ["V0012", "V0014"], "directions_started": "BG0033"}, ("ui", "directions"): _directions_view(status="planned")})
        host = _Host(store, bridge)
        form_value = {"pick_V0014": True, "pick_V0012": "true", "pick_V0013": False, "pick_V0015": "false", "pick_x; rm": True, "hint_F02": "别的字段"}
        assert host._card_action_pod_plan_confirm(_ctx(store, {"batch": "B003"}, form_value=form_value))["toast"]["content"] == "正在确认 B003…"
        host.drain()
        assert bridge.calls[0] == ("directions", "confirm", "B003", "--by", "feishu:ou_x", "--start", "--pick", "V0012,V0014")
        assert "已确认 2 个方向，后台开始出图" in _texts(host)[-1]

        # 一个都没勾：只提示，不调引擎；不在单聊时先挡掉
        payload = host._card_action_pod_plan_confirm(_ctx(store, {"batch": "B003"}, form_value={"pick_V0012": False}))
        assert payload["toast"] == {"type": "warning", "content": "先勾选要出图的方向"}
        group = host._card_action_pod_plan_confirm(_ctx(store, {"batch": "B003"}, chat_id="group-1", form_value={}))
        assert "单聊" in group["toast"]["content"]
        assert host.scheduled == [] and len(bridge.calls) == 2

    def test_reject_replans_in_background_and_returns_to_dashboard(self, store):
        bridge = _FakeBridge({("directions", "reject"): {"rejected": 8, "replan_started": "BG0032"}, ("ui", "dashboard"): DASHBOARD})
        host = _Host(store, bridge)
        host._card_action_pod_plan_reject(_ctx(store, {"batch": "B003"}))
        host.drain()
        assert bridge.calls[0] == ("directions", "reject", "B003", "--by", "feishu:ou_x", "--replan", "--notify")
        assert "已换一批：后台正在重新策划，完成后会通知你（BG0032）" in _texts(host)[-1]
        assert host.patched[-1][1]["header"]["title"]["content"] == "POD 毛毯工作台"

    def test_every_pod_action_has_a_handler(self):
        for action in pod_cards.POD_CARD_ACTIONS:
            assert callable(getattr(PodCardsMixin, f"_card_action_{action}", None)), action


_PLAN_INPUT = {"name": "hint_F01", "placeholder": "这次想要的方向（可选），如：换成海洋动物、冬季主题", "max_length": 100}


def _history_view(*, can_plan: bool = True) -> dict:
    return {
        "title": "任务明细｜S01 · F01",
        "kind": "family",
        "family_id": "F01",
        "lines": [
            "参考图 S01 · 系列 F01 v3　出图中",
            "拆解 ✓ › 策划 ✓ › 挑选 ✓ › **出图 1/8** › 选款 待选 5 · 选中 1",
            "现在：B003「冰雪三伙伴」出图中 4/6",
            "做过 14 个方向：已出完 1、出图中 1、待出图 6、没选 6；选中的款 1 张",
        ],
        "images": [],
        "groups": [
            {
                "batch_id": "B003",
                "head": "**B003**　10-14 15:02 · 已选方向 · 选了 8 个，出完 1 个\n要求：换成海洋动物",
                "lines": ["1. 草坡岩台三伙伴（主题、主体、陪衬、配色…）— 已出完 6 张 · 待选 5 · 选中 1", "2. 冰雪三伙伴（主体、配色）— 出图中 4/6"],
            }
        ],
        "nav": [{"action": "pod_history", "label": "更早的批次", "style": "default", "family": "F01", "offset": 3}],
        "form": {
            "name": "plan_F01",
            "input": _PLAN_INPUT,
            "submit": {"action": "pod_plan", "label": "再策划 33 个方向", "style": "primary", "family_id": "F01", "back": "tasks"},
        }
        if can_plan
        else None,
        "actions": [] if can_plan else [{"action": "pod_plan_view", "label": "去挑选 B004", "style": "primary", "batch": "B004"}],
        "note": None if can_plan else "还有一批方向待挑选（B004）",
        "hint": "每个方向写着细到哪一步；做过的和没选的方向都记着，再策划时会避开它们，写了要求就围绕要求出新方向。",
    }


class TestPodHistory:
    def test_history_card_lists_batches_and_plan_form(self):
        card = build_pod_history_card(_history_view(), {})
        text = json.dumps(card, ensure_ascii=False)
        assert card["header"]["title"]["content"] == "任务明细｜S01 · F01"
        assert "做过 14 个方向" in text and "要求：换成海洋动物" in text and "— 已出完 6 张 · 待选 5 · 选中 1" in text
        assert "**出图 1/8**" in text and "— 出图中 4/6" in text
        forms = [e for e in card["elements"] if e["tag"] == "form"]
        assert len(forms) == 1 and forms[0]["name"] == "plan_F01" and forms[0]["fallback"]["tag"] == "fallback_text"
        field, submit = forms[0]["elements"]
        # 要求可以不填：不填就按系列规则策划
        assert (field["tag"], field["name"], field["required"], field["max_length"]) == ("input", "hint_F01", False, 100)
        assert field["placeholder"]["content"].startswith("这次想要的方向（可选）")
        assert (submit["action_type"], submit["name"], submit["text"]["content"]) == ("form_submit", "plan_F01_submit", "再策划 33 个方向")
        assert submit["value"] == {"action": "pod_plan", "family_id": "F01", "back": "tasks"}
        outside = [b["value"] for e in card["elements"] if e["tag"] == "action" for b in e["actions"]]
        assert outside == [{"action": "pod_history", "family": "F01", "offset": 3}, {"action": "pod_tasks"}, {"action": "pod_dashboard"}]

        waiting = build_pod_history_card(_history_view(can_plan=False), {})
        assert not [e for e in waiting["elements"] if e["tag"] == "form"]
        assert "**现在不能再策划**：还有一批方向待挑选（B004）" in json.dumps(waiting, ensure_ascii=False)
        assert _labels(waiting) == ["更早的批次", "去挑选 B004", "任务列表", "返回工作台"]

        families = build_pod_history_card(
            {
                "title": "任务明细",
                "kind": "families",
                "lines": [],
                "families": [{"family_id": "F01", "line": "**F01**（参考图 S01）：做过 14 个方向", "action": {"action": "pod_history", "label": "看明细", "style": "default", "family": "F01"}}],
            },
            {},
        )
        assert [b["value"] for b in _buttons(families)] == [{"action": "pod_history", "family": "F01"}, {"action": "pod_tasks"}, {"action": "pod_dashboard"}]
        empty = build_pod_history_card({"title": "任务明细", "kind": "families", "lines": ["还没有参考图：进入 POD 对话发一张图开始"], "families": []}, {})
        assert "还没有参考图" in json.dumps(empty, ensure_ascii=False)

    def test_history_action_pages_and_validates(self, store, tmp_path):
        thumb = tmp_path / "S01.thumb.jpg"
        thumb.write_bytes(b"jpg")
        bridge = _FakeBridge({("ui", "history"): {**_history_view(), "images": [{"path": str(thumb), "label": "参考图 S01"}]}})
        host = _Host(store, bridge)
        assert host._card_action_pod_history(_ctx(store, {"action": "pod_history"}))["toast"]["content"] == "正在打开任务明细…"
        host._card_action_pod_history(_ctx(store, {"action": "pod_history", "family": "F01", "offset": 3}))
        host.drain()
        assert bridge.calls == [("ui", "history", "--offset", "0"), ("ui", "history", "--offset", "3", "--family", "F01")]
        assert "key-S01.thumb.jpg" in _texts(host)[-1]
        for bad in ("F01; rm", "--all", 1):
            assert host._card_action_pod_history(_ctx(store, {"family": bad}))["toast"]["type"] == "error"
        assert host.scheduled == []

    def test_plan_form_passes_the_hint(self, store):
        bridge = _FakeBridge({("plan",): {"detached": True, "task_id": "BG0040"}, ("ui", "dashboard"): DASHBOARD})
        host = _Host(store, bridge)
        value = {"action": "pod_plan", "family_id": "F01"}
        host._card_action_pod_plan(_ctx(store, value, form_value={"hint_F01": "  换成  海洋动物 "}))
        # 以「-」开头的要求也只是要求；别的系列的字段不认
        host._card_action_pod_plan(_ctx(store, value, form_value={"hint_F01": "-冬季", "hint_F02": "别的系列"}))
        host._card_action_pod_plan(_ctx(store, value, form_value={"hint_F01": "", "hint_F02": "别的系列"}))
        host.drain()
        assert [call for call in bridge.calls if call[0] == "plan"] == [
            ("plan", "--family", "F01", "--hint=换成 海洋动物", "--detach", "--notify"),
            ("plan", "--family", "F01", "--hint=-冬季", "--detach", "--notify"),
            ("plan", "--family", "F01", "--detach", "--notify"),
        ]
        assert f"{BACKGROUND_NOTICE}（BG0040）" in _texts(host)[0]
        # 超长：只提示，不调引擎
        payload = host._card_action_pod_plan(_ctx(store, value, form_value={"hint_F01": "海" * 101}))
        assert payload["toast"]["type"] == "warning" and host.scheduled == []


def _tasks_view(*, thumb: str | None = None, paused: bool = False) -> dict:
    return {
        "title": "POD 任务列表",
        "total": 3,
        "offset": 0,
        "limit": 2,
        "lines": ["共 3 张参考图：待你处理 0 · 进行中 1 · 已完成 1 · 其他 1｜选款待审 12"] + (["**流水线已暂停**：恢复后才会接着出图"] if paused else []),
        "actions": [{"action": "pod_resume", "label": "恢复", "style": "primary"}] if paused else [],
        "items": [
            {
                "key": "F01",
                "seed_id": "S01",
                "family_id": "F01",
                "status": "generating",
                "title": "S01 · F01 v3　出图中",
                "lines": ["拆解 ✓ › 策划 ✓ › 挑选 ✓ › **出图 6/8** › 选款 待选 12", "现在：B003「冰雪三伙伴」出图中 4/6", "3 批 · 14 个方向 · 出图 41 张"],
                "images": [{"path": thumb, "label": "参考图 S01"}] if thumb else [],
                "actions": [
                    {"action": "pod_reviews", "label": "去选款（12）", "style": "default", "type": "asset_selection"},
                    {"action": "pod_history", "label": "看明细", "style": "default", "family": "F01"},
                ],
                "form": None,
                "note": None,
            },
            {
                "key": "F02",
                "seed_id": "S02",
                "family_id": "F02",
                "status": "done",
                "title": "S02 · F02 v1　已完成（10-14 18:20 出完）",
                "lines": ["拆解 ✓ › 策划 ✓ › 挑选 ✓ › 出图 8/8 ✓ › 选款 选中 3 ✓", "现在：选中的 8 个方向都出完了，共 46 张"],
                "images": [],
                "actions": [{"action": "pod_history", "label": "看明细", "style": "default", "family": "F02"}],
                "form": {
                    "name": "plan_F02",
                    "input": {**_PLAN_INPUT, "name": "hint_F02"},
                    "submit": {"action": "pod_plan", "label": "继续裂变（再策划 33 个方向）", "style": "primary", "family_id": "F02", "back": "tasks"},
                },
                "note": None,
            },
        ],
        "nav": [{"action": "pod_tasks", "label": "下一页", "style": "default", "offset": 2}],
        "hint": "一张参考图一个任务。",
    }


class TestPodTasks:
    def test_tasks_card_lists_each_reference_image(self):
        card = build_pod_tasks_card(_tasks_view(), {"/t/S01.jpg": "key-S01"})
        assert card["header"]["title"]["content"] == "POD 任务列表（3）" and card["config"]["update_multi"] is True
        text = json.dumps(card, ensure_ascii=False)
        assert "**S01 · F01 v3　出图中**" in text and "**出图 6/8**" in text and "现在：B003「冰雪三伙伴」出图中 4/6" in text
        # 已完成的任务带「继续裂变」表单：要求可以不填，提交后回到任务列表
        forms = [e for e in card["elements"] if e["tag"] == "form"]
        assert [f["name"] for f in forms] == ["plan_F02"]
        field, submit = forms[0]["elements"]
        assert (field["name"], field["required"]) == ("hint_F02", False)
        assert (submit["action_type"], submit["text"]["content"]) == ("form_submit", "继续裂变（再策划 33 个方向）")
        assert submit["value"] == {"action": "pod_plan", "family_id": "F02", "back": "tasks"}
        assert _labels(card) == ["去选款（12）", "看明细", "继续裂变（再策划 33 个方向）", "看明细", "下一页", "刷新", "返回工作台"]
        assert next(b for b in _buttons(card) if b["text"]["content"] == "刷新")["value"] == {"action": "pod_tasks", "offset": 0}
        assert card["elements"][-1]["elements"][0]["content"] == "第 1–2 个，共 3 个。一张参考图一个任务。"

        # 有缩略图：图在左、进度在右；暂停时顶部有「恢复」
        view = _tasks_view(thumb="/t/S01.jpg", paused=True)
        card = build_pod_tasks_card(view, {"/t/S01.jpg": "key-S01"}, notice="已在后台开始")
        assert card["elements"][0]["content"] == "已在后台开始" and "流水线已暂停" in card["elements"][1]["content"]
        assert _labels(card)[0] == "恢复"
        columns = next(e for e in card["elements"] if e["tag"] == "column_set")["columns"]
        assert columns[0]["elements"][0]["img_key"] == "key-S01" and "S01 · F01 v3" in columns[1]["elements"][0]["content"]

        # 不能再策划的任务写原因，不给表单
        blocked = _tasks_view()
        blocked["items"][1] = {**blocked["items"][1], "form": None, "note": "系列 F02 已耗尽：给新参考图，或在 POD 对话里修订系列、扩大变化域"}
        card = build_pod_tasks_card(blocked, {})
        assert not [e for e in card["elements"] if e["tag"] == "form"]
        assert "**现在不能再策划**：系列 F02 已耗尽" in json.dumps(card, ensure_ascii=False)
        empty = build_pod_tasks_card(
            {"title": "POD 任务列表", "total": 0, "offset": 0, "limit": 4, "lines": ["还没有参考图"], "actions": [{"action": "pod_enter_chat", "label": "进入 POD 对话", "style": "primary"}], "items": [], "nav": []}, {}
        )
        assert _labels(empty) == ["进入 POD 对话", "刷新", "返回工作台"]

    def test_tasks_action_loads_pages_with_thumbnails(self, store, tmp_path):
        thumb = tmp_path / "seed-S01.jpg"
        thumb.write_bytes(b"jpg")
        bridge = _FakeBridge({("ui", "tasks"): _tasks_view(thumb=str(thumb))})
        host = _Host(store, bridge)
        payload = host._card_action_pod_tasks(_ctx(store, {"action": "pod_tasks", "offset": 4}))
        assert payload["toast"]["content"] == "正在加载任务列表…" and not host.patched
        host._card_action_pod_tasks(_ctx(store, {"action": "pod_tasks", "offset": "--all"}))
        host.drain()
        assert bridge.calls == [("ui", "tasks", "--offset", "4", "--limit", "4"), ("ui", "tasks", "--offset", "0", "--limit", "4")]
        assert "key-seed-S01.jpg" in _texts(host)[0] and host.uploads == [str(thumb)]
        group = host._card_action_pod_tasks(_ctx(store, {}, chat_id="group-1"))
        assert group["toast"]["type"] == "warning"

    def test_continue_from_task_list_comes_back_to_it(self, store):
        bridge = _FakeBridge({("plan",): {"detached": True, "task_id": "BG0050"}, ("ui", "tasks"): _tasks_view(), ("ui", "dashboard"): DASHBOARD})
        host = _Host(store, bridge)
        value = {"action": "pod_plan", "family_id": "F02", "back": "tasks"}
        host._card_action_pod_plan(_ctx(store, value, form_value={"hint_F02": "冬季主题"}))
        # back 只认 tasks，别的值照旧回工作台
        host._card_action_pod_plan(_ctx(store, {**value, "back": "reviews"}))
        host.drain()
        assert [call for call in bridge.calls if call[0] == "plan"] == [
            ("plan", "--family", "F02", "--hint=冬季主题", "--detach", "--notify"),
            ("plan", "--family", "F02", "--detach", "--notify"),
        ]
        first, second = (card for _, card in host.patched)
        assert first["header"]["title"]["content"] == "POD 任务列表（3）"
        assert f"{BACKGROUND_NOTICE}（BG0050）" in json.dumps(first, ensure_ascii=False)
        assert second["header"]["title"]["content"] == "POD 毛毯工作台"


class TestPodCandidateTable:
    def test_task_with_candidate_table_has_links_before_its_buttons(self):
        view = _tasks_view()
        first = view["items"][0]
        view["items"][0] = {
            **first,
            "links": [
                {"label": "候选表", "url": "https://example.feishu.cn/base/bascn1?table=tbl9"},
                {"label": "坏链接", "url": "javascript:alert(1)"},
            ],
            "actions": [{"action": "pod_accept", "label": "选款完成（待选 12）", "style": "primary", "seed": "S01"}, *first["actions"]],
        }
        card = build_pod_tasks_card(view, {})
        labels = _labels(card)
        assert labels[:4] == ["候选表", "选款完成（待选 12）", "去选款（12）", "看明细"]
        assert "坏链接" not in labels
        buttons = {b["text"]["content"]: b for b in _buttons(card)}
        assert buttons["候选表"]["url"].endswith("?table=tbl9") and "value" not in buttons["候选表"]
        assert buttons["选款完成（待选 12）"]["value"] == {"action": "pod_accept", "seed": "S01"}

    def test_accept_runs_in_background_and_comes_back_to_tasks(self, store):
        bridge = _FakeBridge({("table", "accept"): {"detached": True, "task_id": "BG0077"}, ("ui", "tasks"): _tasks_view()})
        host = _Host(store, bridge)
        payload = host._card_action_pod_accept(_ctx(store, {"action": "pod_accept", "seed": "S01"}))
        assert payload["toast"]["content"] == "正在提交 S01 选款验收…"
        host.drain()
        assert ("table", "accept", "--seed", "S01", "--by", "feishu:ou_x", "--detach", "--notify") in bridge.calls
        (card,) = [c for _, c in host.patched]
        assert card["header"]["title"]["content"] == "POD 任务列表（3）"
        assert f"{BACKGROUND_NOTICE}（BG0077）" in json.dumps(card, ensure_ascii=False)

    @pytest.mark.parametrize("value", [{"seed": "S01; rm -rf"}, {"seed": "--all"}, {"seed": "F01"}, {"seed": 1}, {}])
    def test_accept_rejects_bad_task_ids(self, store, value):
        host = _Host(store, _FakeBridge({}))
        assert host._card_action_pod_accept(_ctx(store, value))["toast"]["type"] == "error"
        assert host.scheduled == []


class TestPodScenes:
    def test_scenes_button_queues_and_comes_back_to_tasks(self, store):
        started = {"queued": [{"set_id": "SS0001", "asset_id": "IMG00012", "seed_id": "S01"}], "skipped": [], "sync_task": "BG0090"}
        bridge = _FakeBridge({("scenes", "start"): started, ("ui", "tasks"): _tasks_view()})
        host = _Host(store, bridge)
        payload = host._card_action_pod_scenes(_ctx(store, {"action": "pod_scenes", "seed": "S01"}))
        assert payload["toast"]["content"] == "正在提交 S01 出套图…"
        host.drain()
        assert ("scenes", "start", "--seed", "S01", "--by", "feishu:ou_x") in bridge.calls
        (card,) = [c for _, c in host.patched]
        assert card["header"]["title"]["content"] == "POD 任务列表（3）"
        assert "已把 1 款交给 mooya 出套图，出完会通知你（BG0090）" in json.dumps(card, ensure_ascii=False)

    def test_scenes_button_explains_when_nothing_new(self, store):
        skipped = {"queued": [], "skipped": [{"asset_id": "IMG00012", "reason": "SS0001 出图中"}], "sync_task": None}
        bridge = _FakeBridge({("scenes", "start"): skipped, ("ui", "tasks"): _tasks_view()})
        host = _Host(store, bridge)
        host._card_action_pod_scenes(_ctx(store, {"action": "pod_scenes", "seed": "S01"}))
        host.drain()
        (card,) = [c for _, c in host.patched]
        assert "没有新的款要出套图：SS0001 出图中" in json.dumps(card, ensure_ascii=False)

    @pytest.mark.parametrize("value", [{"seed": "S01; rm -rf"}, {"seed": "--force"}, {"seed": "IMG00012"}, {"seed": 1}, {}])
    def test_scenes_rejects_bad_task_ids(self, store, value):
        host = _Host(store, _FakeBridge({}))
        assert host._card_action_pod_scenes(_ctx(store, value))["toast"]["type"] == "error"
        assert host.scheduled == []

    def test_scenes_button_is_a_registered_card_action(self):
        assert "pod_scenes" in pod_cards.POD_CARD_ACTIONS


class TestPodDispatch:
    def test_dispatch_runs_in_background_and_comes_back_to_tasks(self, store):
        bridge = _FakeBridge({("table", "dispatch"): {"detached": True, "task_id": "BG0078"}, ("ui", "tasks"): _tasks_view()})
        host = _Host(store, bridge)
        payload = host._card_action_pod_dispatch(_ctx(store, {"action": "pod_dispatch", "seed": "S01"}))
        assert payload["toast"]["content"] == "正在提交 S01 先出套图…"
        host.drain()
        assert ("table", "dispatch", "--seed", "S01", "--by", "feishu:ou_x", "--detach", "--notify") in bridge.calls
        (card,) = [c for _, c in host.patched]
        assert card["header"]["title"]["content"] == "POD 任务列表（3）"
        assert f"{BACKGROUND_NOTICE}（BG0078）" in json.dumps(card, ensure_ascii=False)

    def test_accept_refused_while_drawing_says_why(self, store):
        refused = PodctlError("S01 还有 1 个方向 4 张图没出完：先用「选中的先出套图」，全部出完再点「选款完成」")
        bridge = _FakeBridge({("table", "accept"): refused, ("ui", "tasks"): _tasks_view()})
        host = _Host(store, bridge)
        host._card_action_pod_accept(_ctx(store, {"action": "pod_accept", "seed": "S01"}))
        host.drain()
        (card,) = [c for _, c in host.patched]
        text = json.dumps(card, ensure_ascii=False)
        assert "**没有开始**：S01 还有 1 个方向 4 张图没出完" in text

    @pytest.mark.parametrize("value", [{"seed": "S01; rm -rf"}, {"seed": "--all"}, {"seed": "F01"}, {"seed": 1}, {}])
    def test_dispatch_rejects_bad_task_ids(self, store, value):
        host = _Host(store, _FakeBridge({}))
        assert host._card_action_pod_dispatch(_ctx(store, value))["toast"]["type"] == "error"
        assert host.scheduled == []

    def test_dispatch_is_a_registered_card_action(self):
        assert "pod_dispatch" in pod_cards.POD_CARD_ACTIONS


class TestPodDownloaded:
    def test_mark_downloaded_and_archive_comes_back_to_tasks(self, store):
        marked = {"seed_id": "S01", "batch_id": "B008", "label": "S01 森林三伙伴 · B008", "items": 3, "marked_at": "2026-10-26T07:30:00+00:00", "archived": [{"seed_id": "S01", "label": "S01 森林三伙伴"}]}
        bridge = _FakeBridge({("scenes", "downloaded"): marked, ("ui", "tasks"): _tasks_view()})
        host = _Host(store, bridge)
        payload = host._card_action_pod_downloaded(_ctx(store, {"action": "pod_downloaded", "seed": "S01", "batch": "B008"}))
        assert payload["toast"]["content"] == "正在标 B008 已下载…"
        host.drain()
        assert ("scenes", "downloaded", "--seed", "S01", "--batch", "B008", "--by", "feishu:ou_x", "--source", "card") in bridge.calls
        (card,) = [c for _, c in host.patched]
        assert card["header"]["title"]["content"] == "POD 任务列表（3）"
        text = json.dumps(card, ensure_ascii=False)
        assert "已标 **S01 森林三伙伴 · B008** 交付包已下载；任务的套图都出完、交付包都下载了，已归档到「历史任务」" in text

    def test_mark_from_shelf_comes_back_to_shelf_and_says_what_is_left(self, store):
        marked = {"seed_id": "S02", "batch_id": "B004", "label": "S02 · B004", "already": True, "shelf_block": "交付包还没标已下载：B006"}
        bridge = _FakeBridge({("scenes", "downloaded"): marked, ("ui", "shelf"): _shelf_view()})
        host = _Host(store, bridge)
        host._card_action_pod_downloaded(_ctx(store, {"action": "pod_downloaded", "seed": "S02", "batch": "B004", "back": "shelf"}))
        host.drain()
        (card,) = [c for _, c in host.patched]
        assert card["header"]["title"]["content"] == "历史任务（5）"
        text = json.dumps(card, ensure_ascii=False)
        assert "**S02 · B004** 的交付包之前就标过已下载；还没归档：交付包还没标已下载：B006" in text

    def test_refused_mark_says_why(self, store):
        refused = PodctlError("S01 · B008 的交付包还没打好：出完套图打好包再标")
        bridge = _FakeBridge({("scenes", "downloaded"): refused, ("ui", "tasks"): _tasks_view()})
        host = _Host(store, bridge)
        host._card_action_pod_downloaded(_ctx(store, {"action": "pod_downloaded", "seed": "S01", "batch": "B008"}))
        host.drain()
        (card,) = [c for _, c in host.patched]
        assert "**S01 · B008 没有标上**：S01 · B008 的交付包还没打好" in json.dumps(card, ensure_ascii=False)

    @pytest.mark.parametrize(
        "value",
        [{"seed": "S01", "batch": "B008; rm"}, {"seed": "S01", "batch": "--undo"}, {"seed": "S01"}, {"seed": "--all", "batch": "B008"}, {"batch": "B008"}, {}],
    )
    def test_rejects_bad_ids(self, store, value):
        host = _Host(store, _FakeBridge({}))
        assert host._card_action_pod_downloaded(_ctx(store, value))["toast"]["type"] == "error"
        assert host.scheduled == []

    def test_downloaded_is_a_registered_card_action(self):
        assert "pod_downloaded" in pod_cards.POD_CARD_ACTIONS


def _shelf_view(*, query: str | None = None) -> dict:
    item = {
        "key": "F02",
        "seed_id": "S02",
        "family_id": "F02",
        "status": "done",
        "title": "S02 森林三伙伴水彩 1020　已完成（10-24 出完）",
        "lines": ["**已归档**（10-25 自动归档）；候选表在「已归档」文件夹", "拆解 ✓ › 策划 ✓ › 挑选 ✓ › 出图 8/8 ✓ › 选款 选中 3 ✓ › 套图 3/3 ✓"],
        "images": [],
        "actions": [
            {"action": "pod_restore", "label": "调出", "style": "primary", "seed": "S02", "back": "shelf"},
            {"action": "pod_history", "label": "看明细", "style": "default", "family": "F02"},
        ],
        "links": [{"label": "交付包 B004", "url": "https://oss.example.com/b004.zip"}, {"label": "候选表", "url": "https://example.feishu.cn/base/bascn1?table=tbl9&view=vew1"}],
        "form": {
            "name": "plan_F02",
            "input": {**_PLAN_INPUT, "name": "hint_F02"},
            "submit": {"action": "pod_plan", "label": "继续裂变（再策划 33 个方向）", "style": "primary", "family_id": "F02", "back": "tasks"},
        },
        "note": None,
    }
    return {
        "title": "历史任务",
        "total": 5,
        "offset": 0,
        "limit": 4,
        "query": query,
        "lines": [f"搜「{query}」：1 个"] if query else ["已归档 5 个任务（新归档的在前）：调出后回到任务列表，继续裂变会自动调出"],
        "actions": [],
        "items": [item],
        "nav": [
            {"action": "pod_shelf", "label": "下一页", "style": "default", "offset": 4, **({"q": query} if query else {})},
            {"action": "pod_tasks", "label": "任务列表", "style": "default"},
        ],
        "search": {
            "name": "shelf_search",
            "input": {"name": "shelf_q", "placeholder": "搜名字、编号或日期，如：恐龙、S03、1020", "max_length": 20, "value": query},
            "submit": {"action": "pod_shelf", "label": "搜索", "style": "default"},
        },
        "hint": "历史任务是选款完成、套图出完后收起的任务。",
    }


class TestPodShelf:
    def test_shelf_card_has_search_restore_and_continue(self):
        card = pod_cards.build_pod_shelf_card(_shelf_view(), {})
        assert card["header"]["title"]["content"] == "历史任务（5）" and card["header"]["template"] == "grey"
        forms = [e for e in card["elements"] if e["tag"] == "form"]
        assert [f["name"] for f in forms] == ["shelf_search", "plan_F02"]
        field, submit = forms[0]["elements"]
        assert (field["name"], field["required"], field["max_length"]) == ("shelf_q", False, 20)
        assert "default_value" not in field and forms[0]["fallback"]["tag"] == "fallback_text"
        assert (submit["action_type"], submit["text"]["content"], submit["value"]) == ("form_submit", "搜索", {"action": "pod_shelf"})
        assert _labels(card) == ["搜索", "继续裂变（再策划 33 个方向）", "交付包 B004", "候选表", "调出", "看明细", "下一页", "任务列表", "刷新", "返回工作台"]
        buttons = {b["text"]["content"]: b for b in _buttons(card)}
        assert buttons["调出"]["value"] == {"action": "pod_restore", "seed": "S02", "back": "shelf"}
        assert buttons["候选表"]["url"].endswith("&view=vew1") and "value" not in buttons["候选表"]
        assert buttons["刷新"]["value"] == {"action": "pod_shelf", "offset": 0}
        assert card["elements"][-1]["elements"][0]["content"] == "第 1–4 个，共 5 个。历史任务是选款完成、套图出完后收起的任务。"
        # 搜过：搜的字留在框里，翻页 / 刷新都带上它
        card = pod_cards.build_pod_shelf_card(_shelf_view(query="恐龙"), {})
        field = next(e for e in card["elements"] if e["tag"] == "form")["elements"][0]
        assert field["default_value"] == "恐龙"
        buttons = {b["text"]["content"]: b for b in _buttons(card)}
        assert buttons["刷新"]["value"] == {"action": "pod_shelf", "offset": 0, "q": "恐龙"}
        assert buttons["下一页"]["value"] == {"action": "pod_shelf", "offset": 4, "q": "恐龙"}

    def test_shelf_action_searches_and_pages(self, store):
        bridge = _FakeBridge({("ui", "shelf"): _shelf_view()})
        host = _Host(store, bridge)
        payload = host._card_action_pod_shelf(_ctx(store, {"action": "pod_shelf"}))
        assert payload["toast"]["content"] == "正在打开历史任务…" and not host.patched
        # 翻页带上一次搜的字；搜索框提交以框里的为准、从第一页看；以「-」开头、带控制字符、超长的都只当搜索字
        host._card_action_pod_shelf(_ctx(store, {"action": "pod_shelf", "offset": 4, "q": "恐龙"}))
        host._card_action_pod_shelf(_ctx(store, {"action": "pod_shelf", "offset": 4}, form_value={"shelf_q": "  -森林\n三伙伴  "}))
        host._card_action_pod_shelf(_ctx(store, {"action": "pod_shelf"}, form_value={"shelf_q": "海" * 30}))
        host._card_action_pod_shelf(_ctx(store, {"action": "pod_shelf", "offset": "--all", "q": ["S01"]}, form_value={"shelf_q": ""}))
        host.drain()
        assert bridge.calls == [
            ("ui", "shelf", "--offset", "0", "--limit", "4"),
            ("ui", "shelf", "--offset", "4", "--limit", "4", "--query=恐龙"),
            ("ui", "shelf", "--offset", "0", "--limit", "4", "--query=-森林 三伙伴"),
            ("ui", "shelf", "--offset", "0", "--limit", "4", "--query=" + "海" * 20),
            ("ui", "shelf", "--offset", "0", "--limit", "4"),
        ]
        assert host.patched[0][1]["header"]["title"]["content"] == "历史任务（5）"
        assert host._card_action_pod_shelf(_ctx(store, {}, chat_id="group-1"))["toast"]["type"] == "warning"

    def test_archive_comes_back_to_tasks(self, store):
        archived = {"seed_id": "S01", "label": "S01 森林三伙伴水彩 1020", "archived": True}
        bridge = _FakeBridge({("shelf", "archive"): archived, ("ui", "tasks"): _tasks_view()})
        host = _Host(store, bridge)
        payload = host._card_action_pod_archive(_ctx(store, {"action": "pod_archive", "seed": "S01"}))
        assert payload["toast"]["content"] == "正在归档 S01…"
        host.drain()
        assert ("shelf", "archive", "S01", "--by", "feishu:ou_x") in bridge.calls
        (card,) = [c for _, c in host.patched]
        assert card["header"]["title"]["content"] == "POD 任务列表（3）"
        assert "已归档 **S01 森林三伙伴水彩 1020**：在「历史任务」里" in _texts(host)[-1]
        # 引擎拒绝（还有在做的事）：写明原因；卡片上不放强制归档
        bridge.responses[("shelf", "archive")] = PodctlError("S01 现在不能归档：还有 2 款套图在出")
        host._card_action_pod_archive(_ctx(store, {"action": "pod_archive", "seed": "S01"}))
        host.drain()
        assert "**S01 没有归档**：S01 现在不能归档：还有 2 款套图在出" in _texts(host)[-1]
        assert all("--force" not in call for call in bridge.calls)

    def test_restore_goes_back_where_it_came_from(self, store):
        restored = {"seed_id": "S02", "label": "S02 森林三伙伴水彩 1020", "restored": True}
        bridge = _FakeBridge({("shelf", "restore"): restored, ("ui", "tasks"): _tasks_view(), ("ui", "shelf"): _shelf_view()})
        host = _Host(store, bridge)
        payload = host._card_action_pod_restore(_ctx(store, {"action": "pod_restore", "seed": "S02", "back": "shelf"}))
        assert payload["toast"]["content"] == "正在调出 S02…"
        # 归档卡片、任务明细上的「调出」不带 back：回到任务列表；back 只认 shelf
        host._card_action_pod_restore(_ctx(store, {"action": "pod_restore", "seed": "S02"}))
        host._card_action_pod_restore(_ctx(store, {"action": "pod_restore", "seed": "S02", "back": "reviews"}))
        host.drain()
        assert [call for call in bridge.calls if call[0] == "shelf"] == [("shelf", "restore", "S02", "--by", "feishu:ou_x")] * 3
        titles = [card["header"]["title"]["content"] for _, card in host.patched]
        assert titles == ["历史任务（5）", "POD 任务列表（3）", "POD 任务列表（3）"]
        assert "已调出 **S02 森林三伙伴水彩 1020**：回到任务列表" in _texts(host)[0]
        bridge.responses[("shelf", "restore")] = {"seed_id": "S02", "label": "S02", "already_active": True}
        host._card_action_pod_restore(_ctx(store, {"action": "pod_restore", "seed": "S02"}))
        host.drain()
        assert "**S02** 本来就在任务列表里" in _texts(host)[-1]

    @pytest.mark.parametrize("action", ["_card_action_pod_archive", "_card_action_pod_restore"])
    @pytest.mark.parametrize("value", [{"seed": "S01; rm -rf"}, {"seed": "--force"}, {"seed": "F01"}, {"seed": 1}, {}])
    def test_archive_and_restore_reject_bad_task_ids(self, store, action, value):
        host = _Host(store, _FakeBridge({}))
        assert getattr(host, action)(_ctx(store, value))["toast"]["type"] == "error"
        assert host.scheduled == []

    def test_shelf_actions_are_registered(self):
        assert {"pod_shelf", "pod_archive", "pod_restore"} <= pod_cards.POD_CARD_ACTIONS


class TestPodTimelineAndGallery:
    def test_timeline_and_gallery_pages(self, store, tmp_path):
        thumb = tmp_path / "IMG00007.thumb.jpg"
        thumb.write_bytes(b"jpg")
        gallery = {"title": "POD 已选款", "total": 1, "offset": 0, "limit": 3, "batch_id": "B001", "items": [{"asset_id": "IMG00007", "title": "IMG00007", "lines": [], "images": [{"path": str(thumb), "label": "IMG00007"}]}]}
        bridge = _FakeBridge({("ui", "timeline"): {"title": "POD 最近动态", "lines": ["`10-08 13:48` 出图一轮结束"]}, ("ui", "gallery"): gallery})
        host = _Host(store, bridge)
        host._card_action_pod_timeline(_ctx(store, {}))
        host._card_action_pod_gallery(_ctx(store, {"offset": 3, "batch": "B001"}))
        host._card_action_pod_gallery(_ctx(store, {"batch": "B001 --x"}))
        host.drain()
        assert bridge.calls == [
            ("ui", "timeline", "--limit", "20"),
            ("ui", "gallery", "--offset", "3", "--limit", "3", "--batch", "B001"),
            ("ui", "gallery", "--offset", "0", "--limit", "3"),
        ]
        assert "出图一轮结束" in _texts(host)[0] and "key-IMG00007.thumb.jpg" in _texts(host)[1]

    def test_send_originals_uploads_files_and_reports(self, store, tmp_path):
        original = tmp_path / "IMG00007.png"
        original.write_bytes(b"png")
        originals = {
            "files": [
                {"asset_id": "IMG00007", "path": str(original), "filename": "IMG00007.png", "bytes": 3},
                {"asset_id": "IMG00008", "path": str(original), "filename": "IMG00008.png", "bytes": 40 * 1024 * 1024},
            ],
            "refused": [{"asset_id": "IMG00009", "reason": "不是已选中的款"}],
        }
        bridge = _FakeBridge({("ui", "originals"): originals, ("ui", "gallery"): {"items": [], "total": 0}})
        host = _Host(store, bridge)
        payload = host._card_action_pod_send_originals(_ctx(store, {"asset_id": "IMG00007", "offset": 0, "batch": "B001"}))
        assert payload["toast"]["content"] == "正在发送 IMG00007 原图…"
        host.drain()
        assert bridge.calls[0] == ("ui", "originals", "--asset", "IMG00007")
        msg, attachment = host.files[0]
        assert msg.chat_id == "chat-1" and msg.channel_name == "feishu"
        # 按文件发：飞书图片消息会压缩，文件消息保留原图
        assert attachment.is_image is False and attachment.actual_path == original and attachment.filename == "IMG00007.png"
        assert len(host.files) == 1
        text = _texts(host)[0]
        assert "已发送原图：IMG00007" in text and "IMG00008（超过飞书 30MB 文件上限）" in text and "IMG00009（不是已选中的款）" in text

        host.send_ok = False
        host._card_action_pod_send_originals(_ctx(store, {"asset_id": "IMG00007"}))
        host.drain()
        assert "IMG00007（发送失败）" in _texts(host)[1]
        assert host._card_action_pod_send_originals(_ctx(store, {"asset_id": "../etc/passwd"}))["toast"]["type"] == "error"

    def test_send_originals_notice_uses_labels(self, store, tmp_path):
        original = tmp_path / "IMG00007.png"
        original.write_bytes(b"png")
        originals = {
            "files": [{"asset_id": "IMG00007", "label": "IMG00007_三伙伴开怀大笑", "path": str(original), "filename": "IMG00007_三伙伴开怀大笑.png", "bytes": 3}],
            "refused": [{"asset_id": "IMG00009", "label": "IMG00009_小熊野餐", "reason": "不是已选中的款"}],
        }
        bridge = _FakeBridge({("ui", "originals"): originals, ("ui", "gallery"): {"items": [], "total": 0}})
        host = _Host(store, bridge)
        host._card_action_pod_send_originals(_ctx(store, {"asset_id": "IMG00007"}))
        host.drain()
        assert host.files[0][1].filename == "IMG00007_三伙伴开怀大笑.png"
        text = _texts(host)[0]
        assert "已发送原图：IMG00007_三伙伴开怀大笑" in text and "IMG00009_小熊野餐（不是已选中的款）" in text


class TestPodTimer:
    def test_timer_starts_once_per_process_and_stops(self, store, tmp_path, monkeypatch):
        script = tmp_path / "podctl.py"
        script.write_text("", encoding="utf-8")
        monkeypatch.setattr(pod_cards, "podctl_path", lambda: script)
        monkeypatch.setattr(pod_cards, "_timer_owner", None)
        first, second = _Host(store, _FakeBridge({})), _Host(store, _FakeBridge({}))

        async def scenario() -> None:
            first._pod_timer_start()
            second._pod_timer_start()
            assert first._pod_timer_task is not None and getattr(second, "_pod_timer_task", None) is None
            await second._pod_timer_stop()
            assert pod_cards._timer_owner is first
            await first._pod_timer_stop()
            assert first._pod_timer_task is None and pod_cards._timer_owner is None
            second._pod_timer_start()
            assert pod_cards._timer_owner is second
            await second._pod_timer_stop()

        _run(scenario())
        monkeypatch.setattr(pod_cards, "podctl_path", lambda: tmp_path / "missing.py")

        async def without_engine() -> None:
            first._pod_timer_start()
            assert first._pod_timer_task is None and pod_cards._timer_owner is None

        _run(without_engine())

    def test_timer_loop_backs_off_on_errors_and_resets(self, store, monkeypatch):
        bridge = _FakeBridge({("tick", "--auto"): PodctlError("database is locked")})
        host = _Host(store, bridge)
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 3:
                bridge.responses[("tick", "--auto")] = {"skipped": False, "decision": "stop", "reason": "窗口未开"}
            if len(delays) > 4:
                raise asyncio.CancelledError

        monkeypatch.setattr(pod_cards, "asyncio", SimpleNamespace(sleep=fake_sleep, get_running_loop=asyncio.get_running_loop, CancelledError=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            _run(host._pod_timer_loop())
        assert delays == [60, 120, 240, 60, 60]
        assert len(bridge.calls) == 4 and set(bridge.calls) == {("tick", "--auto")}

    def test_channel_lifecycle_drives_the_timer(self, tmp_path, monkeypatch):
        from app.channels.feishu import FeishuChannel

        channel, _store = _channel(tmp_path, _FakeBridge({}))
        upstream_start_ok = {"value": True}

        async def fake_start(self) -> None:
            self._running = upstream_start_ok["value"]

        monkeypatch.setattr(FeishuChannel, "start", fake_start)
        monkeypatch.setattr(FeishuChannel, "stop", AsyncMock())
        channel._pod_timer_start = MagicMock()
        channel._pod_timer_stop = AsyncMock()
        _run(channel.start())
        channel._pod_timer_start.assert_called_once()
        _run(channel.stop())
        channel._pod_timer_stop.assert_awaited_once()
        FeishuChannel.stop.assert_awaited_once()

        # 上游没启动起来（如缺凭证）就不起定时器
        channel._running = False
        upstream_start_ok["value"] = False
        channel._pod_timer_start.reset_mock()
        _run(channel.start())
        channel._pod_timer_start.assert_not_called()


def _bitable_event(*actions, file_token: str = "bascnAPP1", table_id: str = "tblCand1", open_id: object = "ou_x"):
    return SimpleNamespace(event=SimpleNamespace(file_token=file_token, table_id=table_id, operator_id=SimpleNamespace(open_id=open_id), action_list=list(actions)))


def _record_action(record_id: str, kind: str = "record_edited", value: object = "true"):
    return SimpleNamespace(action=kind, record_id=record_id, after_value=[SimpleNamespace(field_id="fldCheck", field_value=value)])


class TestPodBitableEvents:
    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        script = tmp_path / "podctl.py"
        script.write_text("", encoding="utf-8")
        monkeypatch.setattr(pod_cards, "podctl_path", lambda: script)
        monkeypatch.setattr(pod_cards, "POD_BITABLE_DEBOUNCE_SECONDS", 0.01)
        return script

    @staticmethod
    def _settle(host: _Host, *, then=None) -> None:
        """同一个事件循环里跑完投递的协程，再等过防抖时间（then 在等之前执行，比如停掉渠道）。"""

        async def scenario() -> None:
            for coroutine in host.scheduled:
                await coroutine
            host.scheduled.clear()
            if then is not None:
                await then()
            await asyncio.sleep(0.05)

        _run(scenario())

    def test_only_ticked_edits_are_forwarded_once_per_table(self, store, engine):
        bridge = _FakeBridge({("table", "control"): {"started": "BG0101", "seed_id": "S01"}})
        host = _Host(store, bridge)
        # 新增 / 删除行（程序挪控制行）、取消勾选、文本改动都不转发
        host._on_bitable_record_changed(
            _bitable_event(
                _record_action("recAdded", "record_added"),
                _record_action("recGone", "record_deleted"),
                _record_action("recOff", value="false"),
                _record_action("recText", value='[{"type":"text","text":"true"}]'),
            )
        )
        assert host.scheduled == []
        # 同一张表在防抖时间内勾上的合并成一次调用，操作人取最后一个
        host._on_bitable_record_changed(_bitable_event(_record_action("recCtl")))
        host._on_bitable_record_changed(_bitable_event(_record_action("recImg", value=True), open_id="ou_y"))
        host._on_bitable_record_changed(_bitable_event(_record_action("recOther"), table_id="tblOther"))
        self._settle(host)
        assert sorted(bridge.calls) == [
            ("table", "control", "--file", "bascnAPP1", "--table", "tblCand1", "--records", "recCtl,recImg", "--by", "feishu:ou_y"),
            ("table", "control", "--file", "bascnAPP1", "--table", "tblOther", "--records", "recOther", "--by", "feishu:ou_x"),
        ]

    def test_bad_ids_unknown_operator_or_missing_engine_are_dropped(self, store, engine, monkeypatch):
        bridge = _FakeBridge({})
        host = _Host(store, bridge)
        host._on_bitable_record_changed(_bitable_event(_record_action("recCtl"), file_token="bad token!"))
        host._on_bitable_record_changed(_bitable_event(_record_action("rec;rm")))
        host._on_bitable_record_changed(_bitable_event(_record_action("recCtl"), open_id=None))
        host._on_bitable_record_changed(_bitable_event(_record_action("recCtl"), open_id="cli_app"))
        host._on_bitable_record_changed(SimpleNamespace(event=None))
        assert host.scheduled == []
        monkeypatch.setattr(pod_cards, "podctl_path", lambda: engine.parent / "missing.py")
        host._on_bitable_record_changed(_bitable_event(_record_action("recCtl")))
        assert host.scheduled == [] and bridge.calls == []

    def test_engine_errors_are_only_logged(self, store, engine):
        host = _Host(store, _FakeBridge({("table", "control"): PodctlError("database is locked")}))
        host._on_bitable_record_changed(_bitable_event(_record_action("recCtl")))
        self._settle(host)
        assert host._pod_bitable_pending == {} and host._pod_bitable_tasks == {}

    def test_stopping_drops_what_is_still_waiting(self, store, engine):
        bridge = _FakeBridge({})
        host = _Host(store, bridge)
        host._on_bitable_record_changed(_bitable_event(_record_action("recCtl")))
        self._settle(host, then=host._pod_bitable_stop)
        assert bridge.calls == [] and host._pod_bitable_tasks == {} and host._pod_bitable_pending == {}


class TestPodChatSwitch:
    def _sessions(self, store: ChannelStore) -> None:
        # t-main 用通用人设，t-pod 是 POD 会话（登记时快照当时的 chat 级人设）
        store.add_session("feishu", "chat-1", "t-main")
        store.set_agent("feishu", "chat-1", POD_PERSONA)
        store.add_session("feishu", "chat-1", "t-pod")
        store.set_session_title("feishu", "chat-1", "t-pod", "毛毯 B002")

    def test_enter_switches_to_latest_pod_session_and_return_goes_back(self, store):
        self._sessions(store)
        host = _Host(store, _FakeBridge({}))
        payload = host._card_action_pod_enter_chat(_ctx(store, {}))
        assert payload["toast"] == {"type": "success", "content": "已进入 POD 对话：毛毯 B002"}
        assert host.published == [("chat-1", "/sessions _gcswitch t-pod", CARD_ACTION_SOURCE)]
        assert store.get_pod_return_thread("feishu", "chat-1") == "t-main"
        assert store.get_agent("feishu", "chat-1") == POD_PERSONA

        in_pod = ChatIdentity(bound=True, binding_required=True, storage_user_id="admin-1", current_thread_id="t-pod")
        assert host._card_action_pod_enter_chat(_ctx(store, {}, identity=in_pod))["toast"]["type"] == "info"
        payload = host._card_action_pod_return_chat(_ctx(store, {}, identity=in_pod))
        assert payload["toast"]["type"] == "success" and host.published[-1][1] == "/sessions _gcswitch t-main"
        assert store.get_pod_return_thread("feishu", "chat-1") is None
        # 切回时恢复原会话的设置：人设回到通用助手
        assert store.get_agent("feishu", "chat-1") is None
        assert host._card_action_pod_return_chat(_ctx(store, {}))["toast"]["type"] == "warning"

    def test_enter_without_pod_session_creates_one(self, store):
        store.add_session("feishu", "chat-1", "t-main")
        host = _Host(store, _FakeBridge({}))
        payload = host._card_action_pod_enter_chat(_ctx(store, {}))
        assert payload["toast"]["content"] == "正在新建 POD 对话…"
        assert host.published == [("chat-1", f"/agent use {POD_PERSONA}", CARD_ACTION_SOURCE)]
        assert store.get_pod_return_thread("feishu", "chat-1") == "t-main"

    def test_return_pointer_is_not_a_session_setting(self, store):
        store.set_thread_id("feishu", "chat-1", "t-main")
        store.add_session("feishu", "chat-1", "t-main")
        store.set_pod_return_thread("feishu", "chat-1", "t-main")
        assert "pod_return_thread" not in store.get_session("feishu", "chat-1", "t-main")
        store.set_pod_return_thread("feishu", "chat-1", None)
        assert store.get_pod_return_thread("feishu", "chat-1") is None


# ---------------------------------------------------------------------------
# ForkFeishuChannel 接线：/pod、菜单、回调分发与 patch
# ---------------------------------------------------------------------------


def _channel(tmp_path: Path, bridge: _FakeBridge) -> tuple[ForkFeishuChannel, ChannelStore]:
    store = ChannelStore(path=tmp_path / "store.json")
    store.set_user_chat("feishu", "ou_x", "chat-1")
    channel = ForkFeishuChannel(MessageBus(), config={"app_id": "a", "app_secret": "s", "channel_store": store})
    channel._pod_bridge = bridge
    channel._schedule = MagicMock(side_effect=lambda coroutine, **kwargs: coroutine.close())
    channel._publish_synthetic_command = MagicMock()
    return channel, store


def _message_event(text: str, *, chat_id: str = "chat-1", chat_type: str = "p2p") -> MagicMock:
    event = MagicMock()
    event.event.message.chat_id = chat_id
    event.event.message.message_id = "om-1"
    event.event.message.chat_type = chat_type
    event.event.message.content = json.dumps({"text": text})
    event.event.sender.sender_id.open_id = "ou_x"
    return event


def _text_card(content: str) -> str:
    """上游 _build_card_content 生成的单段 markdown 卡片里的文字。"""
    return json.loads(content)["elements"][0]["content"]


_OPEN = ChatIdentity(bound=True, binding_required=True, storage_user_id="admin-1", current_thread_id="t-main")
_BLOCKED = ChatIdentity(bound=False, binding_required=True, storage_user_id=None, current_thread_id=None)


class TestPodWiring:
    def test_bare_pod_command_is_a_card_command(self, tmp_path):
        channel, _store = _channel(tmp_path, _FakeBridge({}))
        assert channel._intercept_card_command(_message_event("/pod")) is True
        assert channel._schedule.call_args.kwargs["name"] == "send_pod_card"
        assert channel._intercept_card_command(_message_event("/pod 看看进度")) is False

    def test_pod_card_is_rendered_by_the_engine_only_in_p2p(self, tmp_path):
        channel, _store = _channel(tmp_path, _FakeBridge({("ui", "dashboard"): DASHBOARD}))
        channel._resolve_identity = AsyncMock(return_value=_OPEN)
        card = _run(channel._card_or_binding_hint("pod", "chat-1", "ou_x"))
        assert card["header"]["title"]["content"] == "POD 毛毯工作台"
        assert "单聊" in _text_card(_run(channel._card_or_binding_hint("pod", "group-1", "ou_x")))
        channel._resolve_identity = AsyncMock(return_value=_BLOCKED)
        assert "绑定" in _text_card(_run(channel._card_or_binding_hint("pod", "chat-1", "ou_x")))

    def test_card_action_dispatch_passes_message_id_and_patches(self, tmp_path):
        channel, _store = _channel(tmp_path, _FakeBridge({("ui", "dashboard"): DASHBOARD}))
        channel._resolve_identity_blocking = MagicMock(return_value=_OPEN)
        scheduled: list = []
        channel._schedule = MagicMock(side_effect=lambda coroutine, **kwargs: scheduled.append(coroutine))
        channel._patch_card = AsyncMock()
        data = SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(value={"action": "pod_dashboard"}),
                context=SimpleNamespace(open_chat_id="chat-1", open_message_id="om-card"),
                operator=SimpleNamespace(open_id="ou_x"),
            )
        )
        assert channel._on_card_action(data).toast.content == "正在刷新…"
        _run(scheduled[0])
        message_id, card = channel._patch_card.await_args.args
        assert message_id == "om-card" and card["header"]["title"]["content"] == "POD 毛毯工作台"

    def test_card_action_dispatch_passes_form_value(self, tmp_path):
        bridge = _FakeBridge({("review", "decide"): {"effect": {"fix_queued": ["IMG00002"]}}, ("ui", "reviews"): _page([])})
        channel, _store = _channel(tmp_path, bridge)
        channel._resolve_identity_blocking = MagicMock(return_value=_OPEN)
        scheduled: list = []
        channel._schedule = MagicMock(side_effect=lambda coroutine, **kwargs: scheduled.append(coroutine))
        channel._patch_card = AsyncMock()
        value = {"action": "pod_decide", "review_id": "RV0007", "decision": "needs_changes", "field": "note_RV0007", "offset": 0}
        data = SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(value=value, form_value={"note_RV0007": "背景换成雾蓝"}),
                context=SimpleNamespace(open_chat_id="chat-1", open_message_id="om-card"),
                operator=SimpleNamespace(open_id="ou_x"),
            )
        )
        assert channel._on_card_action(data).toast.content.startswith("正在提交 RV0007")
        _run(scheduled[0])
        assert bridge.calls[0][-2:] == ("--note", "背景换成雾蓝")
        assert "已排队" in json.dumps(channel._patch_card.await_args.args[1], ensure_ascii=False)

    def test_paused_fix_and_resume_notices(self, tmp_path):
        bridge = _FakeBridge(
            {
                ("review", "decide"): {"effect": {"fix_queued": ["IMG00002"], "reason": "流水线已暂停"}},
                ("resume",): {"paused": False, "fixes": {"fix_started": "BG0001", "assets": ["IMG00002"]}},
                ("ui", "reviews"): _page([]),
                ("ui", "dashboard"): DASHBOARD,
            }
        )
        channel, _store = _channel(tmp_path, bridge)
        channel._resolve_identity_blocking = MagicMock(return_value=_OPEN)
        scheduled: list = []
        channel._schedule = MagicMock(side_effect=lambda coroutine, **kwargs: scheduled.append(coroutine))
        channel._patch_card = AsyncMock()
        context = SimpleNamespace(open_chat_id="chat-1", open_message_id="om-card")
        operator = SimpleNamespace(open_id="ou_x")
        value = {"action": "pod_decide", "review_id": "RV0007", "decision": "needs_changes", "field": "note_RV0007", "offset": 0}
        decide = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value=value, form_value={"note_RV0007": "改短"}), context=context, operator=operator))
        channel._on_card_action(decide)
        _run(scheduled[0])
        assert "流水线已暂停，恢复后接着改" in json.dumps(channel._patch_card.await_args.args[1], ensure_ascii=False)
        resume = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"action": "pod_resume"}), context=context, operator=operator))
        channel._on_card_action(resume)
        _run(scheduled[1])
        assert "已恢复，接着改 IMG00002" in json.dumps(channel._patch_card.await_args.args[1], ensure_ascii=False)

    @staticmethod
    def _menu(key: str) -> SimpleNamespace:
        return SimpleNamespace(event=SimpleNamespace(event_key=key, operator=SimpleNamespace(operator_id=SimpleNamespace(open_id="ou_x"))))

    def test_menu_pod_opens_workbench_and_pod_chat_enters_conversation(self, tmp_path):
        channel, store = _channel(tmp_path, _FakeBridge({}))
        channel._on_bot_menu(self._menu("pod"))
        assert channel._schedule.call_args.kwargs["name"] == "menu_pod_card"

        scheduled: list = []
        channel._schedule = MagicMock(side_effect=lambda coroutine, **kwargs: scheduled.append(coroutine))
        channel._resolve_identity = AsyncMock(return_value=_OPEN)
        channel._send_interactive = AsyncMock()
        store.add_session("feishu", "chat-1", "t-main")
        channel._on_bot_menu(self._menu("POD_CHAT"))
        _run(scheduled[0])
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", f"/agent use {POD_PERSONA}", source=CARD_ACTION_SOURCE)
        assert "正在新建 POD 对话" in _text_card(channel._send_interactive.await_args.args[0])
        assert store.get_pod_return_thread("feishu", "chat-1") == "t-main"

    def test_patch_card_sends_card_json(self, tmp_path):
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        channel, _store = _channel(tmp_path, _FakeBridge({}))
        channel._PatchMessageRequest, channel._PatchMessageRequestBody = PatchMessageRequest, PatchMessageRequestBody
        channel._api_client = MagicMock()
        channel._api_client.im.v1.message.patch.return_value = MagicMock(success=lambda: True)
        _run(channel._patch_card("om-card", {"elements": []}))
        request = channel._api_client.im.v1.message.patch.call_args.args[0]
        assert request.message_id == "om-card" and json.loads(request.request_body.content) == {"elements": []}


# ---------------------------------------------------------------------------
# 已绑定账号：新建线程时会话快照记对人设（POD 会话靠它识别）
# ---------------------------------------------------------------------------


class _Repo:
    def __init__(self) -> None:
        self.threads: dict[tuple[str, str, str], str] = {}

    async def get_thread_id(self, connection_id, external_conversation_id, external_topic_id=None):
        return self.threads.get((connection_id, external_conversation_id, external_topic_id or ""))

    async def set_thread_id(self, *, connection_id, owner_user_id, provider, external_conversation_id, thread_id, external_topic_id=None):
        self.threads[(connection_id, external_conversation_id, external_topic_id or "")] = thread_id


def test_bound_thread_creation_snapshots_the_new_persona(tmp_path):
    repo = _Repo()
    store = ChannelStore(path=tmp_path / "store.json")
    manager = ForkChannelManager(bus=MessageBus(), store=store, connection_repo=repo)
    client = MagicMock()
    client.threads.create = AsyncMock(return_value={"thread_id": "t-pod"})
    store.add_session("feishu", "chat1", "t-old")
    store.set_thread_id("feishu", "chat1", "t-stale")
    store.add_session("feishu", "chat1", "t-stale")
    msg = InboundMessage(channel_name="feishu", chat_id="chat1", user_id="ou_x", text="", msg_type=InboundMessageType.COMMAND, connection_id="conn-1", owner_user_id="admin-1")

    assert _run(manager._create_thread(client, msg, agent_name=POD_PERSONA)) == "t-pod"

    assert repo.threads[("conn-1", "chat1", "")] == "t-pod"
    assert store.get_thread_id("feishu", "chat1") == "t-pod"
    assert store.get_session("feishu", "chat1", "t-pod")["agent"] == POD_PERSONA
    # 绑定前留在 store 里的旧指针所指的会话不会被改成 POD 人设
    assert store.get_session("feishu", "chat1", "t-stale").get("agent") is None
