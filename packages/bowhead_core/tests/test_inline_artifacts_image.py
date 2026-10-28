"""_deliverable_inline_artifact / _collect_inline_artifacts 的 image 分支（multimodal-v1）。

覆盖（tech-design §8 + review UX-P0 必验）：
- raster deliverable → {kind:"image"} chip（url 带 disposition=inline + download_url）
- pptx deliverable → 仍 {kind:"file"} chip（原路径不变）
- **chip 越过 keep-gate**：report-worthy turn（有 insight_card）仍出 image chip
- 海报-only turn：image deliverable → has_deliverable=True → keep=True
  （canvas 不被 hide）且 chip 照出 —— UX-P0-1/P0-2 的代码级断言
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.agents.lead_orchestrator import (
    _collect_inline_artifacts,
    _deliverable_inline_artifact,
    is_report_worthy,
)
from bowhead_core.artifact_store import ArtifactStore, LocalBackend

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture
async def canvas_store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "c.db")
    await s.initialize()
    return s


@pytest.fixture
async def art_store(tmp_path: Path) -> ArtifactStore:
    backend = LocalBackend(root_dir=tmp_path / "files")
    s = ArtifactStore(backend=backend, db_path=tmp_path / "a.db")
    await s.initialize()
    return s


async def _put_image(art_store: ArtifactStore, turn: str = "t_mm"):
    return await art_store.put_bytes(
        content=_PNG,
        filename="gen_海报_1.png",
        mime="image/png",
        source="image_generate",
        created_by="u",
        session_id="s_mm",
        kind="deliverable",
        producer_kind="image_generate",
        created_by_turn=turn,
    )


async def test_raster_deliverable_emits_image_chip(art_store: ArtifactStore) -> None:
    art = await _put_image(art_store)
    chip = await _deliverable_inline_artifact(art_store, art)
    assert chip is not None
    assert chip["kind"] == "image"
    assert chip["id"] == art.id
    assert chip["url"] == f"/api/v1/files/{art.id}?disposition=inline"
    assert chip["download_url"] == f"/api/v1/files/{art.id}"
    assert chip["mime"] == "image/png"
    assert chip["size_bytes"] == len(_PNG)
    assert chip["alt"]


async def test_pptx_deliverable_still_file_chip(art_store: ArtifactStore) -> None:
    art = await art_store.put_bytes(
        content=b"PK\x03\x04fake",
        filename="deck.pptx",
        mime=_PPTX_MIME,
        source="consultant_deck",
        created_by="u",
        session_id="s_mm",
        kind="deliverable",
        created_by_turn="t_mm",
    )
    chip = await _deliverable_inline_artifact(art_store, art)
    assert chip is not None
    assert chip["kind"] == "file"
    assert chip["file_type"] == "PPTX"
    assert "url" not in chip  # File chips have no inline URL field.


async def test_non_deliverable_image_no_chip(art_store: ArtifactStore) -> None:
    art = await art_store.put_bytes(
        content=_PNG,
        filename="up.png",
        mime="image/png",
        source="upload",
        created_by="u",
        session_id="s_mm",
        kind="user_upload",
    )
    assert await _deliverable_inline_artifact(art_store, art) is None


async def test_poster_only_turn_keeps_canvas_and_emits_chip(
    canvas_store: CanvasStore,
    art_store: ArtifactStore,
) -> None:
    """UX-P0-1/P0-2 断言：海报-only turn（只有 image section + image deliverable）
    → is_report_worthy keep=True（canvas 不藏）+ chat 拿到 image chip。"""
    turn = "t_poster"
    art = await _put_image(art_store, turn=turn)
    c = await canvas_store.ensure_canvas(session_id="s_mm", workspace_id="w", user_id="u")
    await canvas_store.append_section(
        canvas_id=c.id,
        kind="image",
        payload={"artifact_id": art.id, "caption": "夏季海报"},
        by_turn_id=turn,
    )

    sections = await canvas_store.list_sections_appended_by_turn(turn)
    keep, _reason = is_report_worthy(sections, has_deliverable=True)
    assert keep is True  # Deliverable keep-signal retains the canvas.

    ctx = SimpleNamespace(canvas_store=canvas_store, artifact_store=art_store)
    out = await _collect_inline_artifacts(ctx, turn)
    kinds = [a["kind"] for a in out]
    assert "image" in kinds  # Chip passes the keep-gate (an affordance, not a duplicate).
    img_chip = next(a for a in out if a["kind"] == "image")
    assert img_chip["id"] == art.id


async def test_report_worthy_turn_still_gets_image_chip(
    canvas_store: CanvasStore,
    art_store: ArtifactStore,
) -> None:
    """research+poster 组合：insight_card（report-worthy）+ chart + image deliverable
    → image chip 照出；review R5-12：deliverable 轮维持 CR-6 chip-only，
    chart 不内联（C7 有界选取只对非 deliverable 报告轮）。"""
    turn = "t_combo"
    await _put_image(art_store, turn=turn)
    c = await canvas_store.ensure_canvas(session_id="s_mm2", workspace_id="w", user_id="u")
    await canvas_store.append_section(
        canvas_id=c.id,
        kind="insight_card",
        payload={"title": "定位洞察", "body": "x"},
        by_turn_id=turn,
    )
    await canvas_store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {"type": "bar"}},
        by_turn_id=turn,
    )

    ctx = SimpleNamespace(canvas_store=canvas_store, artifact_store=art_store)
    out = await _collect_inline_artifacts(ctx, turn)
    kinds = [a["kind"] for a in out]
    assert "image" in kinds  # Affordance passes the gate.
    assert kinds.count("chart") == 0  # R5-12: deliverable Turns use chips only, without inline charts.


async def test_chat_turn_inlines_canvas_image_section(canvas_store: CanvasStore, art_store: ArtifactStore) -> None:
    """★ review2-R1：chat 声明轮里 peer 调 `add_image`（F9 的 matplotlib PNG）——
    图必须能进聊天气泡。此前它既进不了内联、又让终态把 chat 声明翻成 canvas：
    用户拿到完整 chat 答复 + 一份自己不知道存在的画布（chat directive 明写「不要提及画布」）。"""
    canvas = await canvas_store.ensure_canvas(session_id="s_img", workspace_id="w", user_id="u")
    turn = "t_chat_img"
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="image",
        payload={"artifact_id": "art_png1", "caption": "箱线图", "alt": "各省分布"},
        by_turn_id=turn,
    )
    sections = await canvas_store.list_sections_appended_by_turn(turn)
    keep, _ = is_report_worthy(sections, declared="chat")
    assert keep is False, "image 能内联 ⇒ 不该把 chat 声明翻成报告"

    ctx = SimpleNamespace(canvas_store=canvas_store, artifact_store=art_store)
    out = await _collect_inline_artifacts(ctx, turn)
    imgs = [o for o in out if o.get("kind") == "image"]
    assert len(imgs) == 1, out
    chip = imgs[0]
    # Shape must match deliverable image chips: frontend `extractInlineArtifacts` reads **top-level**
    # url/download_url and validates the `/api/v1/files/` prefix; nesting them in data silently drops them.
    assert chip["url"] == "/api/v1/files/art_png1?disposition=inline"
    assert chip["download_url"] == "/api/v1/files/art_png1"
    assert chip["alt"] == "各省分布"
    assert "data" not in chip, "image chip 不用 data 包裹（与 deliverable chip 同款）"
