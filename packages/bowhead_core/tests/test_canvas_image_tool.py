"""`add_image` 单测（multimodal-v1）。

覆盖（tech-design §8）：
- 校验失败路径：缺 id / 坏格式（silent-strip skipped-shape）/ 跨 user 拒绝 /
  非图片 mime → skipped
- 成功路径：**mock bus 断言 emit 三连事件序列 + payload 字段**（emit 契约规则——
  FE 靠 section payload 里的 artifact_id/caption/alt 渲染）
- caption/alt citation marker strip + 截断
- hydration：真 CanvasStore append → list_sections + to_report_section 保真（F5 存活）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_tools import _add_image, _strip_caption_markers

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@dataclass
class _StubCtx:
    turn_id: str = "t_img"
    user_id: str = "u1"
    workspace_id: str = "w"
    session_id: str = "s_img"
    artifact_store: Any = None
    canvas_store: Any = None
    parent_bus: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


class _RecordingBus:
    """收 OrchestratorEvent，按序记录 (event_type, payload)。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: Any) -> None:
        self.events.append((str(event.event_type), dict(event.payload or {})))


@pytest.fixture
async def stores(tmp_path: Path):
    canvas = CanvasStore(tmp_path / "c.db")
    await canvas.initialize()
    backend = LocalBackend(root_dir=tmp_path / "files")
    art = ArtifactStore(backend=backend, db_path=tmp_path / "a.db")
    await art.initialize()
    return canvas, art


async def _put_png(art_store: ArtifactStore, *, created_by: str = "u1"):
    return await art_store.put_bytes(
        content=_PNG,
        filename="gen_海报_1.png",
        mime="image/png",
        source="image_generate",
        created_by=created_by,
        session_id="s_img",
        kind="deliverable",
        producer_kind="image_generate",
        created_by_turn="t_img",
    )


# ─── Validation failure paths ──────────────────────────────────────


async def test_missing_artifact_id_errors() -> None:
    out = await _add_image({}, _StubCtx())
    assert "error" in out


async def test_invalid_format_silent_strips() -> None:
    out = await _add_image({"artifact_id": "4"}, _StubCtx())
    assert "error" not in out
    assert out["skipped"] == "invalid_artifact_id_format"
    assert "image_generate" in out["hint"]


async def test_cross_user_artifact_rejected(stores) -> None:
    """SEC-P1-1：别人 user 的图 → owner check 静默 not found。"""
    canvas, art_store = stores
    other = await _put_png(art_store, created_by="someone_else")
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)
    out = await _add_image({"artifact_id": other.id}, ctx)
    assert "not found" in out.get("error", "")
    secs = await canvas.list_sections_appended_by_turn("t_img")
    assert secs == []  # Nothing persisted


async def test_non_image_mime_skipped(stores) -> None:
    canvas, art_store = stores
    csv_art = await art_store.put_bytes(
        content=b"a,b\n1,2\n",
        filename="rows.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
        session_id="s_img",
    )
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)
    out = await _add_image({"artifact_id": csv_art.id}, ctx)
    assert out["skipped"] == "not_an_image"
    assert "add_chart" in out["hint"]


# ─── Success path + emit contract ──────────────────────────────────


async def test_success_emits_three_events_with_payload(stores) -> None:
    canvas, art_store = stores
    art = await _put_png(art_store)
    bus = _RecordingBus()
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas, parent_bus=bus)

    out = await _add_image(
        {
            "artifact_id": art.id,
            "caption": "夏季促销海报 [ref:s_abc][cite:3]",
            "alt": "冰镇奶茶海报 [num:V:12:杯]",
            "anchor_heading": "营销物料",
        },
        ctx,
    )

    assert out["kind"] == "image"
    assert out["artifact_id"] == art.id
    section_id = out["section_id"]

    # Three-event sequence: streaming → done → section_added (same contract as chart).
    types = [t for t, _ in bus.events]
    assert types == [
        "canvas.section_streaming",
        "canvas.section_done",
        "canvas.section_added",
    ]
    streaming_p = bus.events[0][1]
    assert streaming_p["section_id"] == section_id
    assert streaming_p["kind"] == "image"

    done_p = bus.events[1][1]
    assert done_p["section_id"] == section_id
    sec_payload = done_p["payload"]
    assert sec_payload["kind"] == "image"
    assert sec_payload["artifact_id"] == art.id
    # Strip all markers (dual citation-path rule).
    assert sec_payload["caption"] == "夏季促销海报"
    assert sec_payload["alt"] == "冰镇奶茶海报"

    added_p = bus.events[2][1]
    assert added_p["section"]["kind"] == "image"
    assert added_p["section"]["artifact_id"] == art.id
    assert added_p["canvas_id"] == out["canvas_id"]


async def test_hydration_roundtrip(stores) -> None:
    """F5 存活：append → list_sections → to_report_section 字段保真。"""
    canvas, art_store = stores
    art = await _put_png(art_store)
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)
    out = await _add_image(
        {"artifact_id": art.id, "caption": "说明", "alt": "替代文本"},
        ctx,
    )

    secs = await canvas.list_sections_appended_by_turn("t_img")
    assert len(secs) == 1
    sec = secs[0]
    assert sec.kind == "image"
    rs = sec.to_report_section()
    assert rs["id"] == out["section_id"]
    assert rs["kind"] == "image"
    assert rs["artifact_id"] == art.id
    assert rs["caption"] == "说明"
    assert rs["alt"] == "替代文本"


async def test_caption_truncation_and_anchor(stores) -> None:
    canvas, art_store = stores
    art = await _put_png(art_store)
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)
    out = await _add_image(
        {"artifact_id": art.id, "caption": "长" * 500, "alt": "好" * 500, "anchor_heading": "  物料  "},
        ctx,
    )
    secs = await canvas.list_sections_appended_by_turn("t_img")
    p = secs[0].payload
    assert len(p["caption"]) == 200
    assert len(p["alt"]) == 120
    assert p["anchor_heading"] == "物料"
    assert out["section_id"]


def test_strip_caption_markers_patterns() -> None:
    assert _strip_caption_markers("a [ref:s_x] b [cite:2] c [chart:s_1] [num:V:3:杯] [data:art_ab12cd34ef56] [3]") == "a  b  c"
    assert _strip_caption_markers(None) == ""


async def test_no_bus_still_persists(stores) -> None:
    canvas, art_store = stores
    art = await _put_png(art_store)
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas, parent_bus=None)
    out = await _add_image({"artifact_id": art.id}, ctx)
    assert out["kind"] == "image"
    assert len(await canvas.list_sections_appended_by_turn("t_img")) == 1
