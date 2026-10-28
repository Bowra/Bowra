"""Selected chart scope stays lossless and cannot grant data or root access."""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.selected_context import ChartSelectedContext, render_selected_contexts, resolve_selected_contexts
from bowhead_core.chat_turn_source import ChatTurnUserSource, chat_turn_user_source_projection
from bowhead_core.session_store import SessionStore
from pydantic import ValidationError
from test_chart_evidence_plan import prepared as evidence_prepared
from test_chart_time_window import time_prepared, window_args
from test_document_store import migrated_db as migrated_db


@pytest.fixture
async def prepared(tmp_path, migrated_db):
    with closing(sqlite3.connect(migrated_db)) as original, closing(sqlite3.connect(tmp_path / "canvas.db")) as target:
        original.backup(target)
    return await evidence_prepared.__wrapped__(tmp_path)


async def select_prepared(prepared):
    artifacts, canvas, _, ctx, _, data, _, _ = prepared
    result = await _add_chart(window_args(prepared), ctx)
    assert "error" not in result, result
    root = (await canvas.list_sections(result["canvas_id"]))[0]
    store = SessionStore(artifacts.db_path)
    await store.ensure_session(session_id=ctx.session_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    await store.pin_session_attachments(session_id=ctx.session_id, artifact_ids=[data.id])
    plan = root.payload["spec"]["presentation_plan"]
    bid = plan["view_bindings"]["detail"]
    ref = {
        "fileId": data.id,
        "filename": data.filename,
        "mime": data.mime,
        "size": data.size,
        "referenceKind": "result",
        "selectedContext": {
            "version": 1,
            "sectionId": root.id,
            "sectionRev": root.rev,
            "viewId": "detail",
            "bindingId": bid,
            "datasetDigest": plan["dataset"]["content_digest"],
            "tuples": [["2026-02"]],
            "measureKeys": ["revenue", "cost"],
        },
    }
    return artifacts, store, root, ref, ctx


@pytest.fixture
async def selected(prepared):
    return await select_prepared(await time_prepared(prepared))


async def resolve(selected, ref=None, **scope):
    artifacts, _, _, original, ctx = selected
    return await resolve_selected_contexts(
        artifacts,
        [ref or original],
        user_id=scope.get("user_id", ctx.user_id),
        workspace_id=scope.get("workspace_id", ctx.workspace_id),
        session_id=scope.get("session_id", ctx.session_id),
    )


async def test_selection_derives_missing_and_raw_values_without_narrative(selected):
    facts = (await resolve(selected))[0]
    assert facts["observations"] == [{"month": "2026-02", "revenue": None, "cost": 3}]
    assert facts["selectedRows"] == 1
    assert facts["artifactCoverage"] == {"available": 4, "total": 4}
    assert facts["coverage"] == {"retained_rows": 4}
    text = render_selected_contexts([facts])
    assert "may cover more" in text and "null is missing" in text
    assert "Revenue trend" not in text and "Original observations" not in text


async def test_window_and_selected_point_use_the_same_raw_population(selected):
    ref = copy.deepcopy(selected[3])
    ref["selectedContext"].pop("tuples")
    ref["selectedContext"]["window"] = {"lower": "2026-01", "upper": "2026-03"}
    facts = (await resolve(selected, ref))[0]
    assert facts["selectedRows"] == 3
    assert facts["observations"][0]["revenue"] == 12.12345
    ref["selectedContext"]["tuples"] = [["2026-04"]]
    with pytest.raises(ValueError, match="chart_selection_unavailable"):
        await resolve(selected, ref)
    ref["selectedContext"]["tuples"] = [["2026-01"]]
    assert (await resolve(selected, ref))[0]["observations"] == [{"month": "2026-01", "revenue": -5, "cost": 2}]


@pytest.mark.parametrize(
    "change",
    [
        {"sectionRev": 999},
        {"sectionId": "other"},
        {"datasetDigest": "0" * 64},
        {"viewId": "other"},
        {"bindingId": "other"},
        {"measureKeys": ["invented"]},
        {"tuples": [[2026]]},
        {"tuples": [["2026-02"], ["2026-02"]]},
        {"window": {"lower": "2026-03", "upper": "2026-01"}},
    ],
)
async def test_canonical_scope_rejects_stale_and_invented_selectors(selected, change):
    ref = copy.deepcopy(selected[3])
    ref["selectedContext"].update(change)
    with pytest.raises(ValueError):
        await resolve(selected, ref)


@pytest.mark.parametrize("scope", [{"user_id": "other"}, {"workspace_id": "other"}, {"session_id": "other"}])
async def test_result_membership_does_not_grant_another_canvas_scope(selected, scope):
    with pytest.raises(ValueError):
        await resolve(selected, **scope)


async def test_changed_bytes_and_deleted_artifacts_fail_before_model_material(selected):
    artifacts, _, _, ref, _ = selected
    artifact = await artifacts.get(ref["fileId"])
    Path(artifact.path).write_bytes(b"changed source bytes")
    with pytest.raises(ValueError, match="chart_selection_unavailable"):
        await resolve(selected)
    async with aiosqlite.connect(artifacts.db_path) as connection:
        await connection.execute("UPDATE artifacts SET deleted_at='2026-10-09' WHERE id=?", (ref["fileId"],))
        await connection.commit()
    with pytest.raises(ValueError):
        await resolve(selected)


async def test_immutable_source_retains_selection_and_transaction_rejects_new_stale_turn(selected):
    artifacts, store, root, ref, ctx = selected
    source = ChatTurnUserSource(
        turn_id="t_scope",
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        user_text="Explain this observation",
        attachment_ids=(ref["fileId"],),
        attachment_refs=(ref,),
        parts=None,
    )
    await store.record_turn(turn_id=source.turn_id, session_id=source.session_id, user_text=source.user_text, user_source=source)
    saved = await store.get_chat_turn_user_source(turn_id=source.turn_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
    assert chat_turn_user_source_projection(saved) == chat_turn_user_source_projection(source)
    async with aiosqlite.connect(artifacts.db_path) as connection:
        await connection.execute("UPDATE canvas_sections SET rev=rev+1 WHERE id=?", (root.id,))
        await connection.commit()
    # Exact history retains its original selector; new execution cannot retarget it.
    assert (
        await store.get_chat_turn_user_source(turn_id=source.turn_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
    ).attachment_refs == source.attachment_refs
    from dataclasses import replace

    await store.mark_completed(turn_id=source.turn_id, intent=None, final_answer="Done", tokens_input=0, tokens_output=0, cost_usd=0)
    async with aiosqlite.connect(artifacts.db_path) as connection:
        await connection.execute("UPDATE sessions SET active_turn_id=NULL WHERE id=? AND active_turn_id=?", (source.session_id, source.turn_id))
        await connection.commit()
    stale = replace(source, turn_id="t_stale")
    from bowhead_core.chat_attachment_access import ChatAttachmentAccessError

    with pytest.raises(ChatAttachmentAccessError, match="chart_selection_unavailable"):
        await store.record_turn(turn_id=stale.turn_id, session_id=stale.session_id, user_text=stale.user_text, user_source=stale)
    assert await store.get_turn("t_stale") is None
    with pytest.raises(ValueError):
        await resolve(selected)


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"sectionRev": True},
        {"tuples": [[True]]},
        {"tuples": [[float("nan")]]},
        {"tuples": [[2**53]]},
        {"tuples": [["x" * 2000]]},
        {"tuples": [["a"]] * 21},
        {"measureKeys": ["revenue", "revenue"]},
        {"observations": [{"revenue": 999}]},
    ],
)
def test_closed_selector_rejects_browser_facts_and_unsafe_identity(change):
    wire = {
        "version": 1,
        "sectionId": "sec",
        "sectionRev": 1,
        "viewId": "v",
        "bindingId": "b",
        "datasetDigest": "c" * 64,
        "measureKeys": ["revenue"],
        "tuples": [[None]],
    }
    wire.update(change)
    with pytest.raises(ValidationError):
        ChartSelectedContext.model_validate(wire)


