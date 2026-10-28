from __future__ import annotations

import base64
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.results_report_relations import project_report_relations
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_gateway.results_contracts import ResultsFiltersV1, ResultsQueryRequestV1
from bowhead_gateway.results_cursor import ResultsCursorCodec
from bowhead_gateway.results_query_service import (
    ResultsQueryError,
    query_owner_results,
    query_report_results,
)
from cryptography.fernet import Fernet

from bowhead_core import CanvasStore, SessionStore


async def _stores(tmp_path: Path):  # type: ignore[no-untyped-def]
    db_path = str(tmp_path / "bowhead.db")
    session_store = SessionStore(db_path)
    canvas_store = CanvasStore(db_path)
    artifact_store = ArtifactStore(
        db_path=db_path,
        backend=LocalBackend(root_dir=tmp_path / "files"),
    )
    await session_store.initialize()
    await TurnLogStore(db_path=db_path).initialize()
    await canvas_store.initialize()
    await artifact_store.initialize()
    session = await session_store.ensure_session(
        session_id="session_results",
        workspace_id="workspace_results",
        user_id="user_results",
    )
    canvas = await canvas_store.ensure_canvas(
        session_id=session.id,
        workspace_id=session.workspace_id,
        user_id=session.user_id,
        title="季度经营报告",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={"text": "季度经营报告"},
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="code_block",
        payload={"lang": "python", "code": "print('ok')"},
    )
    await artifact_store.put_bytes(
        content=b"deck",
        filename="交付报告.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source="generated",
        created_by=session.user_id,
        session_id=session.id,
        kind="deliverable",
    )
    data = await artifact_store.put_bytes(
        content=b"a,b\n1,2\n",
        filename="季度数据.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by=session.user_id,
        session_id=session.id,
        kind="tool_data",
    )
    upload = await artifact_store.put_bytes(
        content=b"input",
        filename="输入材料.txt",
        mime="text/plain",
        source="upload",
        created_by=session.user_id,
        session_id=session.id,
        kind="user_upload",
    )
    return session_store, canvas_store, artifact_store, session, data, upload


def _codec() -> ResultsCursorCodec:
    return ResultsCursorCodec(base64.urlsafe_b64decode(Fernet.generate_key()))


