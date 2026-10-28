"""V4 Sprint 2 PRD-D-4-1 · canvas_store invalidation hook integration.

Verifies that delete_canvas / delete_section / patch_section trigger the
injected `_anchor_invalidation_hook` with the correct event_kind +
canvas_id / section_id / new_payload arguments.
"""

from __future__ import annotations

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_router.invocation import (
    physical_invocation_required,
    physical_invocation_scope,
)
from bowhead_router.usage import current_usage_context, record_usage, usage_context


@pytest.fixture
async def store(tmp_path):
    s = CanvasStore(db_path=tmp_path / "cv.db")
    await s.initialize()
    return s


@pytest.fixture
def hook_calls():
    """Recorder list. Each entry: (event_kind, canvas_id, section_id, new_payload)."""
    return []


@pytest.fixture
def make_hook(hook_calls):
    async def _hook(event_kind, canvas_id, section_id, new_payload):
        hook_calls.append((event_kind, canvas_id, section_id, new_payload))

    return _hook


async def _seed_canvas_with_section(
    store,
    *,
    kind: str = "kpi_row",
    payload: dict | None = None,
):
    canvas = await store.ensure_canvas(
        session_id="s",
        workspace_id="ws",
        user_id="u",
        title="T",
    )
    section = await store.append_section(
        canvas_id=canvas.id,
        kind=kind,
        payload=payload or {"label": "Q1", "value": "100"},
    )
    return canvas, section


# ─── delete_canvas ───────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_canvas_triggers_hook(store, hook_calls, make_hook):
    canvas, _ = await _seed_canvas_with_section(store)
    store._anchor_invalidation_hook = make_hook
    ok = await store.delete_canvas(canvas_id=canvas.id)
    assert ok is True
    assert len(hook_calls) == 1
    event, cid, sid, payload = hook_calls[0]
    assert event == "canvas_deleted"
    assert cid == canvas.id
    assert sid is None
    assert payload is None


@pytest.mark.asyncio
async def test_delete_canvas_no_hook_when_unset(store):
    canvas, _ = await _seed_canvas_with_section(store)
    # _anchor_invalidation_hook is None by default
    ok = await store.delete_canvas(canvas_id=canvas.id)
    assert ok is True


# ─── delete_section ──────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_section_triggers_hook(store, hook_calls, make_hook):
    canvas, section = await _seed_canvas_with_section(store)
    store._anchor_invalidation_hook = make_hook
    # A2 (report-edit-audit): delete_section returns **actually deleted IDs** (cascade needs full list),
    # empty list falsy retains `if not ok:`; noncascade deletion →singleton list.
    deleted = await store.delete_section(section_id=section.id)
    assert deleted == [section.id]
    assert hook_calls == [
        ("section_deleted", canvas.id, section.id, None),
    ]


# ─── patch_section ───────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_section_triggers_hook_with_merged_payload(
    store,
    hook_calls,
    make_hook,
):
    canvas, section = await _seed_canvas_with_section(
        store,
        payload={"label": "Q1", "value": "100"},
    )
    store._anchor_invalidation_hook = make_hook
    result = await store.patch_section(
        section_id=section.id,
        patch={"value": "200"},  # only value changed
    )
    assert result is not None
    assert hook_calls[0][0] == "payload_changed"
    assert hook_calls[0][1] == canvas.id
    assert hook_calls[0][2] == section.id
    # Merged payload reflects shallow merge
    new_payload = hook_calls[0][3]
    assert new_payload == {"label": "Q1", "value": "200"}


# ─── error handling ──────────────────────────────────────


@pytest.mark.asyncio
async def test_hook_exception_does_not_block_canvas_delete(store):
    canvas, _ = await _seed_canvas_with_section(store)

    async def _broken_hook(*args):
        raise RuntimeError("boom")

    store._anchor_invalidation_hook = _broken_hook
    # Must not raise — canvas delete must succeed
    ok = await store.delete_canvas(canvas_id=canvas.id)
    assert ok is True
    # Verify canvas actually deleted
    assert await store.get_canvas(canvas.id) is None


@pytest.mark.asyncio
async def test_patch_anchor_hook_is_non_root_auxiliary_and_failure_keeps_primary(
    store,
) -> None:
    _canvas, section = await _seed_canvas_with_section(store)
    partial_usage: list[object] = []
    observed: dict[str, object] = {}

    async def _broken_hook(*_args):
        observed["physical_required"] = physical_invocation_required()
        observed["reason"] = current_usage_context().reason
        record_usage(
            agent_type="test",
            provider="test",
            model="test",
            tokens_input=5,
            tokens_output=1,
        )
        raise RuntimeError("hook crashed")

    store._anchor_invalidation_hook = _broken_hook
    with (
        physical_invocation_scope(object()),
        usage_context(
            turn_id="turn_1",
            reason="turn",
            turn_usage_sink=partial_usage.append,
        ),
    ):
        result = await store.patch_section(
            section_id=section.id,
            patch={"value": "200"},
        )
        assert physical_invocation_required() is True

    persisted = next(item for item in await store.list_sections(_canvas.id) if item.id == section.id)
    assert result is not None
    assert persisted is not None
    assert persisted.payload["value"] == "200"
    assert observed == {
        "physical_required": False,
        "reason": "canvas_anchor_invalidation",
    }
    assert partial_usage == []


# ─── memory-revive S1: discovery §6.4 missing-hook fix ────────────


async def _seed_two_narratives(store):
    canvas = await store.ensure_canvas(session_id="s", workspace_id="ws", user_id="u", title="T")
    s1 = await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "段落甲内容" * 3})
    s2 = await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "段落乙内容"})
    return canvas, s1, s2


@pytest.mark.asyncio
async def test_split_section_triggers_payload_changed(store, hook_calls, make_hook):
    canvas, s1, _ = await _seed_two_narratives(store)
    store._anchor_invalidation_hook = make_hook
    result = await store.split_section(section_id=s1.id, offset=3)
    assert result is not None
    assert len(hook_calls) == 1
    event, cid, sid, payload = hook_calls[0]
    assert event == "payload_changed"
    assert cid == canvas.id and sid == s1.id
    # Hook receives truncated payload (same-source recheck_hash detects stale).
    assert payload["markdown"] == result[0].payload["markdown"]


@pytest.mark.asyncio
async def test_merge_with_prev_triggers_change_and_delete(store, hook_calls, make_hook):
    canvas, s1, s2 = await _seed_two_narratives(store)
    store._anchor_invalidation_hook = make_hook
    merged = await store.merge_with_prev(section_id=s2.id)
    assert merged is not None
    assert [(e, c, s) for e, c, s, _ in hook_calls] == [
        ("payload_changed", canvas.id, s1.id),
        ("section_deleted", canvas.id, s2.id),
    ]
    assert hook_calls[0][3]["markdown"] == merged.payload["markdown"]


@pytest.mark.asyncio
async def test_clear_sections_triggers_per_section_delete(store, hook_calls, make_hook):
    canvas, s1, s2 = await _seed_two_narratives(store)
    store._anchor_invalidation_hook = make_hook
    n = await store.clear_sections(canvas_id=canvas.id)
    assert n == 2
    events = sorted((e, s) for e, _, s, _ in hook_calls)
    assert events == sorted([("section_deleted", s1.id), ("section_deleted", s2.id)])
