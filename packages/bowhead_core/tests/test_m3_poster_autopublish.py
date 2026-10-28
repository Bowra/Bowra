"""M3 (report-pipeline-reliability): deterministic poster auto-publish at turn
finalize.

`_flatten_turn_posters` flattens this turn's un-flattened `design` sections (→
finished raster + deliverable chip) and emits `canvas.section_patched` for the
live canvas — idempotently. The real flatten is covered by test_design_flatten;
here we mock it to assert the orchestration logic (which designs get flattened,
the rev-1 idempotency, the emit, and the flag gate).
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents import lead_orchestrator as lo


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


def _flat_res():
    return SimpleNamespace(
        artifact_id="art_flat",
        section={"flattened_source_rev": 1, "flattened_at": "2026-07-01T00:00:00+00:00"},
        rev=2,
        warnings=[],
    )


def _design(**kw):
    base = {"kind": "design", "canvas_id": "cv1", "payload": {}}
    base.update(kw)
    return SimpleNamespace(**base)


async def test_flatten_turn_posters_flattens_unflattened_and_emits(monkeypatch) -> None:
    calls: list = []

    async def _fake_flatten(**kw):
        calls.append(kw)
        return _flat_res()

    monkeypatch.setattr("bowhead_core.design_renderer.flatten_design_section", _fake_flatten)
    monkeypatch.setattr("bowhead_core.design_flags.is_design_v2a_enabled", lambda: True)

    unflat = _design(id="sec_d1", rev=1, payload={"background": {"artifact_id": "art_bg"}})
    # already flattened & FRESH: after a flatten flattened_source_rev == rev-1 → skip
    fresh = _design(
        id="sec_d2",
        rev=3,
        payload={
            "background": {"artifact_id": "art_bg"},
            "flattened_artifact_id": "art_x",
            "flattened_source_rev": 2,
        },
    )
    no_bg = _design(id="sec_d3", rev=1, payload={})  # nothing to render → skip
    non_design = SimpleNamespace(kind="chart", id="sec_c1", canvas_id="cv1", rev=1, payload={})

    bus = _Bus()
    await lo._flatten_turn_posters(
        bus=bus,
        store=object(),
        art_store=object(),
        sections=[unflat, fresh, no_bg, non_design],
        turn_id="t1",
        user_id="u",
        session_id="s",
    )

    # ONLY the un-flattened design with a background is flattened
    assert [c["section_id"] for c in calls] == ["sec_d1"]
    assert calls[0]["created_by_turn"] == "t1"  # deliverable ties to the turn (chip)
    assert calls[0]["expected_rev"] == 1
    patched = [e for e in bus.events if getattr(e, "event_type", None) == "canvas.section_patched"]
    assert len(patched) == 1
    assert patched[0].payload["section_id"] == "sec_d1"
    assert patched[0].payload["patch"]["flattened_artifact_id"] == "art_flat"


async def test_flatten_turn_posters_one_failure_does_not_block_others(monkeypatch) -> None:
    """review P2-a: a flatten failure on one design must NOT abort the loop or
    propagate — the other designs still flatten. (per-item resilience promise)"""
    calls: list = []

    async def _fake_flatten(**kw):
        calls.append(kw["section_id"])
        if kw["section_id"] == "sec_bad":
            raise RuntimeError("render boom")
        return _flat_res()

    monkeypatch.setattr("bowhead_core.design_renderer.flatten_design_section", _fake_flatten)
    monkeypatch.setattr("bowhead_core.design_flags.is_design_v2a_enabled", lambda: True)

    bad = _design(id="sec_bad", rev=1, payload={"background": {"artifact_id": "a"}})
    good = _design(id="sec_good", rev=1, payload={"background": {"artifact_id": "b"}})
    bus = _Bus()
    await lo._flatten_turn_posters(  # must NOT raise
        bus=bus,
        store=object(),
        art_store=object(),
        sections=[bad, good],
        turn_id="t",
        user_id="u",
        session_id="s",
    )
    assert calls == ["sec_bad", "sec_good"]  # both attempted — the failure didn't abort the loop
    patched = [e for e in bus.events if getattr(e, "event_type", None) == "canvas.section_patched"]
    assert [p.payload["section_id"] for p in patched] == ["sec_good"]  # only the good one emitted


async def test_flatten_turn_posters_idempotency_is_rev_minus_one(monkeypatch) -> None:
    """Regression for the off-by-one: flattened_source_rev == rev-1 means fresh.
    Using == rev would (wrongly) re-flatten every already-flattened poster →
    duplicate deliverable artifacts + duplicate chat chips."""
    calls: list = []

    async def _fake_flatten(**kw):
        calls.append(kw)
        return _flat_res()

    monkeypatch.setattr("bowhead_core.design_renderer.flatten_design_section", _fake_flatten)
    monkeypatch.setattr("bowhead_core.design_flags.is_design_v2a_enabled", lambda: True)

    # rev=5, flattened_source_rev=4 == rev-1 → fresh → MUST skip
    already = _design(
        id="s1",
        rev=5,
        payload={
            "background": {"artifact_id": "bg"},
            "flattened_artifact_id": "art_prev",
            "flattened_source_rev": 4,
        },
    )
    # stale flatten (edited since: source_rev far behind) → re-flatten
    stale = _design(
        id="s2",
        rev=9,
        payload={
            "background": {"artifact_id": "bg"},
            "flattened_artifact_id": "art_old",
            "flattened_source_rev": 2,
        },
    )
    await lo._flatten_turn_posters(
        bus=_Bus(),
        store=object(),
        art_store=object(),
        sections=[already, stale],
        turn_id="t",
        user_id="u",
        session_id="s",
    )
    assert [c["section_id"] for c in calls] == ["s2"]  # fresh skipped, stale re-flattened


async def test_flatten_turn_posters_gated_off(monkeypatch) -> None:
    monkeypatch.setattr("bowhead_core.design_flags.is_design_v2a_enabled", lambda: False)
    called: list = []

    async def _fake(**kw):
        called.append(kw)
        return _flat_res()

    monkeypatch.setattr("bowhead_core.design_renderer.flatten_design_section", _fake)
    d = _design(id="s1", rev=1, payload={"background": {"artifact_id": "a"}})
    bus = _Bus()
    await lo._flatten_turn_posters(
        bus=bus,
        store=object(),
        art_store=object(),
        sections=[d],
        turn_id="t",
        user_id="u",
        session_id="s",
    )
    assert called == [] and bus.events == []  # flag off → no-op


async def test_flatten_turn_posters_no_user_id_noop(monkeypatch) -> None:
    monkeypatch.setattr("bowhead_core.design_flags.is_design_v2a_enabled", lambda: True)
    called: list = []

    async def _fake(**kw):
        called.append(kw)
        return _flat_res()

    monkeypatch.setattr("bowhead_core.design_renderer.flatten_design_section", _fake)
    d = _design(id="s1", rev=1, payload={"background": {"artifact_id": "a"}})
    await lo._flatten_turn_posters(
        bus=_Bus(),
        store=object(),
        art_store=object(),
        sections=[d],
        turn_id="t",
        user_id=None,
        session_id="s",
    )
    assert called == []  # no user_id → can't flatten → no-op
