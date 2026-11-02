"""去重只剩生成前的计划指纹：换措辞不算新计划；成图不做相似度判定。另含选款拼图。"""

from __future__ import annotations

from conftest import make_png
from podflow import images
from podflow.stages.plan import plan_hash


def test_plan_hash_ignores_wording_noise_but_not_targets() -> None:
    def task(to: str, fid: str = "D09") -> dict:
        return {
            "changed_fields": [{"field_id": fid, "from": "小熊", "to": to}],
            "difference_rationale": "随便写写",
        }

    assert plan_hash("F01", task("幼态小兔")) == plan_hash("F01", task(" 幼态 小兔。"))
    assert plan_hash("F01", task("Bunny")) == plan_hash("F01", task("bunny!"))
    assert plan_hash("F01", task("幼态小兔")) != plan_hash("F01", task("幼态小鹿"))
    assert plan_hash("F01", task("幼态小兔")) != plan_hash("F02", task("幼态小兔"))
    assert plan_hash("F01", task("幼态小兔")) != plan_hash(
        "F01", task("幼态小兔", "D10")
    )


def test_contact_sheet_renders() -> None:
    data = images.contact_sheet(
        [
            (make_png(1), ["IMG00001  V0001", "nano-banana-pro  archived"]),
            (make_png(2), ["IMG00002", "gpt-2.5-flare"]),
        ]
    )
    fmt, w, h = images.probe(data)
    assert fmt == "PNG" and w > 400 and h > 400


def test_sheet_label_font_and_width_fit() -> None:
    assert images.label_font(None) is None
    assert images.label_font("/nonexistent/cjk.ttf") is None
    font = images._font(18)
    assert images._fit(font, "IMG00001", 200) == "IMG00001"
    clipped = images._fit(font, "IMG00001 V0001 " * 10, 200)
    assert clipped.endswith("...") and font.getlength(clipped) <= 200