async def test_each_measure_retains_its_own_unit_and_percent_shape(selected):
    artifacts, _, root, ref, _ = selected
    async with aiosqlite.connect(artifacts.db_path) as connection:
        spec = copy.deepcopy(root.payload["spec"])
        for view in spec["views"]:
            if view["id"] == "detail":
                view["spec"]["columns"][2]["unit"] = "%"
                view["spec"]["columns"][2]["percent_scale"] = "points"
                view["spec"]["columns"][2].pop("percent_shape_unknown", None)
        await connection.execute("UPDATE canvas_sections SET payload=? WHERE id=?", (json.dumps({"spec": spec}), root.id))
        await connection.commit()
    facts = (await resolve(selected))[0]
    assert facts["measures"][1] == {"key": "cost", "unit": "%", "percent_scale": "points"}
    assert "total" not in facts


async def test_large_window_supplies_count_and_range_without_sample_or_total(prepared):
    raw = ("month,revenue,cost\n" + "".join(f"2026-01-{day:02d},{day},{day * 2}\n" for day in range(1, 26))).encode()
    selected = await select_prepared(await time_prepared(prepared, raw))
    ref = copy.deepcopy(selected[3])
    ref["selectedContext"].pop("tuples")
    ref["selectedContext"]["window"] = {"lower": "2026-01-01", "upper": "2026-01-25"}
    facts = (await resolve(selected, ref))[0]
    assert facts["selectedRows"] == 25
    assert facts["artifactCoverage"] == {"available": 25, "total": 25}
    assert "observations" not in facts and "total" not in facts


