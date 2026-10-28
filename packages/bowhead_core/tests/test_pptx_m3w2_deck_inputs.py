"""Sprint pptx-from-template M3w.2 (B) — deck-render input preparation.

Covers prepare_deck_render_inputs: the backend-deterministic (.pptx-in-scope)
serialization of the canvas → sections.json + the injected compute-peer guidance
block that replaces the dropped SOP (RD-4, B-pivot).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from bowhead_core.deck_render_inputs import prepare_deck_render_inputs


@pytest.fixture(autouse=True)
def legacy_document_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the explicit legacy contract independently of packaged edition."""
    monkeypatch.setattr("bowhead_core.document_runtime.read_document_profile", lambda: "legacy")


_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@dataclass
class FakeArt:
    id: str
    filename: str
    mime: str = "application/octet-stream"
    kind: str = ""
    source: str = "upload"


@dataclass
class FakeSection:
    id: str
    seq: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeCanvas:
    id: str


class FakeArtifactStore:
    def __init__(self, session_arts: list[FakeArt]) -> None:
        self._arts = session_arts
        self.put_calls: list[dict] = []
        self.read_ids: list[str] = []

    async def list_for_session(self, *, session_id: str, user_id: str) -> list[FakeArt]:
        return self._arts

    async def get(self, artifact_id: str) -> FakeArt | None:
        return next((art for art in self._arts if art.id == artifact_id), None)

    async def open_read(self, art: FakeArt) -> bytes:
        self.read_ids.append(art.id)
        return b"synthetic-template-bytes"

    async def put_bytes(self, **kw) -> FakeArt:
        self.put_calls.append(kw)
        return FakeArt(id="art_sections_json", filename="sections.json", mime="application/json")


class FakeCanvasStore:
    def __init__(self, canvas: FakeCanvas | None, sections: list[FakeSection]) -> None:
        self._canvas = canvas
        self._sections = sections

    async def get_canvas_by_session(self, session_id: str) -> FakeCanvas | None:
        return self._canvas

    async def list_sections(self, canvas_id: str, *, include_hidden: bool = False):
        return self._sections


async def test_no_pptx_in_scope_returns_none() -> None:
    """No template → not a deck request → None (no serialization)."""
    art_store = FakeArtifactStore([FakeArt("art_csv", "data.csv", "text/csv")])
    cv_store = FakeCanvasStore(FakeCanvas("c1"), [FakeSection("s1", 0, "h1")])
    block = await prepare_deck_render_inputs(
        canvas_store=cv_store,
        artifact_store=art_store,
        session_id="sess1",
        user_id="u1",
    )
    assert block is None
    assert art_store.put_calls == []  # no sections.json written


async def test_pptx_in_scope_serializes_and_returns_block() -> None:
    """.pptx in scope + canvas content → serialize sections.json (pinned) +
    return a guidance block naming BOTH the template and the sections.json."""
    tmpl = FakeArt("art_tmpl", "业务规划.pptx", _PPTX_MIME)
    art_store = FakeArtifactStore([tmpl, FakeArt("art_csv", "d.csv", "text/csv")])
    cv_store = FakeCanvasStore(
        FakeCanvas("c1"),
        [FakeSection("s1", 0, "h1", {"text": "标题"}), FakeSection("s2", 1, "kpi_row", {"cells": []})],
    )
    block = await prepare_deck_render_inputs(
        canvas_store=cv_store,
        artifact_store=art_store,
        session_id="sess1",
        user_id="u1",
    )
    assert block is not None
    # serialized sections.json was pinned to the session, typed canvas_export
    assert len(art_store.put_calls) == 1
    pc = art_store.put_calls[0]
    assert pc["session_id"] == "sess1"
    assert pc["source"] == "canvas_export"
    assert pc["filename"] == "sections.json"
    # block names BOTH inputs by artifact_id so the peer can pass them
    assert "art_tmpl" in block  # template
    assert "art_sections_json" in block  # serialized content
    assert "python_sandbox" in block and "output.pptx" in block
    # the serialized payload carries the full section shapes (no truncation)
    body = pc["content"].decode("utf-8")
    assert "kpi_row" in body and "标题" in body


async def test_pptx_detected_by_extension_even_without_mime() -> None:
    tmpl = FakeArt("art_tmpl2", "deck.PPTX", "application/octet-stream")
    art_store = FakeArtifactStore([tmpl])
    cv_store = FakeCanvasStore(FakeCanvas("c1"), [FakeSection("s1", 0, "h1")])
    block = await prepare_deck_render_inputs(
        canvas_store=cv_store,
        artifact_store=art_store,
        session_id="sess1",
        user_id="u1",
    )
    assert block is not None and "art_tmpl2" in block


