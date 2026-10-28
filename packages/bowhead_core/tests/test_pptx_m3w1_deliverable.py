"""Sprint pptx-from-template M3w.1 — deliverable-payload mode + keep-signal.

The runtime `subagent_return` gate is the hand-rolled `validate_payload`
(`analysis_payload.py`) + the default-ON `auto_fix_payload` soft gate — NOT the
manifest YAML `output_schema` (that is only validated at manifest load time). A
deck/render leaf returns a *deliverable* payload (a downloadable .pptx, no
insights), so both validators grew a `deliverables`-mode branch (RD-4). Plus the
`is_report_worthy` deliverable keep-signal (RD-7/CR-6) and the artifact-store
`turn_has_deliverable` query that feeds it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bowhead_core.analysis_payload import is_deliverable_payload, validate_payload
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.payload_auto_fix import auto_fix_payload

# ── is_deliverable_payload helper ────────────────────────────────────────────


def test_is_deliverable_payload_basics() -> None:
    assert is_deliverable_payload({"title": "t", "deliverables": [{"kind": "pptx"}]})
    assert not is_deliverable_payload({"title": "t"})
    assert not is_deliverable_payload({"title": "t", "deliverables": []})
    assert not is_deliverable_payload({"deliverables": "nope"})
    # bail envelope takes priority — NOT a deliverable even if deliverables present
    assert not is_deliverable_payload({"error": {"kind": "tool_failure"}, "deliverables": [{"kind": "pptx"}]})
    assert not is_deliverable_payload("not a dict")  # type: ignore[arg-type]


# ── validate_payload deliverable mode ────────────────────────────────────────


def _deck(**over):
    p = {
        "title": "业务规划复盘",
        "deliverables": [{"kind": "pptx", "artifact_id": "art_deck123", "slide_count": 8}],
    }
    p.update(over)
    return p


def test_validate_deliverable_ok() -> None:
    ok, errs = validate_payload(_deck())
    assert ok, errs
    assert errs == []


def test_validate_deliverable_no_insights_required() -> None:
    """The whole point: a deck payload with NO insights/exec_summary/kpis/
    recommendations validates (would be rejected by the analytical path)."""
    p = _deck()
    assert "insights" not in p and "exec_summary" not in p
    ok, _ = validate_payload(p)
    assert ok


def test_validate_deliverable_missing_artifact_id_rejects() -> None:
    ok, errs = validate_payload(_deck(deliverables=[{"kind": "pptx"}]))
    assert not ok
    assert any("artifact_id" in e for e in errs)


def test_validate_deliverable_bad_kind_rejects() -> None:
    ok, errs = validate_payload(_deck(deliverables=[{"kind": "exe", "artifact_id": "art_x"}]))
    assert not ok
    assert any("kind" in e for e in errs)


def test_validate_deliverable_missing_kind_explicit_error() -> None:
    """Missing kind is distinguished from invalid kind (mirrors artifact_id)."""
    ok, errs = validate_payload(_deck(deliverables=[{"artifact_id": "art_x"}]))
    assert not ok
    assert any("kind missing" in e for e in errs)


def test_validate_deliverable_with_insights_is_deliverable_mode() -> None:
    """INTENTIONAL deck-only semantics: a payload carrying BOTH deliverables and
    insights validates as a deliverable; the (here malformed) insights are
    ignored, not validated. Documents the early-return contract so a future dev
    doesn't read the bypass as a bug."""
    p = _deck(insights=[{"garbage": True}])  # malformed insights, but ignored
    ok, errs = validate_payload(p)
    assert ok, errs


def test_validate_deliverable_missing_title_rejects() -> None:
    p = _deck()
    del p["title"]
    ok, errs = validate_payload(p)
    assert not ok
    assert any("title" in e for e in errs)


def test_validate_bail_still_priority_over_deliverable() -> None:
    """An error envelope is validated as a bail (not a deliverable) even if a
    deliverables list is also present — bail branch is checked first."""
    ok, errs = validate_payload({"error": {"kind": "tool_failure", "message": "boom"}, "deliverables": [{"kind": "pptx"}]})
    assert ok, errs  # valid bail envelope


def test_validate_normal_analytical_payload_unaffected() -> None:
    """Regression: a non-deliverable payload still requires insights — the
    deliverable branch must not weaken the analytical gate."""
    ok, errs = validate_payload({"title": "x"})  # no insights, no deliverables
    assert not ok
    assert any("insight" in e.lower() or "exec_summary" in e for e in errs)


# ── auto_fix_payload deliverable short-circuit ───────────────────────────────


def test_auto_fix_deliverable_no_hard_reject() -> None:
    """A deck payload (no insights) must NOT trip auto_fix's R7 dual-empty
    hard-reject — it short-circuits before the insight pipeline."""
    res = auto_fix_payload(_deck())
    assert res.hard_reject is False
    assert res.payload.get("deliverables")


def test_auto_fix_normal_empty_insights_still_hard_rejects() -> None:
    """Regression: a non-deliverable payload with no insights still R7-rejects."""
    res = auto_fix_payload({"title": "x", "kpis": []})
    assert res.hard_reject is True


