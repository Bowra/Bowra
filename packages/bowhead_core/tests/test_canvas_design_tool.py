"""Editable design canvas tools (multimodal-v2A)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_tools import (
    _create_design,
    _flatten_design,
    _update_design_text,
    register_canvas_tools,
)
from bowhead_core.tool_registry import ToolRegistry

from bowhead_core import CanvasStore
from bowhead_core import metrics as core_metrics

if TYPE_CHECKING:
    from pathlib import Path

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@dataclass
class _StubCtx:
    turn_id: str = "t_design"
    user_id: str = "u1"
    workspace_id: str = "w"
    session_id: str = "s_design"
    artifact_store: Any = None
    canvas_store: Any = None
    parent_bus: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def _enable_design_v2a(monkeypatch):
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "1")


@pytest.fixture(autouse=True)
def _reset_metrics():
    core_metrics.get_registry().reset()
    yield
    core_metrics.get_registry().reset()


@pytest.fixture
async def stores(tmp_path: Path):
    canvas = CanvasStore(tmp_path / "c.db")
    await canvas.initialize()
    backend = LocalBackend(root_dir=tmp_path / "files")
    art = ArtifactStore(backend=backend, db_path=tmp_path / "a.db")
    await art.initialize()
    return canvas, art


def test_register_canvas_tools_skips_design_tools_when_flag_off(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "0")
    registry = ToolRegistry()

    register_canvas_tools(registry)

    assert registry.has("add_image")
    assert not registry.has("canvas_create_design")
    assert not registry.has("canvas_update_design_text")
    assert not registry.has("canvas_flatten_design")


def test_register_canvas_tools_includes_design_tools_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_DESIGN_V2A", raising=False)
    registry = ToolRegistry()

    register_canvas_tools(registry)

    assert registry.has("canvas_create_design")
    assert registry.has("canvas_update_design_text")
    assert registry.has("canvas_flatten_design")


def test_register_canvas_tools_includes_design_tools_when_flag_on(_enable_design_v2a) -> None:
    registry = ToolRegistry()

    register_canvas_tools(registry)

    assert registry.has("canvas_create_design")
    assert registry.has("canvas_update_design_text")
    assert registry.has("canvas_flatten_design")


async def _put_bg(
    art_store: ArtifactStore,
    *,
    created_by: str = "u1",
    mime: str = "image/png",
    filename: str = "bg.png",
    content: bytes = _PNG,
):
    return await art_store.put_bytes(
        content=content,
        filename=filename,
        mime=mime,
        source="image_generate",
        created_by=created_by,
        session_id="s_design",
        kind="tool_data",
        inject="stub",
        producer_kind="image_generate",
        created_by_turn="t_design",
    )


def _layers() -> list[dict[str, Any]]:
    return [
        {
            "id": "title",
            "type": "text",
            "text": "夏日冰爽",
            "x": 0.1,
            "y": 0.16,
            "w": 0.8,
            "h": 0.12,
            "font_size": 72,
            "color": "#0055AA",
            "align": "center",
        },
        {
            "id": "subtitle",
            "type": "text",
            "text": "第二杯半价",
            "x": 0.14,
            "y": 0.3,
            "w": 0.72,
            "h": 0.1,
            "font_size": 44,
            "color": "#111111",
            "align": "center",
        },
    ]


async def _create(stores):
    canvas, art_store = stores
    bg = await _put_bg(art_store)
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)
    out = await _create_design(
        {
            "background_artifact_id": bg.id,
            "title": "奶茶促销海报",
            "size_preset": "portrait_3_4",
            "layers": _layers(),
        },
        ctx,
    )
    return canvas, art_store, out


async def test_create_design_appends_design_section(stores, _enable_design_v2a) -> None:
    canvas, _, out = await _create(stores)

    assert out["kind"] == "design"
    sections = await canvas.list_sections_appended_by_turn("t_design")
    assert len(sections) == 1
    section = sections[0]
    assert section.kind == "design"
    payload = section.payload
    assert payload["title"] == "奶茶促销海报"
    assert payload["size_preset"] == "portrait_3_4"
    assert payload["canvas_width"] == 1140
    assert payload["canvas_height"] == 1472
    assert payload["background"]["artifact_id"] == out["background_artifact_id"]
    assert payload["layers"][0]["id"] == "title"
    assert payload["layers"][0]["font_family"] == "Noto Sans SC"
    assert payload["flattened_artifact_id"] is None
    assert "bowhead_design_create_total" in core_metrics.get_registry().render_prometheus()


async def test_create_design_disabled_by_flag(stores, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "0")
    canvas, art_store = stores
    bg = await _put_bg(art_store)
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)

    out = await _create_design(
        {
            "background_artifact_id": bg.id,
            "title": "x",
            "size_preset": "portrait_3_4",
            "layers": _layers(),
        },
        ctx,
    )

    assert out["skipped"] == "design_v2a_disabled"
    assert await canvas.list_sections_appended_by_turn("t_design") == []


async def test_create_design_rejects_cross_user_background(stores, _enable_design_v2a) -> None:
    canvas, art_store = stores
    bg = await _put_bg(art_store, created_by="other")
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)

    out = await _create_design(
        {
            "background_artifact_id": bg.id,
            "title": "x",
            "size_preset": "portrait_3_4",
            "layers": _layers(),
        },
        ctx,
    )

    assert "not found" in out["error"]
    assert await canvas.list_sections_appended_by_turn("t_design") == []


async def test_create_design_rejects_svg_background(stores, _enable_design_v2a) -> None:
    canvas, art_store = stores
    bg = await _put_bg(
        art_store,
        filename="bg.svg",
        mime="image/svg+xml",
        content=b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    )
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)

    out = await _create_design(
        {
            "background_artifact_id": bg.id,
            "title": "x",
            "size_preset": "portrait_3_4",
            "layers": _layers(),
        },
        ctx,
    )

    assert out["skipped"] == "not_an_image"
    assert await canvas.list_sections_appended_by_turn("t_design") == []


async def test_update_design_text_by_layer_id_preserves_other_layers(
    stores,
    _enable_design_v2a,
) -> None:
    canvas, _, out = await _create(stores)
    ctx = _StubCtx(canvas_store=canvas)

    patched = await _update_design_text(
        {
            "section_id": out["section_id"],
            "layer_id": "subtitle",
            "match_text": "ignored audit context",
            "text": "买一送一",
            "expected_rev": 1,
        },
        ctx,
    )

    assert patched["layer_id"] == "subtitle"
    assert patched["rev"] == 2
    section = (await canvas.list_sections_appended_by_turn("t_design"))[0]
    assert section.payload["layers"][0]["text"] == "夏日冰爽"
    assert section.payload["layers"][1]["text"] == "买一送一"
    assert section.payload["layers"][1]["font_size"] == 44


async def test_update_design_text_emits_patch_and_invalidates_flatten_cache(
    stores,
    _enable_design_v2a,
) -> None:
    canvas, _, out = await _create(stores)
    section = (await canvas.list_sections_appended_by_turn("t_design"))[0]
    cached = await canvas.patch_section(
        section_id=section.id,
        patch={
            "flattened_artifact_id": "art_flattened001",
            "flattened_source_rev": section.rev,
            "flattened_at": "2026-06-12T00:00:00Z",
        },
        expected_rev=section.rev,
    )
    assert cached is not None
    bus = _Bus()
    ctx = _StubCtx(canvas_store=canvas, parent_bus=bus)

    patched = await _update_design_text(
        {
            "section_id": out["section_id"],
            "layer_id": "subtitle",
            "text": "买一送一",
            "expected_rev": cached.rev,
        },
        ctx,
    )

    assert patched["rev"] == cached.rev + 1
    section = (await canvas.list_sections_appended_by_turn("t_design"))[0]
    assert section.payload["layers"][1]["text"] == "买一送一"
    assert section.payload["flattened_artifact_id"] is None
    assert section.payload["flattened_source_rev"] is None
    assert section.payload["flattened_at"] is None
    assert len(bus.events) == 1
    event = bus.events[0]
    assert event.event_type == "canvas.section_patched"
    assert event.payload["section_id"] == out["section_id"]
    assert event.payload["rev"] == patched["rev"]
    assert event.payload["patch"]["layers"][1]["text"] == "买一送一"
    assert event.payload["patch"]["flattened_artifact_id"] is None


async def test_update_design_text_by_exact_match(stores, _enable_design_v2a) -> None:
    canvas, _, out = await _create(stores)
    ctx = _StubCtx(canvas_store=canvas)

    patched = await _update_design_text(
        {
            "section_id": out["section_id"],
            "match_text": "第二杯半价",
            "text": "限时三天",
        },
        ctx,
    )

    assert patched["layer_id"] == "subtitle"
    section = (await canvas.list_sections_appended_by_turn("t_design"))[0]
    assert section.payload["layers"][1]["text"] == "限时三天"


async def test_update_design_text_missing_locator(stores, _enable_design_v2a) -> None:
    canvas, _, out = await _create(stores)
    ctx = _StubCtx(canvas_store=canvas)

    patched = await _update_design_text(
        {"section_id": out["section_id"], "text": "新文案"},
        ctx,
    )

    assert patched["skipped"] == "missing_layer_locator"


async def test_update_design_text_rejects_empty_text(stores, _enable_design_v2a) -> None:
    canvas, _, out = await _create(stores)
    ctx = _StubCtx(canvas_store=canvas)

    patched = await _update_design_text(
        {"section_id": out["section_id"], "layer_id": "title", "text": "   \n\t"},
        ctx,
    )

    assert patched["skipped"] == "empty_text"
    section = (await canvas.list_sections_appended_by_turn("t_design"))[0]
    assert section.payload["layers"][0]["text"] == "夏日冰爽"


async def test_update_design_text_ambiguous_match(stores, _enable_design_v2a) -> None:
    canvas, art_store = stores
    bg = await _put_bg(art_store)
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)
    duplicate_layers = _layers()
    duplicate_layers[1]["text"] = duplicate_layers[0]["text"]
    out = await _create_design(
        {
            "background_artifact_id": bg.id,
            "title": "x",
            "size_preset": "portrait_3_4",
            "layers": duplicate_layers,
        },
        ctx,
    )

    patched = await _update_design_text(
        {
            "section_id": out["section_id"],
            "match_text": "夏日冰爽",
            "text": "新文案",
        },
        _StubCtx(canvas_store=canvas),
    )

    assert patched["skipped"] == "ambiguous_layer"
    assert patched["candidate_layer_ids"] == ["title", "subtitle"]


async def test_update_design_text_rev_conflict(stores, _enable_design_v2a) -> None:
    canvas, _, out = await _create(stores)
    ctx = _StubCtx(canvas_store=canvas)

    patched = await _update_design_text(
        {
            "section_id": out["section_id"],
            "layer_id": "title",
            "text": "新标题",
            "expected_rev": 99,
        },
        ctx,
    )

    assert patched["skipped"] == "rev_conflict"
    assert patched["current_rev"] == 1


async def test_update_design_text_redirects_wrong_section_id_single_design(
    stores,
    _enable_design_v2a,
) -> None:
    """Cross-turn the LLM often passes the canvas_id / a simple-int / a
    fabricated sec_N instead of the real design section_id. With exactly one
    design the resolver redirects to it (instead of failing → model regenerating
    the whole poster)."""
    canvas, _, out = await _create(stores)
    ctx = _StubCtx(canvas_store=canvas)

    # pass the CANVAS id (the exact run-1 failure shape), not the section id
    patched = await _update_design_text(
        {
            "section_id": out["canvas_id"],
            "match_text": "夏日冰爽",
            "text": "盛夏冰饮节",
        },
        ctx,
    )

    assert patched.get("rev") == 2
    assert patched["section_id"] == out["section_id"]
    section = (await canvas.list_sections_appended_by_turn("t_design"))[0]
    assert section.payload["layers"][0]["text"] == "盛夏冰饮节"


async def test_design_tools_clarify_when_multiple_designs_and_bad_id(
    stores,
    _enable_design_v2a,
) -> None:
    """With >1 design the single-design shortcut is unsafe, so a bad id yields a
    needs_clarification listing the real section ids (model retries precisely)
    instead of silently editing/flattening the wrong one."""
    canvas, art_store, out1 = await _create(stores)
    _, _, out2 = await _create(stores)  # second design in the same canvas
    ctx = _StubCtx(artifact_store=art_store, canvas_store=canvas)

    upd = await _update_design_text(
        {"section_id": "sec_bogus", "match_text": "夏日冰爽", "text": "x"},
        ctx,
    )
    assert upd["skipped"] == "needs_clarification"
    assert upd["reason"] == "design_section_ambiguous"
    assert {d["section_id"] for d in upd["designs"]} == {
        out1["section_id"],
        out2["section_id"],
    }

    flat = await _flatten_design({"section_id": "sec_bogus"}, ctx)
    assert flat["skipped"] == "needs_clarification"
    assert {d["section_id"] for d in flat["designs"]} == {
        out1["section_id"],
        out2["section_id"],
    }


async def test_marketing_edits_existing_design_without_image_generation(stores, _enable_design_v2a, monkeypatch) -> None:
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec
    from test_peer_empty_closeout import ScriptedRouter

    from bowhead_router import LLMResponse, ToolCall, ToolSchema

    canvas, art_store, created = await _create(stores)
    registry = ToolRegistry()
    register_canvas_tools(registry)
    register_subagent_return_tool(registry)
    image_calls = []

    async def generate(_args, _ctx):
        image_calls.append(True)
        raise AssertionError("A text-only edit must not generate an image")

    registry.register(
        ToolSpec(schema=ToolSchema(name="image_generate", description="Generate image", input_schema={"type": "object"}), handler=generate)
    )
    monkeypatch.setitem(
        PROFILES,
        "marketing-analyst",
        SubagentProfile(
            name="marketing-analyst",
            llm_agent_type="analyst",
            system_prompt="Edit the requested saved text and return the receipt.",
            tool_whitelist=("image_generate", "canvas_update_design_text", "subagent_return"),
            max_iterations=2,
        ),
    )
    router = ScriptedRouter(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="canvas_update_design_text",
                        arguments={"section_id": created["section_id"], "layer_id": "subtitle", "text": "买一送一", "expected_rev": 1},
                    )
                ],
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="return",
                        name="subagent_return",
                        arguments={
                            "payload": {
                                "delivery_surface": "chat",
                                "exec_summary": "Updated only the requested subtitle in the existing editable design.",
                            }
                        },
                    )
                ],
            ),
        ]
    )
    ctx = ToolExecutionContext(
        turn_id="t_design",
        user_id="u1",
        member_uid="m1",
        workspace_id="w",
        session_id="s_design",
        datarc=None,
        artifact_store=art_store,
        canvas_store=canvas,
        unconfigured_capability_tools=frozenset({"image_generate"}),
    )
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name="marketing-analyst",
        task="Change only the subtitle of the saved poster",
        parent_turn_id=ctx.turn_id,
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )
    assert result.ok
    assert not image_calls
    assert all("image_generate" not in {schema.name for schema in request.tools} for request in router.requests)
    assert [inv.tool_name for inv in result.tool_invocations] == ["canvas_update_design_text", "subagent_return"]
    section = (await canvas.list_sections_appended_by_turn("t_design"))[0]
    assert section.payload["layers"][1]["text"] == "买一送一"
    assert section.payload["layers"][0]["text"] == "夏日冰爽"
    assert section.rev == 2