async def test_pptx_in_scope_but_no_canvas_yet() -> None:
    """Template uploaded but no canvas content yet → block still returned (peer
    is told to bi_fetch / clarify), but NO sections.json is written."""
    tmpl = FakeArt("art_tmpl", "t.pptx", _PPTX_MIME)
    art_store = FakeArtifactStore([tmpl])
    cv_store = FakeCanvasStore(None, [])  # no canvas
    block = await prepare_deck_render_inputs(
        canvas_store=cv_store,
        artifact_store=art_store,
        session_id="sess1",
        user_id="u1",
    )
    assert block is not None
    assert art_store.put_calls == []  # nothing to serialize
    assert "暂无 canvas 内容" in block  # peer told to fetch/clarify


async def test_empty_canvas_sections_no_serialize() -> None:
    """Canvas exists but has 0 sections → no sections.json written."""
    tmpl = FakeArt("art_tmpl", "t.pptx", _PPTX_MIME)
    art_store = FakeArtifactStore([tmpl])
    cv_store = FakeCanvasStore(FakeCanvas("c1"), [])
    block = await prepare_deck_render_inputs(
        canvas_store=cv_store,
        artifact_store=art_store,
        session_id="sess1",
        user_id="u1",
    )
    assert block is not None
    assert art_store.put_calls == []


async def test_best_effort_on_store_error() -> None:
    """Any store error → None (spawn proceeds without deck inputs)."""

    class Boom:
        async def list_for_session(self, **kw):
            raise RuntimeError("db down")

    block = await prepare_deck_render_inputs(
        canvas_store=FakeCanvasStore(None, []),
        artifact_store=Boom(),
        session_id="sess1",
        user_id="u1",
    )
    assert block is None


async def test_missing_stores_returns_none() -> None:
    assert (
        await prepare_deck_render_inputs(
            canvas_store=None,
            artifact_store=None,
            session_id="",
            user_id=None,
        )
        is None
    )


# ── Sprint followup-context-deck (P0-B) — no-template deck path ──────────────


async def test_consultant_no_template_falls_back_to_default() -> None:
    """CONSULTANT spawn with NO uploaded .pptx → use the bundled default
    template (so "做个ppt" still yields a deck) + reuse the prior-turn canvas."""
    art_store = FakeArtifactStore([FakeArt("art_csv", "data.csv", "text/csv")])
    cv_store = FakeCanvasStore(
        FakeCanvas("c1"),
        [FakeSection("s1", 0, "h1", {"text": "2025各产品线销售变化"})],
    )
    block = await prepare_deck_render_inputs(
        canvas_store=cv_store,
        artifact_store=art_store,
        session_id="sess1",
        user_id="u1",
        mode="consultant",
    )
    assert block is not None, "consultant deck request must not silently return None"
    assert "render_deck" in block  # routes through the tested renderer
    assert "server-bound template" in block
    assert "Existing Canvas material" in block
    # sections.json for the prior report was serialized (canvas reuse)
    assert art_store.put_calls, "prior canvas should be serialized for the deck"


async def test_compute_no_template_still_returns_none() -> None:
    """COMPUTE spawn with no template is unchanged — not a deck request."""
    art_store = FakeArtifactStore([FakeArt("art_csv", "data.csv", "text/csv")])
    cv_store = FakeCanvasStore(FakeCanvas("c1"), [FakeSection("s1", 0, "h1")])
    block = await prepare_deck_render_inputs(
        canvas_store=cv_store,
        artifact_store=art_store,
        session_id="sess1",
        user_id="u1",
        mode="compute",
    )
    assert block is None
    assert art_store.put_calls == []


@pytest.mark.parametrize("profile,delivery", [("compute", None), ("compute", "chat"), ("compute", "report"), ("consultant", "report")])
async def test_spawn_prepares_deck_only_for_the_deck_peer(monkeypatch, profile, delivery) -> None:
    """An old pinned template must not turn a new read task into a deck job."""
    from bowhead_core.subagent import register_subagent_tool
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
    from test_subagent_spawn_delivery import _CapturingRunner, _FakeBus

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    monkeypatch.setenv("FEATURE_DELIVERY_HINT_GROUNDED", "0")
    art_store = FakeArtifactStore([FakeArt("art_old_template", "previous-quarter.pptx", _PPTX_MIME)])
    canvas = FakeCanvasStore(FakeCanvas("c1"), [FakeSection("s1", 0, "h1", {"text": "Previous analysis"})])
    runner = _CapturingRunner()
    registry = build_default_registry()
    register_subagent_tool(registry)
    task = "Create a deck from the previous analysis." if profile == "consultant" else "Read only this turn's JSON and return its profit margin."
    ctx = ToolExecutionContext(
        user_id="u1",
        member_uid="u1",
        workspace_id="ws_test",
        session_id="s_test",
        turn_id="t_followup",
        datarc=None,
        subagent_runner=runner,
        parent_bus=_FakeBus(),
        artifact_store=art_store,
        canvas_store=canvas,
        user_text=task,
    )
    result = await registry.execute("subagent_spawn", {"profile": profile, "task": task, **({"delivery": delivery} if delivery else {})}, ctx)
    assert result.error is None
    assert runner.kwargs["task"] == task
    if profile == "consultant":
        assert len(art_store.put_calls) == 1
        assert "render_deck" in runner.kwargs["extra_instructions"]
        assert "not an in-place edit" in runner.kwargs["extra_instructions"]
    else:
        assert art_store.put_calls == []
        assert "output.pptx" not in runner.kwargs["extra_instructions"]


