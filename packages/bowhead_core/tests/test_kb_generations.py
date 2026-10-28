"""Real SQLite/Chroma publication and source-use boundaries; only model I/O is stubbed."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.artifact_store import LocalBackend
from bowhead_core.kb.eligibility import KbUseEligibility
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.generations import KbGenerationStore
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.store import KbStore
from bowhead_core.kb.worker import KbWorker

pytestmark = pytest.mark.integration


@pytest.fixture
async def source(tmp_path, initialize_migrated_kb_files_database, monkeypatch):
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    monkeypatch.setenv("FEATURE_KB_USE_GUARD", "1")
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files = KbFilesStore(tmp_path / "kb_files.db")
    await files.initialize()
    path = tmp_path / "original.md"
    path.write_text("# Source\n\nRevenue is 123 units in 2026.\n")
    fid = await files.create(
        artifact_id=str(path),
        filename="source.md",
        mime_type="text/markdown",
        size_bytes=path.stat().st_size,
        scope="space",
        scope_id="ws_test",
        workspace_id="ws_test",
        team_id="team_test",
        uploaded_by="u_test",
    )
    index = KbStore(str(tmp_path / "chroma"))
    embedder = SimpleNamespace(embed_batch=AsyncMock(side_effect=lambda texts: [[0.1, 0.2, 0.3] for _ in texts]))
    worker = KbWorker(files_store=files, store=index, embedder=embedder)
    await worker.parse_and_index(fid)
    artifacts = SimpleNamespace(
        backend=LocalBackend(tmp_path),
        get=AsyncMock(return_value=SimpleNamespace(path=str(path), status="ready")),
        list_recent_ready_paths=AsyncMock(return_value=[("a", str(path))]),
    )
    return SimpleNamespace(
        files=files,
        index=index,
        path=path,
        fid=fid,
        worker=worker,
        artifacts=artifacts,
        embedder=embedder,
        generations=KbGenerationStore(files._db_path),
        scope=ScopeContext(user_id="u_test", workspace_id="ws_test", team_id="team_test", role="user"),
    )


async def test_publication_keeps_old_citations_and_failed_reparse_keeps_current(source):
    old = await source.files.get(source.fid)
    assert old.parse_status == "done" and old.active_generation_id
    record = await source.generations.get(old.active_generation_id)
    assert record["state"] == "published"
    assert record["source_sha256"] == hashlib.sha256(source.path.read_bytes()).hexdigest()
    old_chunks = await source.index.list_active_chunks(old)
    await source.worker.parse_and_index(source.fid, allow_terminal=True)
    new = await source.files.get(source.fid)
    new_chunks = await source.index.list_active_chunks(new)
    assert new.active_generation_id != old.active_generation_id
    assert {c[0] for c in new_chunks}.isdisjoint(c[0] for c in old_chunks)
    assert (await source.index.list_active_chunks(new, chunk_id=old_chunks[0][0]))[0][1] == old_chunks[0][1]
    source.embedder.embed_batch.side_effect = RuntimeError("model offline")
    await source.worker.parse_and_index(source.fid, allow_terminal=True)
    failed = await source.files.get(source.fid)
    assert failed.parse_status == "failed" and failed.active_generation_id == new.active_generation_id
    assert await source.index.verify_generation(failed, new.active_generation_id, len(new_chunks))


async def test_first_binding_same_bytes_recovery_and_changed_bytes(source, monkeypatch):
    check = KbUseEligibility(source.files, source.artifacts, source.index)
    authorize = AsyncMock(return_value=True)
    assert (await check.check(source.fid, source.scope, authorize)).state == "eligible"
    file = await source.files.get(source.fid)
    assert file.source_binding_json
    original = source.path.read_bytes()
    source.path.write_bytes(b"different bytes")
    assert (await check.check(source.fid, source.scope, authorize)).reason == "source_changed"
    assert (await source.files.get(source.fid)).source_absent
    source.path.write_bytes(original)
    assert (await check.check(source.fid, source.scope, authorize)).allowed
    assert not (await source.files.get(source.fid)).source_absent
    assert source.embedder.embed_batch.await_count == 1  # Recovery never invokes a model.


async def test_files_index_source_snapshot_rejects_missing_components(source, tmp_path):
    file = await source.files.get(source.fid)
    assert file.active_generation_id
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    with sqlite3.connect(source.files._db_path) as live, sqlite3.connect(snapshot / "kb_files.db") as saved:
        live.backup(saved)
    shutil.copytree(tmp_path / "chroma", snapshot / "chroma")
    shutil.copyfile(source.path, snapshot / "original.md")

    restored_files = KbFilesStore(snapshot / "kb_files.db")
    await restored_files.initialize()
    restored_index = KbStore(str(snapshot / "chroma"))
    restored_artifacts = SimpleNamespace(
        backend=LocalBackend(snapshot),
        get=AsyncMock(return_value=SimpleNamespace(path=str(snapshot / "original.md"), status="ready")),
        list_recent_ready_paths=AsyncMock(return_value=[("sample", str(snapshot / "original.md"))]),
    )
    authorize = AsyncMock(return_value=True)
    complete = KbUseEligibility(restored_files, restored_artifacts, restored_index)
    assert (await complete.check(source.fid, source.scope, authorize)).allowed

    index_missing = KbUseEligibility(restored_files, restored_artifacts, KbStore(str(tmp_path / "empty_chroma")))
    assert (await index_missing.check(source.fid, source.scope, authorize)).reason == "index_unavailable"
    metadata_missing = KbUseEligibility(
        restored_files,
        SimpleNamespace(backend=LocalBackend(snapshot), get=AsyncMock(return_value=None)),
        restored_index,
    )
    assert (await metadata_missing.check(source.fid, source.scope, authorize)).reason == "source_unavailable"
    (snapshot / "original.md").unlink()
    assert not (await complete.check(source.fid, source.scope, authorize)).allowed


async def test_temporary_outage_requires_prior_binding_and_no_confirmed_loss(source, monkeypatch):
    checker = KbUseEligibility(source.files, source.artifacts, source.index)
    authorize = AsyncMock(return_value=True)
    original_head = source.artifacts.backend.head
    monkeypatch.setattr(source.artifacts.backend, "head", AsyncMock(side_effect=TimeoutError))
    assert not (await checker.check(source.fid, source.scope, authorize)).allowed
    monkeypatch.setattr(source.artifacts.backend, "head", original_head)
    assert (await checker.check(source.fid, source.scope, authorize)).allowed
    monkeypatch.setattr(source.artifacts.backend, "head", AsyncMock(side_effect=TimeoutError))
    assert (await checker.check(source.fid, source.scope, authorize)).state == "warning"
    await source.generations.mark_absent(await source.files.get(source.fid))
    assert not (await checker.check(source.fid, source.scope, authorize)).allowed


async def test_authorization_precedes_io_and_is_rechecked_after_io(source):
    checker = KbUseEligibility(source.files, source.artifacts, source.index)
    denied = AsyncMock(return_value=False)
    assert not (await checker.check(source.fid, source.scope, denied)).allowed
    source.artifacts.get.assert_not_awaited()
    late_denied = AsyncMock(side_effect=[True, False])
    assert not (await checker.check(source.fid, source.scope, late_denied)).allowed
    other_team = ScopeContext(user_id="u_test", workspace_id="ws_test", team_id="other", role="user")
    source.artifacts.get.reset_mock()
    assert not (await checker.check(source.fid, other_team, AsyncMock(return_value=True))).allowed
    source.artifacts.get.assert_not_awaited()


async def test_publication_cas_rejects_requeued_worker_and_incomplete_index(source):
    file = await source.files.get(source.fid)
    gid = await source.generations.begin(file, "a" * 64, 2, "")
    assert not await source.index.verify_generation(file, gid, 2)
    assert await source.files.requeue_for_reparse(source.fid, include_done=True)
    assert not await source.generations.publish(file, gid, layer="L1", duration_ms=1, summary_json=None)
    assert (await source.files.get(source.fid)).active_generation_id == file.active_generation_id


async def test_embedding_reindex_copies_published_ids_without_reparsing(source, tmp_path):
    file = await source.files.get(source.fid)
    target = KbStore(str(tmp_path / "chroma"), index_version="next")
    worker = KbWorker(files_store=source.files, store=target, embedder=source.embedder)
    source.path.unlink()  # Model migration is about published chunks, not parsing unavailable bytes.
    assert await worker.reindex_file_chunks(file.file_id, file.version, source_store=source.index)
    assert await target.list_active_chunks(file) == await source.index.list_active_chunks(file)
    assert (await source.files.get(file.file_id)).version == file.version


@pytest.mark.parametrize(
    ("locale", "warning", "paused"),
    [
        ("en-US", "Some originals are temporarily unavailable.", "Knowledge sources changed"),
        ("zh-CN", "部分知识库原件暂时无法核对", "知识库来源已变化"),
        ("de-DE", "Einige Originaldateien sind vorübergehend nicht verfügbar.", "Die Wissensquellen haben sich geändert"),
        ("fr-FR", "Certains fichiers originaux sont temporairement indisponibles.", "Les sources de connaissances ont changé"),
    ],
)
async def test_source_warning_survives_no_citation_and_late_change_suppresses_citation(source, monkeypatch, locale, warning, paused):
    from bowhead_core.citations import TurnCitationRegistry
    from bowhead_core.kb.turn_guard import recheck_turn_sources

    file = await source.files.get(source.fid)
    checker = KbUseEligibility(source.files, source.artifacts, source.index)
    assert (await checker.check(source.fid, source.scope, AsyncMock(return_value=True))).allowed
    registry = TurnCitationRegistry()
    ctx = SimpleNamespace(
        language_context=SimpleNamespace(output_language=locale),
        user_id="u_test",
        workspace_id="ws_test",
        team_id="team_test",
        kb_files_store=source.files,
        kb_retriever=SimpleNamespace(_store=source.index),
        artifact_store=source.artifacts,
        kb_file_authorizer=AsyncMock(return_value=True),
        turn_citation_registry=registry,
        kb_source_uses={"source": {"file_id": source.fid, "generation_id": file.active_generation_id, "check_state": "verified_binding"}},
    )
    persisted = SimpleNamespace(record_turn_kb_source_checks=AsyncMock())
    original_head = source.artifacts.backend.head
    monkeypatch.setattr(source.artifacts.backend, "head", AsyncMock(side_effect=TimeoutError))
    allowed, notice = await recheck_turn_sources(ctx, session_store=persisted, turn_id="t_test")
    assert allowed and warning in notice
    assert persisted.record_turn_kb_source_checks.await_args.args[1][0]["check_state"] == "source_temporarily_unavailable"
    monkeypatch.setattr(source.artifacts.backend, "head", original_head)
    source.path.write_text("the original has changed")
    chunks = await source.index.list_active_chunks(file)
    registry.register(f"kb:{file.file_id}:{chunks[0][0]}", label="Source", kind="kb_chunk")
    allowed, notice = await recheck_turn_sources(ctx, session_store=persisted, turn_id="t_test")
    assert not allowed and paused in notice
    assert registry.all_citations() == []


async def test_matrix_finalize_checks_generation_in_sql_even_if_probe_passed(source, tmp_path, monkeypatch):
    from bowhead_core.kb.matrix_store import KbMatrixStore
    from test_chat_reference_storage import _upgrade

    path = tmp_path / "kb_matrix.db"
    _upgrade(path, target="matrix")
    store = KbMatrixStore(path)
    await store.initialize()
    file = await source.files.get(source.fid)
    matrix = await store.create_matrix(
        name="Test",
        workspace_id="ws_test",
        team_id="team_test",
        created_by="u_test",
        files=[{"file_id": file.file_id, "char_count": 100}],
        columns=[{"name": "Revenue", "type": "question", "question": "Revenue?"}],
    )
    col = matrix.columns[0]["column_id"]
    await store.enqueue_cells(
        matrix.matrix_id,
        [(file.file_id, col)],
        run_id="mxrun_test",
        workspace_id="ws_test",
        team_id="team_test",
        column_versions={col: 1},
        file_versions={file.file_id: file.version},
    )
    kwargs = {
        "status": "done",
        "variants": [{"is_primary": True, "normalized": {"value": "123"}}],
        "file_version": file.version,
        "kb_generation_id": file.active_generation_id,
        "kb_source_check_state": "verified_binding",
        "generation_db_path": source.files._db_path,
    }
    assert await store.finalize_cell(matrix.matrix_id, file.file_id, col, **kwargs)
    assert (await store.get_cell(matrix.matrix_id, file.file_id, col)).kb_generation_id == file.active_generation_id
    # Second call uses pooled connection: the attached database must not leak.
    await source.generations.mark_absent(file)
    assert not await store.finalize_cell(
        matrix.matrix_id, file.file_id, col, **{**kwargs, "variants": [{"is_primary": True, "normalized": {"value": "999"}}]}
    )
    assert (await store.get_cell(matrix.matrix_id, file.file_id, col)).variants[0]["normalized"]["value"] == "123"


@pytest.mark.parametrize("cached", [False, True])
async def test_matrix_late_source_loss_preserves_manual_value_and_does_not_charge(
    source, tmp_path, initialize_migrated_kb_files_database, monkeypatch, cached
):
    from test_chat_reference_storage import _upgrade
    from test_kb_matrix_engine import TEAM, USER, WS, _content_for, _mk_env, _mk_matrix, _parse_requested_fields, _Resp, _wait_run

    initialize_migrated_kb_files_database(tmp_path / "files.db")
    _upgrade(tmp_path / "kb_matrix.db", target="matrix")
    env = await _mk_env(tmp_path)
    fid = await env.files_store.create(
        artifact_id=str(source.path),
        filename="source.md",
        mime_type="text/markdown",
        size_bytes=50,
        scope="space",
        scope_id=WS,
        workspace_id=WS,
        team_id=TEAM,
        uploaded_by=USER,
    )
    worker = KbWorker(files_store=env.files_store, store=source.index, embedder=source.embedder)
    await worker.parse_and_index(fid)
    env.engine._kb_store = source.index
    env.engine._artifact_store = source.artifacts
    env.engine._file_authorizer = AsyncMock(return_value=True)
    matrix = await _mk_matrix(env, [fid], [{"name": "Revenue", "type": "question", "question": "Revenue?"}])
    col = matrix.columns[0]["column_id"]
    await env.store.edit_cell_value(matrix.matrix_id, fid, col, value="manual", column=matrix.columns[0], workspace_id=WS, team_id=TEAM)
    await env.store.set_cell_verified(matrix.matrix_id, fid, col, workspace_id=WS)
    original = await env.store.get_cell(matrix.matrix_id, fid, col)
    before = (await env.user_store.get_user(USER)).credit_balance

    if cached:

        async def late_cache(*args):
            source.path.write_text("replaced during cache lookup")
            return SimpleNamespace(variants=[{"is_primary": True, "normalized": {"value": "999"}}])

        monkeypatch.setattr(env.store, "find_cached_cell", late_cache)
    else:

        def late_model(i, req, pref):
            source.path.write_text("replaced during model invocation")
            return _Resp(content=_content_for(_parse_requested_fields(req)))

        env.router.handler = late_model
    run = await env.engine.start_run(matrix, {"kind": "single_cell", "file_id": fid, "column_id": col}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    cell = await env.store.get_cell(matrix.matrix_id, fid, col)
    assert cell.fail_reason == "source_unavailable" and cell.status == "failed"
    assert cell.variants == original.variants and cell.edited_by_user and cell.verified
    assert (await env.user_store.get_user(USER)).credit_balance == before
    assert (await env.store.get_run(run.run_id)).credits_charged == 0
    assert len(env.router.calls) == (0 if cached else 1)


async def test_confirmed_missing_source_requires_full_hash_after_head_returns(source, monkeypatch):
    from pathlib import Path

    checker = KbUseEligibility(source.files, source.artifacts, source.index)
    auth = AsyncMock(return_value=True)
    assert (await checker.check(source.fid, source.scope, auth)).allowed
    raw = source.path.read_bytes()
    control = Path(str(source.path) + ".control")
    control.write_bytes(b"healthy storage control")
    source.artifacts.list_recent_ready_paths.return_value = [("control", str(control))]
    source.path.unlink()
    assert (await checker.check(source.fid, source.scope, auth)).reason == "source_missing"
    assert (await source.files.get(source.fid)).source_absent
    source.path.write_bytes(raw)
    with monkeypatch.context() as patch:
        patch.setattr("bowhead_core.kb.eligibility.full_source_hash", AsyncMock(side_effect=TimeoutError))
        assert not (await checker.check(source.fid, source.scope, auth)).allowed
        assert (await source.files.get(source.fid)).source_absent
    assert (await checker.check(source.fid, source.scope, auth)).allowed
    assert not (await source.files.get(source.fid)).source_absent


async def test_citation_provenance_persists_before_live_event_and_survives_hydration(source, tmp_path, monkeypatch):
    from bowhead_core.agents.lead_orchestrator import _flush_citations
    from bowhead_core.citations import TurnCitationRegistry
    from bowhead_core.session_store import SessionStore
    from test_chat_reference_storage import _upgrade

    path = tmp_path / "core.db"
    _upgrade(path, target="core")
    sessions = SessionStore(path)
    await sessions.ensure_session(session_id="s_test", workspace_id="ws_test", user_id="u_test")
    await sessions.record_turn(turn_id="t_test", session_id="s_test", user_text="Revenue?")
    registry = TurnCitationRegistry()
    file = await source.files.get(source.fid)
    chunk = (await source.index.list_active_chunks(file))[0]
    identity = f"kb:{file.file_id}:{chunk[0]}"
    registry.register(identity, "Source", "kb_chunk")
    registry.bind_kb_check(identity, file.active_generation_id, "source_temporarily_unavailable", allowed=True)
    delivered = []

    async def emit(event):
        rows = await sessions.list_kb_citation_provenance(["t_test"])
        assert rows["t_test"][1]["kb_generation_id"] == event.payload["kb_generation_id"]
        delivered.append(event)

    ctx = SimpleNamespace(turn_citation_registry=registry)
    runtime = SimpleNamespace(parent_ctx=ctx, parent_bus=SimpleNamespace(emit=emit), session_store=sessions)
    await _flush_citations(runtime, SimpleNamespace(turn_id="t_test"))
    assert len(delivered) == 1
    assert delivered[0].payload["kb_source_check_state"] == "source_temporarily_unavailable"
    assert (await sessions.list_kb_citation_provenance(["t_test"]))["t_test"][1]["kb_source_check_state"] == "source_temporarily_unavailable"
    registry.bind_kb_check(identity, file.active_generation_id, "verified_binding", allowed=True)
    monkeypatch.setattr(sessions, "record_turn_citations", AsyncMock(side_effect=RuntimeError("disk full")))
    with pytest.raises(RuntimeError, match="KB citation persistence failed"):
        await _flush_citations(runtime, SimpleNamespace(turn_id="t_test"))
    assert len(delivered) == 1


async def test_search_filters_before_rerank_and_reports_incomplete_sources(source):
    from bowhead_core.kb.retriever import KbRetriever

    second = await source.files.create(
        artifact_id=str(source.path),
        filename="legacy.md",
        mime_type="text/markdown",
        size_bytes=50,
        scope="space",
        scope_id="ws_test",
        workspace_id="ws_test",
        team_id="team_test",
        uploaded_by="u_test",
    )
    # Real second generation competes for the same vector slots, then loses source access.
    await source.worker.parse_and_index(second)
    current = await source.files.get(second)
    await source.files.soft_delete(second)
    reranker = SimpleNamespace(rerank=AsyncMock(side_effect=lambda **kw: [(i, 0.9) for i in range(len(kw["documents"]))]))
    retriever = KbRetriever(source.embedder, source.index, reranker, files_store=source.files, artifact_store=source.artifacts)
    result = await retriever.invoke("Revenue in 2026", source.scope, top_k=1, authorize_file=AsyncMock(return_value=True))
    assert len(result.chunks) == 1 and result.chunks[0].file_id == source.fid
    assert current.filename not in " ".join(reranker.rerank.await_args.kwargs["documents"])
    assert result.chunks[0].generation_id == (await source.files.get(source.fid)).active_generation_id
    source.path.write_text("replaced original")
    unavailable = await retriever.invoke("Revenue in 2026", source.scope, top_k=1, authorize_file=AsyncMock(return_value=True))
    assert unavailable.chunks == [] and unavailable.partial


async def test_source_warning_persists_on_new_and_revised_report_sections(tmp_path):
    from bowhead_core.canvas_store import CanvasStore
    from bowhead_core.kb.turn_guard import PAUSED_NOTICE, annotate_turn_report

    canvas = CanvasStore(tmp_path / "canvas.db")
    await canvas.initialize()
    report = await canvas.ensure_canvas(session_id="s_report", workspace_id="ws_test", user_id="u_test")
    old = await canvas.append_section(canvas_id=report.id, kind="narrative", payload={"markdown": "Previous conclusion"}, by_turn_id="t_old")
    untouched = await canvas.append_section(canvas_id=report.id, kind="narrative", payload={"markdown": "Unrelated"}, by_turn_id="t_old")
    await canvas.patch_section(
        section_id=old.id, patch={"markdown": "Revised conclusion"}, expected_rev=old.rev, by_turn_id="t_new", caller="test", skip_owner_check=True
    )
    await canvas.append_section(canvas_id=report.id, kind="narrative", payload={"markdown": "New conclusion"}, by_turn_id="t_new")
    ctx = SimpleNamespace(canvas_store=canvas, turn_id="t_new", parent_bus=None)
    await annotate_turn_report(ctx, PAUSED_NOTICE, paused=True)
    sections = await canvas.list_sections_appended_by_turn("t_new", kinds=["narrative"], include_patches=True)
    assert len(sections) == 2
    assert all(PAUSED_NOTICE in section.payload["markdown"] for section in sections)
    assert all(PAUSED_NOTICE in section.payload["unverified_numbers"] for section in sections)
    assert untouched.id not in {section.id for section in sections}
    await annotate_turn_report(ctx, PAUSED_NOTICE, paused=True)
    assert [s.rev for s in await canvas.list_sections_appended_by_turn("t_new", include_patches=True)] == [s.rev for s in sections]