# ── artifact_store.turn_has_deliverable (feeds is_report_worthy) ─────────────


@pytest.fixture
async def store() -> ArtifactStore:
    tmp = Path(tempfile.mkdtemp())
    backend = LocalBackend(root_dir=tmp / "files")
    s = ArtifactStore(backend=backend, db_path=tmp / "test.db")
    await s.initialize()
    return s


async def _put(store: ArtifactStore, **kw):
    base = {"content": b"x", "filename": "f.pptx", "mime": "application/octet-stream", "created_by": None}
    base.update(kw)
    return await store.put_bytes(**base)


async def test_turn_has_deliverable_true(store: ArtifactStore) -> None:
    await _put(store, source="python_sandbox", kind="deliverable", created_by_turn="t_1")
    assert await store.turn_has_deliverable("t_1") is True


async def test_turn_has_deliverable_deck_preview_true(store: ArtifactStore) -> None:
    await _put(store, source="sandbox_output", kind="deck_preview", created_by_turn="t_2")
    assert await store.turn_has_deliverable("t_2") is True


async def test_turn_has_deliverable_non_deliverable_false(store: ArtifactStore) -> None:
    await _put(store, source="bi_fetch_rows", kind="tool_data", created_by_turn="t_3")
    assert await store.turn_has_deliverable("t_3") is False


async def test_turn_has_deliverable_other_turn_false(store: ArtifactStore) -> None:
    await _put(store, source="python_sandbox", kind="deliverable", created_by_turn="t_4")
    assert await store.turn_has_deliverable("t_other") is False


async def test_turn_has_deliverable_empty_turn_id_false(store: ArtifactStore) -> None:
    assert await store.turn_has_deliverable("") is False


async def test_turn_has_deliverable_deleted_excluded(store: ArtifactStore) -> None:
    art = await _put(store, source="python_sandbox", kind="deliverable", created_by="u_1", created_by_turn="t_5")
    assert await store.turn_has_deliverable("t_5") is True
    await store.soft_delete(art.id, user_id="u_1")
    assert await store.turn_has_deliverable("t_5") is False


# ── artifact_store.list_deliverables_for_turn (feeds P5a deck chip) ──────────
# Sibling of turn_has_deliverable: same WHERE clause, returns the RECORDS so the
# lead-final collector can build a download chip for a deck deliverable.


async def test_list_deliverables_for_turn_returns_deliverable_and_preview(
    store: ArtifactStore,
) -> None:
    d = await _put(store, source="consultant_deck", kind="deliverable", created_by="u_1", created_by_turn="t_d1")
    p = await _put(store, source="sandbox_output", kind="deck_preview", created_by="u_1", created_by_turn="t_d1")
    await _put(store, source="bi_fetch_rows", kind="tool_data", created_by="u_1", created_by_turn="t_d1")  # not a deliverable
    out = await store.list_deliverables_for_turn("t_d1")
    ids = {a.id for a in out}
    assert ids == {d.id, p.id}  # tool_data excluded; same set as turn_has_deliverable


async def test_list_deliverables_for_turn_other_turn_empty(
    store: ArtifactStore,
) -> None:
    await _put(store, source="consultant_deck", kind="deliverable", created_by="u_1", created_by_turn="t_d2")
    assert await store.list_deliverables_for_turn("t_other") == []


async def test_list_deliverables_for_turn_empty_turn_id(store: ArtifactStore) -> None:
    assert await store.list_deliverables_for_turn("") == []


async def test_list_deliverables_for_turn_deleted_excluded(
    store: ArtifactStore,
) -> None:
    art = await _put(store, source="consultant_deck", kind="deliverable", created_by="u_1", created_by_turn="t_d3")
    assert len(await store.list_deliverables_for_turn("t_d3")) == 1
    await store.soft_delete(art.id, user_id="u_1")
    assert await store.list_deliverables_for_turn("t_d3") == []


async def test_list_result_artifacts_for_turn_includes_stable_outputs_only(
    store: ArtifactStore,
) -> None:
    manifest = await _put(
        store,
        filename="manifest.json",
        source="canvas_export",
        kind="deliverable",
        producer_kind="canvas_export_manifest_b1a_v1",
        created_by="u_1",
        created_by_turn="t_results",
    )
    exported = await _put(
        store,
        filename="report.docx",
        source="canvas_export",
        kind="deliverable",
        producer_kind="canvas_export_skill",
        manifest_artifact_id=manifest.id,
        created_by="u_1",
        created_by_turn="t_results",
    )
    data = await _put(
        store,
        filename="analysis.csv",
        source="python_sandbox",
        kind="tool_data",
        created_by="u_1",
        created_by_turn="t_results",
    )
    legacy = await _put(
        store,
        filename="legacy.json",
        source="python_sandbox",
        kind=None,
        created_by="u_1",
        created_by_turn="t_results",
    )
    await _put(
        store,
        filename="input.csv",
        source="upload",
        kind="user_upload",
        created_by="u_1",
        created_by_turn="t_results",
    )

    rows = await store.list_result_artifacts_for_turn("t_results")

    assert {artifact.id for artifact in rows} == {exported.id, data.id, legacy.id}
    assert await store.list_result_artifacts_for_turn("") == []