@pytest.mark.parametrize("delivery", [None, "chat", "report"])
async def test_spawned_consultant_gets_new_deck_authority_without_a_report_assertion(monkeypatch, delivery) -> None:
    """116 t_211c92781dd4: Lead dispatched a new deck without `delivery`, so render_deck was refused."""
    import shutil
    from pathlib import Path

    from bowhead_core.deck_render_tool import DeckRenderBinding, deck_target
    from bowhead_core.subagent import register_subagent_tool
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
    from test_subagent_spawn_delivery import _CapturingRunner, _FakeBus

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    monkeypatch.setenv("FEATURE_DELIVERY_HINT_GROUNDED", "0")
    runner = _CapturingRunner()
    registry = build_default_registry()
    register_subagent_tool(registry)
    task = "Create a six-slide landscape presentation."
    ctx = ToolExecutionContext(
        user_id="u1",
        member_uid="u1",
        workspace_id="ws_test",
        session_id="s_test",
        turn_id="t_deck",
        datarc=None,
        subagent_runner=runner,
        parent_bus=_FakeBus(),
        artifact_store=FakeArtifactStore([]),
        canvas_store=FakeCanvasStore(None, []),
        user_text=task,
    )
    result = await registry.execute("subagent_spawn", {"profile": "consultant", "task": task, **({"delivery": delivery} if delivery else {})}, ctx)
    assert result.error is None
    binding = runner.kwargs["parent_ctx"].deck_render_binding
    try:
        assert isinstance(binding, DeckRenderBinding) and binding.target == deck_target(runner.kwargs["parent_ctx"])
        assert binding.required is True
        assert "render_deck" in runner.kwargs["extra_instructions"]
    finally:
        if binding is not None:
            shutil.rmtree(Path(binding.template_path).parent, ignore_errors=True)


@pytest.mark.parametrize("kind,source", [("deliverable", "upload"), ("", "python_sandbox"), ("deck_preview", "upload")])
async def test_new_deck_style_reference_excludes_generated_output(kind, source):
    artifacts = FakeArtifactStore(
        [FakeArt("art_output", "output.pptx", _PPTX_MIME, kind, source), FakeArt("art_reference", "reference.pptx", _PPTX_MIME)]
    )
    block = await prepare_deck_render_inputs(
        canvas_store=FakeCanvasStore(None, []), artifact_store=artifacts, session_id="s_test", user_id="u1", mode="consultant"
    )
    assert artifacts.read_ids == ["art_reference"]
    assert "not an in-place edit" in block
    assert "existing-template edits retain their original workflows" in block


def test_overclaim_caveat_needed_logic() -> None:
    from types import SimpleNamespace as NS

    from bowhead_core.deck_render_inputs import deck_overclaim_caveat_needed

    consultant_spawn = NS(tool_name="subagent_spawn", arguments={"profile": "consultant"})
    compute_spawn = NS(tool_name="subagent_spawn", arguments={"profile": "compute"})
    pptx_art = NS(created_by_turn="t1", filename="consultant-deck.pptx", kind="deliverable")
    other_art = NS(created_by_turn="t1", filename="data.csv", kind="tool_data")
    pptx_other_turn = NS(created_by_turn="tOTHER", filename="d.pptx", kind="deliverable")

    # consultant spawned, no deck artifact → caveat needed (the dogfood case)
    assert deck_overclaim_caveat_needed(tool_invocations=[consultant_spawn], turn_artifacts=[other_art], turn_id="t1") is True
    # consultant spawned + a real .pptx deliverable this turn → no caveat
    assert deck_overclaim_caveat_needed(tool_invocations=[consultant_spawn], turn_artifacts=[pptx_art], turn_id="t1") is False
    # NOT a consultant turn (compute) → never caveat
    assert deck_overclaim_caveat_needed(tool_invocations=[compute_spawn], turn_artifacts=[], turn_id="t1") is False
    # a .pptx from a DIFFERENT turn doesn't count → caveat needed
    assert deck_overclaim_caveat_needed(tool_invocations=[consultant_spawn], turn_artifacts=[pptx_other_turn], turn_id="t1") is True
    # empty everything → no caveat
    assert deck_overclaim_caveat_needed(tool_invocations=[], turn_artifacts=[], turn_id="t1") is False


def test_materialize_default_template_returns_real_file() -> None:
    from pathlib import Path

    from bowhead_core.deck_render_inputs import _materialize_default_template

    p = _materialize_default_template()
    assert p is not None, "The selected packaged template must be available"
    assert Path(p).is_file() and Path(p).suffix == ".pptx"