async def test_note_only_loss_keeps_independent_data_and_helper_scope(selected):
    artifacts, _, root, _, ctx = selected
    # Add no permission through narrative: even a private title is omitted.
    async with aiosqlite.connect(artifacts.db_path) as connection:
        payload = copy.deepcopy(root.payload)
        payload["spec"]["title"] = "Private narrative"
        await connection.execute("UPDATE canvas_sections SET payload=? WHERE id=?", (json.dumps(payload), root.id))
        await connection.execute("UPDATE artifacts SET deleted_at='2026-10-09' WHERE filename='definition.txt'")
        await connection.commit()
    facts = await resolve(selected)
    ctx._chart_selected_facts = facts
    from types import SimpleNamespace

    from bowhead_core.chat_reference_runtime import _attachment_context

    block, _ = await _attachment_context(SimpleNamespace(source=SimpleNamespace(attachment_ids=(selected[3]["fileId"],))), ctx)
    assert '"selectedRows": 1' in block and '"revenue": null' in block
    assert "Private narrative" not in block and "Revenue includes returns" not in block


async def test_prepared_frame_followup_uses_exact_current_observations_and_existing_source(prepared):
    from test_chart_prepared_frames import RAW, frame_args

    value = await time_prepared(prepared, RAW)
    artifacts, canvas, _, ctx, _, data, _, _ = value
    result = await _add_chart(frame_args(value), ctx)
    root = (await canvas.list_sections(result["canvas_id"]))[0]
    store = SessionStore(artifacts.db_path)
    await store.ensure_session(session_id=ctx.session_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    await store.pin_session_attachments(session_id=ctx.session_id, artifact_ids=[data.id])
    plan = root.payload["spec"]["presentation_plan"]
    ref = {
        "fileId": data.id,
        "filename": data.filename,
        "mime": data.mime,
        "size": data.size,
        "referenceKind": "result",
        "selectedContext": {
            "version": 1,
            "sectionId": root.id,
            "sectionRev": root.rev,
            "viewId": "relationship",
            "bindingId": plan["view_bindings"]["relationship"],
            "datasetDigest": plan["dataset"]["content_digest"],
            "frameKey": "2026-01",
            "measureKeys": ["revenue", "cost"],
        },
    }
    selected = (artifacts, store, root, ref, ctx)
    facts = (await resolve(selected))[0]
    assert facts["observationKeys"] == ["month", "region"]
    assert facts["selectedRows"] == 2
    assert facts["observations"] == [
        {"month": "2026-01", "region": "B", "revenue": -5, "cost": 2},
        {"month": "2026-01", "region": "A", "revenue": None, "cost": 3},
    ]
    ref["selectedContext"]["frameKey"] = "2026-02"
    assert (await resolve(selected))[0]["observations"][0]["revenue"] == 0.000012345
    ref["selectedContext"].update(frameKey="2026-03", viewId="detail")
    assert (await resolve(selected))[0]["observations"] == [{"month": "2026-03", "region": "A", "revenue": 12.123456, "cost": 5}]
    for patch in (
        {"frameKey": "2026-04"},
        {"frameKey": True},
        {"frameKey": 2026},
        {"frameKey": None},
        {"tuples": [["2026-03", "A"]]},
        {"window": {"lower": "2026-01", "upper": "2026-02"}},
        {"sectionRev": 99},
    ):
        forged = copy.deepcopy(ref)
        forged["selectedContext"].update(patch)
        with pytest.raises(ValueError):
            await resolve(selected, forged)
    source = ChatTurnUserSource(
        turn_id="t_frame",
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        user_text="Explain this frame",
        attachment_ids=(data.id,),
        attachment_refs=(ref,),
        parts=None,
    )
    await store.record_turn(turn_id=source.turn_id, session_id=source.session_id, user_text=source.user_text, user_source=source)
    saved = await store.get_chat_turn_user_source(turn_id=source.turn_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
    assert chat_turn_user_source_projection(saved) == chat_turn_user_source_projection(source)
    Path(data.path).write_bytes(b"changed observations")
    with pytest.raises(ValueError, match="chart_selection_unavailable"):
        await resolve(selected)