async def _query(tmp_path: Path, body: ResultsQueryRequestV1):  # type: ignore[no-untyped-def]
    session_store, canvas_store, artifact_store, session, data, upload = await _stores(tmp_path)
    response = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=body,
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )
    return response, (session_store, canvas_store, artifact_store, session, data, upload)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_query_is_complete_and_uses_unified_cursor(tmp_path: Path) -> None:
    session_store, canvas_store, artifact_store, session, _, _ = await _stores(tmp_path)
    codec = _codec()
    first = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer", page_size=2),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    second = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            page_size=2,
            snapshot_token=first.snapshot_token,
            cursor=first.next_cursor,
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    assert second.previous_cursor is not None
    previous = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            page_size=2,
            snapshot_token=first.snapshot_token,
            cursor=second.previous_cursor,
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )

    assert first.authorized_total == 5
    assert first.matched_total == 5
    assert first.group_counts.model_dump() == {
        "deliverables": 2,
        "evidence": 3,
        "data": 1,
        "code": 1,
        "input": 1,
    }
    assert [item.object_type for item in first.items] == ["report", "artifact"]
    assert [item.position for item in first.items] == [1, 2]
    assert first.next_cursor is not None
    assert [item.position for item in second.items] == [3, 4]
    assert [item.key for item in previous.items] == [item.key for item in first.items]
    assert len({item.key for item in [*first.items, *second.items]}) == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_query_does_not_invent_report_from_code_only_canvas(tmp_path: Path) -> None:
    session_store, canvas_store, artifact_store, session, _, _ = await _stores(tmp_path)
    async with aiosqlite.connect(session_store.db_path) as db:
        await db.execute(
            "UPDATE canvas_sections SET hidden_for_chat_mode=1 WHERE kind='h1'",
        )
        await db.commit()
    response = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )

    assert "report" not in {item.object_type for item in response.items}
    assert "code" in {item.object_type for item in response.items}
    assert response.authorized_total == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_query_keeps_report_for_data_table_only_canvas(
    tmp_path: Path,
) -> None:
    session_store, canvas_store, artifact_store, session, data, _ = await _stores(tmp_path)
    async with aiosqlite.connect(session_store.db_path) as db:
        await db.execute(
            "UPDATE canvas_sections SET hidden_for_chat_mode=1",
        )
        await db.commit()
    canvas = await canvas_store.get_canvas_by_session(session.id)
    assert canvas is not None
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="data_table",
        payload={"meta": {"artifact_id": data.id}},
    )

    response = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )

    assert response.items[0].object_type == "report"
    assert response.authorized_total == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_query_search_filters_and_locates_deep_item(tmp_path: Path) -> None:
    session_store, canvas_store, artifact_store, session, _, upload = await _stores(tmp_path)
    codec = _codec()
    search = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            query="数据",
            filters=ResultsFiltersV1(primary="evidence", secondary="data"),
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    located = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="locate",
            anchor_key=f"artifact:{upload.id}",
            page_size=3,
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )

    assert [item.title for item in search.items] == ["季度数据.csv"]
    assert search.group_counts.model_dump() == {
        "deliverables": 0,
        "evidence": 1,
        "data": 1,
        "code": 0,
        "input": 0,
    }
    assert f"artifact:{upload.id}" in {item.key for item in located.items}
    assert located.page_start > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_query_supports_report_relation_filter_and_rejects_stale_projection(
    tmp_path: Path,
) -> None:
    session_store, canvas_store, artifact_store, session, _, _ = await _stores(tmp_path)
    codec = _codec()
    report_lens = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            filters=ResultsFiltersV1(lens="current_report"),
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    first = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    canvas = await canvas_store.get_canvas_by_session(session.id)
    assert canvas is not None
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="markdown",
        payload={"text": "new relation revision"},
    )
    with pytest.raises(ResultsQueryError) as stale_error:
        await query_owner_results(
            session=session,
            team_id="team_results",
            request_body=ResultsQueryRequestV1(
                surface="explorer",
                snapshot_token=first.snapshot_token,
            ),
            scale_nav=True,
            session_store=session_store,
            canvas_store=canvas_store,
            artifact_store=artifact_store,
            cursor_codec=codec,
        )

    assert report_lens.items == []
    assert report_lens.group_counts.model_dump() == {
        "deliverables": 0,
        "evidence": 0,
        "data": 0,
        "code": 0,
        "input": 0,
    }
    assert report_lens.projection_status.report_relations == "ready"
    assert stale_error.value.code == "results_snapshot_stale"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_changes_reuses_snapshot_binding_and_counts_only_new_artifacts(tmp_path: Path) -> None:
    session_store, canvas_store, artifact_store, session, _, _ = await _stores(tmp_path)
    codec = _codec()
    first = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    await artifact_store.put_bytes(
        content=b"new",
        filename="new.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by=session.user_id,
        session_id=session.id,
        kind="tool_data",
    )
    changes = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="changes",
            snapshot_token=first.snapshot_token,
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )

    assert changes.items == []
    assert changes.new_results.count == 1
    assert changes.new_results.projection_changed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_allows_deletion_but_stales_on_restore_of_preexisting_trash(
    tmp_path: Path,
) -> None:
    session_store, canvas_store, artifact_store, session, data, upload = await _stores(tmp_path)
    codec = _codec()
    assert await artifact_store.soft_delete(data.id, user_id=session.user_id)
    first = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    assert await artifact_store.soft_delete(upload.id, user_id=session.user_id)
    after_delete = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            snapshot_token=first.snapshot_token,
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )

    assert after_delete.authorized_total == first.authorized_total - 1
    assert await artifact_store.restore(data.id, user_id=session.user_id)
    with pytest.raises(ResultsQueryError) as stale_error:
        await query_owner_results(
            session=session,
            team_id="team_results",
            request_body=ResultsQueryRequestV1(
                surface="explorer",
                snapshot_token=first.snapshot_token,
            ),
            scale_nav=True,
            session_store=session_store,
            canvas_store=canvas_store,
            artifact_store=artifact_store,
            cursor_codec=codec,
        )

    assert stale_error.value.code == "results_snapshot_stale"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_quick_reauthorizes_current_and_recent_keys(tmp_path: Path) -> None:
    session_store, canvas_store, artifact_store, session, data, upload = await _stores(tmp_path)
    response = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="quick",
            current_key=f"artifact:{upload.id}",
            recent_keys=[f"artifact:{data.id}", "artifact:not_authorized"],
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )

    assert [item.object_type for item in response.items[:3]] == [
        "report",
        "artifact",
        "artifact",
    ]
    assert response.items[1].key == f"artifact:{upload.id}"
    assert response.items[2].key == f"artifact:{data.id}"
    assert "artifact:not_authorized" not in {item.key for item in response.items}
    assert response.next_cursor is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_quick_fetches_deep_direct_relation_and_search_tiebreaker(
    tmp_path: Path,
) -> None:
    (
        session_store,
        canvas_store,
        artifact_store,
        session,
        report_data,
        _,
    ) = await _stores(tmp_path)
    canvas = await canvas_store.get_canvas_by_session(session.id)
    assert canvas is not None
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"data_artifact_id": report_data.id}},
    )
    for index in range(16):
        await artifact_store.put_bytes(
            content=b"deliverable",
            filename=f"delivery-{index:02}.pdf",
            mime="application/pdf",
            source="generated",
            created_by=session.user_id,
            session_id=session.id,
            kind="deliverable",
        )
    ordinary_data = []
    for index in range(16):
        ordinary_data.append(
            await artifact_store.put_bytes(
                content=b"data",
                filename=f"ordinary-{index:02}.csv",
                mime="text/csv",
                source="bi_fetch_rows",
                created_by=session.user_id,
                session_id=session.id,
                kind="tool_data",
            ),
        )

    quick = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="quick"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )
    searched = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="quick",
            query="数据",
            current_key=f"artifact:{ordinary_data[0].id}",
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )
    relation_search = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            query="图表数据",
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )

    assert quick.items[0].object_type == "report"
    assert quick.items[1].key == f"artifact:{report_data.id}"
    assert quick.items[1].relation_roles == ["report_chart_data"]
    assert searched.items[0].key == f"artifact:{report_data.id}"
    assert searched.items[1].key == f"artifact:{ordinary_data[0].id}"
    assert [item.key for item in relation_search.items] == [
        f"artifact:{report_data.id}",
    ]
    assert quick.page_start == searched.page_start == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_read_only_projection_removes_write_capabilities(tmp_path: Path) -> None:
    session_store, canvas_store, artifact_store, session, _, _ = await _stores(tmp_path)
    response = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
        writable=False,
    )

    report = next(item for item in response.items if item.object_type == "report")
    artifacts = [item for item in response.items if item.object_type == "artifact"]
    code = next(item for item in response.items if item.object_type == "code")
    assert "read_report" in report.capabilities
    assert "edit_report" not in report.capabilities
    assert "export_report" not in report.capabilities
    assert all("reference_in_chat" not in item.capabilities for item in artifacts)
    assert "reference_in_chat" not in code.capabilities


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_quick_and_locate_never_resolve_pending_artifact(tmp_path: Path) -> None:
    session_store, canvas_store, artifact_store, session, data, _ = await _stores(
        tmp_path,
    )
    async with aiosqlite.connect(session_store.db_path) as db:
        await db.execute(
            "UPDATE artifacts SET status='pending_upload' WHERE id=?",
            (data.id,),
        )
        await db.commit()

    quick = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="quick",
            current_key=f"artifact:{data.id}",
        ),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )
    with pytest.raises(ResultsQueryError) as locate_error:
        await query_owner_results(
            session=session,
            team_id="team_results",
            request_body=ResultsQueryRequestV1(
                surface="locate",
                anchor_key=f"artifact:{data.id}",
            ),
            scale_nav=True,
            session_store=session_store,
            canvas_store=canvas_store,
            artifact_store=artifact_store,
            cursor_codec=_codec(),
        )

    assert f"artifact:{data.id}" not in {item.key for item in quick.items}
    assert locate_error.value.code == "results_anchor_not_in_view"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_running_uses_latest_code_event_of_active_turn(tmp_path: Path) -> None:
    session_store, canvas_store, artifact_store, session, _, _ = await _stores(tmp_path)
    turn_log_store = TurnLogStore(db_path=session_store.db_path)
    turn = await session_store.record_turn(
        turn_id="turn_running_code",
        session_id=session.id,
        user_text="run code",
    )
    await turn_log_store.record(
        turn_id=turn.id,
        event_type="tool_call",
        payload={"tool_name": "python_sandbox", "call_id": "call_1"},
    )
    running = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )
    assert next(item for item in running.items if item.object_type == "code").status == "running"

    await turn_log_store.record(
        turn_id=turn.id,
        event_type="tool_result",
        payload={"tool_name": "python_sandbox", "call_id": "call_1", "ok": True},
    )
    settled = await query_owner_results(
        session=session,
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        session_store=session_store,
        canvas_store=canvas_store,
        artifact_store=artifact_store,
        cursor_codec=_codec(),
    )
    assert next(item for item in settled.items if item.object_type == "code").status == "ready"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_query_is_closure_first_and_projects_direct_roles(
    tmp_path: Path,
) -> None:
    session_store, canvas_store, artifact_store, session, data, upload = await _stores(
        tmp_path,
    )
    canvas = await canvas_store.get_canvas_by_session(session.id)
    assert canvas is not None
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"data_artifact_id": data.id}},
    )
    state = await canvas_store.read_canvas_projection_state(canvas.id)
    assert state is not None
    canvas, sections = state
    relations = project_report_relations(sections)
    codec = _codec()

    response = await query_report_results(
        canvas=canvas,
        canvas_store=canvas_store,
        sections=sections,
        relations=relations,
        viewer_id="shared_viewer",
        team_id="team_results",
        request_body=ResultsQueryRequestV1(surface="explorer"),
        scale_nav=True,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    report_lens = await query_report_results(
        canvas=canvas,
        canvas_store=canvas_store,
        sections=sections,
        relations=relations,
        viewer_id="shared_viewer",
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            filters=ResultsFiltersV1(lens="current_report"),
        ),
        scale_nav=True,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    outside_search = await query_report_results(
        canvas=canvas,
        canvas_store=canvas_store,
        sections=sections,
        relations=relations,
        viewer_id="shared_viewer",
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            query=upload.filename,
        ),
        scale_nav=True,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )
    relation_search = await query_report_results(
        canvas=canvas,
        canvas_store=canvas_store,
        sections=sections,
        relations=relations,
        viewer_id="shared_viewer",
        team_id="team_results",
        request_body=ResultsQueryRequestV1(
            surface="explorer",
            query="图表数据",
        ),
        scale_nav=True,
        artifact_store=artifact_store,
        cursor_codec=codec,
    )

    assert response.authorized_total == 2
    assert {item.object_type for item in response.items} == {"report", "artifact"}
    artifact = next(item for item in response.items if item.object_type == "artifact")
    assert artifact.key == f"artifact:{data.id}"
    assert artifact.relation_roles == ["report_chart_data"]
    assert artifact.artifact_metadata is not None
    assert artifact.artifact_metadata.mime == data.mime
    assert artifact.artifact_metadata.size == data.size
    assert artifact.artifact_metadata.origin == "query"
    assert {
        "derived_from",
        "manifest_artifact_id",
        "created_by_turn",
        "path",
    }.isdisjoint(artifact.artifact_metadata.model_dump())
    assert "reference_in_chat" not in artifact.capabilities
    assert "manage_in_files" not in artifact.capabilities
    assert [item.key for item in report_lens.items] == [f"artifact:{data.id}"]
    assert outside_search.items == []
    assert outside_search.matched_total == 0
    assert [item.key for item in relation_search.items] == [
        f"artifact:{data.id}",
    ]
