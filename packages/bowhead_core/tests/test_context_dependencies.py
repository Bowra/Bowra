"""Source hints remain reauthorizable after their text has been summarized."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
from bowhead_core.context_dependencies import capture_context_dependencies, validate_context_dependencies
from bowhead_core.context_execution import ExecutionContextView
from bowhead_router.types import Message
from test_agent_collaboration import collab as collaboration_fixture
from test_context_execution import prepare
from test_context_maintenance import context_case as maintenance_fixture

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


async def test_hint_dependencies_stay_encrypted_and_revocation_invalidates_restored_summary(collab, context_case, monkeypatch):
    from bowhead_core import knowledge_source_runtime

    allowed = {"file_original": "generation_original"}

    async def readable(ctx, identity, *, generation, enforce):
        assert enforce
        return allowed.get(identity) == generation

    monkeypatch.setattr(knowledge_source_runtime, "readable_source", readable)

    def context():
        return SimpleNamespace(knowledge_source_uses={}, knowledge_source_ancestors=(), ordinary_lead_state=None)

    def owner(ctx):
        async def validate(messages):
            await validate_context_dependencies(ctx, messages)

        return ExecutionContextView(
            db_path=collab.path,
            worker=collab.worker,
            execution_id=collab.requester,
            lane="ordinary",
            cipher=collab.store.cipher,
            validate_sources=validate,
            activity=AsyncMock(),
            source_dependencies=lambda: capture_context_dependencies(ctx),
        )

    original = context()
    original.knowledge_source_uses.update(allowed)
    execution = owner(original)
    await prepare(execution, context_case)
    assert len(context_case.sent) == 1
    assert not any(message.name == "context_source_dependencies_v1" for message in context_case.sent[0].messages)
    restored_context = context()
    await owner(restored_context).restore()
    assert restored_context.knowledge_source_uses == allowed
    allowed.clear()
    with pytest.raises(KnowledgeAccessError, match="knowledge_source_changed"):
        await owner(context()).restore()
    assert len(context_case.sent) == 1


@pytest.mark.unit
def test_conflicting_retained_hint_generations_cannot_be_combined():
    ctx = SimpleNamespace(knowledge_source_uses={"file": "one"}, ordinary_lead_state=SimpleNamespace(knowledge_sources={"file": "two"}))
    with pytest.raises(KnowledgeAccessError, match="knowledge_source_changed"):
        capture_context_dependencies(ctx)


@pytest.mark.unit
async def test_malformed_dependency_data_is_not_accepted_as_source_evidence():
    with pytest.raises(KnowledgeAccessError, match="knowledge_source_provenance_unavailable"):
        await validate_context_dependencies(None, [Message(role="user", name="context_source_dependencies_v1", content='{"knowledge_sources":true}')])


@pytest.mark.parametrize("continuation_engaged", [True, False])
async def test_material_dependencies_repeat_original_readers_after_restoring_summary(collab, context_case, monkeypatch, continuation_engaged):
    from bowhead_core.context_dependencies import ContextSourceObservations
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable

    from bowhead_core import ordinary_lead_material

    readable = True
    calls = []
    record = {"kind": "sandbox", "skills": [], "artifacts": [], "tool": "python_exec"}

    async def read(ctx, tools, *, contract):
        calls.append((ctx.turn_id, tools, contract))
        if not readable:
            raise OrdinaryContinuationUnavailable()

    metadata = AsyncMock()
    monkeypatch.setattr(ordinary_lead_material, "revalidate_tool_materials", read)
    monkeypatch.setattr(ordinary_lead_material, "revalidate_material_authority_in_connection", metadata)
    ctx = SimpleNamespace(
        turn_id=collab.worker.turn_id,
        session_id=collab.worker.session_id,
        session_store=SimpleNamespace(db_path=collab.path),
        knowledge_source_uses={},
        ordinary_lead_state=SimpleNamespace(knowledge_sources={}, checkpoint=None, tool_materials={"call": record}) if continuation_engaged else None,
        context_source_observations=None if continuation_engaged else ContextSourceObservations(tool_materials={"call": record}),
    )

    def owner():
        return ExecutionContextView(
            db_path=collab.path,
            worker=collab.worker,
            execution_id=collab.requester,
            lane="ordinary",
            cipher=collab.store.cipher,
            validate_sources=lambda messages: validate_context_dependencies(ctx, messages),
            activity=AsyncMock(),
            source_dependencies=lambda: capture_context_dependencies(ctx),
        )

    await prepare(owner(), context_case)
    assert calls and metadata.await_count
    assert all(turn == collab.worker.turn_id and tools == [record] for turn, tools, _ in calls)
    assert not any(message.name == "context_material_dependencies_v1" for message in context_case.sent[0].messages)
    await owner().restore()
    readable = False
    with pytest.raises(OrdinaryContinuationUnavailable):
        await owner().restore()


@pytest.mark.parametrize("mutation", ["revoke", "replace_bytes", "none"])
async def test_attachment_proofs_keep_current_access_and_exact_image_bytes(collab, mutation):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.attachments_injector import prepare_attachments
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.context_dependencies import capture_execution_dependencies
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
    from bowhead_core.session_store import SessionStore, _insert_chat_turn_user_source_in_tx

    worker = collab.worker
    artifacts = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / "context-images"))
    await artifacts.initialize()
    original = b"original-private-image"
    art = await artifacts.put_bytes(
        content=original,
        filename="evidence.png",
        mime="image/png",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        bind_chat_upload=True,
    )
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT user_text FROM turns WHERE id=?", (worker.turn_id,)) as cursor:
            user_text = (await cursor.fetchone())[0]
        await _insert_chat_turn_user_source_in_tx(
            db,
            created_at="2026-10-30T00:00:00Z",
            source=ChatTurnUserSource(
                turn_id=worker.turn_id,
                session_id=worker.session_id,
                user_id=worker.user_id,
                workspace_id=worker.workspace_id,
                user_text=user_text,
                attachment_ids=(art.id,),
                attachment_refs=({"fileId": art.id, "filename": art.filename, "mime": art.mime, "size": art.size},),
                parts=None,
            ),
        )
        await db.commit()
    ctx = SimpleNamespace(
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_store=SessionStore(collab.path),
        artifact_store=artifacts,
        knowledge_source_uses={},
        ordinary_lead_state=None,
        _psi_parsed_results=await prepare_attachments(store=artifacts, artifact_ids=[art.id]),
    )
    proofs = await capture_execution_dependencies(ctx)
    assert len(proofs) == 1 and proofs[0].name == "context_attachment_dependencies_v1"
    assert original.decode() not in proofs[0].content
    assert ctx._psi_parsed_results[0].image_base64 not in proofs[0].content
    await validate_context_dependencies(ctx, proofs)
    from bowhead_core.context_dependencies import ContextSourceValidator, PreparedContextSourceValidator

    prepared = PreparedContextSourceValidator(ContextSourceValidator(ctx))
    await prepared.collect(proofs)
    if mutation == "none":
        await prepared(proofs)
        return
    if mutation == "revoke":
        async with open_shared_ctx(collab.path) as db:
            await db.execute("DELETE FROM artifact_upload_bindings WHERE artifact_id=?", (art.id,))
            await db.commit()
    else:
        from pathlib import Path

        Path(art.path).write_bytes(b"x" * len(original))
    with pytest.raises(OrdinaryContinuationUnavailable):
        await validate_context_dependencies(ctx, proofs)
    if mutation == "revoke":
        with pytest.raises(OrdinaryContinuationUnavailable):
            await prepared(proofs)


async def test_context_sources_do_not_inherit_continuation_mime_or_size_caps(collab):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.attachments_injector import prepare_attachments
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.context_attachments import capture_attachment_dependencies, validate_attachment_dependencies

    artifacts = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / "context-large"))
    await artifacts.initialize()
    worker = collab.worker
    art = await artifacts.put_bytes(
        content=b"a" * (21 * 1024 * 1024),
        filename="large.txt",
        mime="text/plain",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        bind_chat_upload=True,
    )
    unsupported = await artifacts.put_bytes(
        content=b"opaque",
        filename="data.bin",
        mime="application/octet-stream",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        bind_chat_upload=True,
    )
    office = await artifacts.put_bytes(
        content=b"unopened",
        filename="report.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        bind_chat_upload=True,
    )
    items = [art, unsupported, office]
    source = ChatTurnUserSource(
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        user_text="Inspect these files",
        attachment_ids=tuple(item.id for item in items),
        attachment_refs=tuple({"fileId": item.id, "filename": item.filename, "mime": item.mime, "size": item.size} for item in items),
        parts=None,
    )
    parsed = await prepare_attachments(store=artifacts, artifact_ids=list(source.attachment_ids))
    ctx = SimpleNamespace(
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_store=SimpleNamespace(db_path=collab.path),
        artifact_store=artifacts,
        _psi_parsed_results=parsed,
    )
    assert parsed[0].content_hash and parsed[1].content_hash
    assert parsed[2].content_deferred and parsed[2].content_hash is None
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN")
        proof = await capture_attachment_dependencies(ctx, db, source)
        assert len(proof.files) == 3 and proof.files[2].content_hash is None
        await validate_attachment_dependencies(ctx, db, source, proof)


@pytest.mark.parametrize("publish", [False, True])
async def test_compaction_material_bytes_do_not_hold_the_shared_writer_lock(collab, context_case, monkeypatch, publish):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.context_dependencies import ContextSourceObservations, ContextSourceValidator

    from bowhead_core import ordinary_lead_material

    checks = []

    async def read(ctx, tools, *, contract):
        # A byte read may be slow. Another writer must remain able to proceed.
        async with open_shared_ctx(collab.path) as concurrent:
            await concurrent.execute("PRAGMA busy_timeout=25")
            await concurrent.execute("BEGIN IMMEDIATE")
            await concurrent.rollback()
        checks.append(tools)

    monkeypatch.setattr(ordinary_lead_material, "revalidate_tool_materials", read)
    monkeypatch.setattr(ordinary_lead_material, "revalidate_material_authority_in_connection", AsyncMock())
    record = {"kind": "sandbox", "skills": [], "artifacts": [], "tool": "python_exec"}
    ctx = SimpleNamespace(
        turn_id=collab.worker.turn_id,
        session_id=collab.worker.session_id,
        session_store=SimpleNamespace(db_path=collab.path),
        knowledge_scope=None,
        knowledge_source_uses={},
        knowledge_source_ancestors=(),
        ordinary_lead_state=None,
        context_source_observations=ContextSourceObservations(tool_materials={"call": record}),
    )
    owner = ExecutionContextView(
        db_path=collab.path,
        worker=collab.worker,
        execution_id=collab.lead if publish else collab.requester,
        lane="ordinary",
        cipher=collab.store.cipher,
        validate_sources=ContextSourceValidator(ctx),
        activity=AsyncMock(),
        source_dependencies=lambda: capture_context_dependencies(ctx),
    )
    await prepare(owner, context_case)
    if publish:
        from bowhead_core.session_store import SessionStore
        from test_context_history import completion

        publication = await owner.conversation_publication(context_case.request.messages)
        assert await SessionStore(collab.path).mark_completed(**completion(collab, publication))
    assert checks


@pytest.mark.parametrize("same_call_id", [False, True])
async def test_ancestry_revalidates_each_material_once_per_adoption_and_never_reuses_authority(collab, monkeypatch, same_call_id):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.context_dependencies import ContextSourceValidator, MaterialContextDependencies, PreparedContextSourceValidator
    from bowhead_core.context_store import ContextOrigin, read_context_graph_in_tx, write_context_in_tx
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
    from bowhead_router.context_policy import ContextPolicy

    from bowhead_core import ordinary_lead_material

    allowed = True
    checked = []

    async def metadata(ctx, db, material, *, contract):
        if not allowed:
            raise OrdinaryContinuationUnavailable()
        checked.extend(record["revision"] for record in material["tools"])

    monkeypatch.setattr(ordinary_lead_material, "revalidate_tool_materials", AsyncMock())
    monkeypatch.setattr(ordinary_lead_material, "revalidate_material_authority_in_connection", metadata)
    ctx = SimpleNamespace(
        turn_id=collab.worker.turn_id,
        session_id=collab.worker.session_id,
        session_store=SimpleNamespace(db_path=collab.path),
        knowledge_scope=None,
        knowledge_source_uses={},
        knowledge_source_ancestors=(),
        ordinary_lead_state=None,
    )
    messages = []
    generation = None
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        records = []
        for index in range(7):
            records.append(
                {"kind": "sandbox", "skills": [], "artifacts": [], "call_id": "same" if same_call_id else str(index), "revision": str(index)}
            )
            proof = MaterialContextDependencies(turn_id=ctx.turn_id, tools=list(records))
            message = Message(role="user", name="context_material_dependencies_v1", content=proof.model_dump_json())
            messages.append(message)
            generation = await write_context_in_tx(
                db,
                worker=collab.worker,
                execution_id=collab.requester,
                lane="ordinary",
                expected_generation=generation,
                messages=[message],
                policy=ContextPolicy(),
                origin=ContextOrigin(provider="synthetic", model="synthetic"),
                cipher=collab.store.cipher,
                validate_sources=AsyncMock(),
            )
        await db.commit()
    prepared = PreparedContextSourceValidator(ContextSourceValidator(ctx))
    await prepared.collect(messages)
    checked.clear()

    async def adopt():
        async with open_shared_ctx(collab.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            return await read_context_graph_in_tx(
                db,
                worker=collab.worker,
                execution_id=collab.requester,
                lane="ordinary",
                generation_id=generation,
                cipher=collab.store.cipher,
                validate_sources=prepared,
            )

    graph = await adopt()
    assert len(graph) == 7
    assert sorted(checked) == [str(index) for index in range(7)]
    allowed = False
    with pytest.raises(OrdinaryContinuationUnavailable):
        await adopt()


async def test_adoption_observes_source_revocation_in_its_own_transaction(collab, monkeypatch):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.context_dependencies import ContextSourceValidator, MaterialContextDependencies, PreparedContextSourceValidator
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable

    from bowhead_core import ordinary_lead_material

    async with open_shared_ctx(collab.path) as db:
        await db.execute("CREATE TABLE context_source_access (allowed INTEGER NOT NULL)")
        await db.execute("INSERT INTO context_source_access VALUES (1)")
        await db.commit()

    async def metadata(ctx, db, material, *, contract):
        async with db.execute("SELECT allowed FROM context_source_access") as cursor:
            if not (await cursor.fetchone())[0]:
                raise OrdinaryContinuationUnavailable()

    monkeypatch.setattr(ordinary_lead_material, "revalidate_tool_materials", AsyncMock())
    monkeypatch.setattr(ordinary_lead_material, "revalidate_material_authority_in_connection", metadata)
    ctx = SimpleNamespace(
        session_id=collab.worker.session_id,
        session_store=SimpleNamespace(db_path=collab.path),
        knowledge_scope=None,
        knowledge_source_uses={},
        ordinary_lead_state=None,
    )
    proof = MaterialContextDependencies(turn_id=collab.worker.turn_id, tools=[{"kind": "sandbox", "skills": [], "artifacts": []}])
    messages = [Message(role="user", name="context_material_dependencies_v1", content=proof.model_dump_json())]
    prepared = PreparedContextSourceValidator(ContextSourceValidator(ctx))
    await prepared.collect(messages)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE context_source_access SET allowed=0")
        with pytest.raises(OrdinaryContinuationUnavailable):
            await prepared.for_adoption(db)(messages)
        await db.rollback()


async def test_compaction_and_publication_encryption_allow_an_independent_writer(collab, context_case, monkeypatch):
    import sqlite3

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.session_store import SessionStore
    from test_context_execution import owner
    from test_context_history import completion

    from bowhead_core import context_store

    async with open_shared_ctx(collab.path) as db:
        await db.execute("CREATE TABLE context_independent_writes (id INTEGER PRIMARY KEY)")
        await db.commit()
    original = context_store.seal_context_messages

    def seal(*args, **kwargs):
        # Sealing runs in a worker thread. B can commit while A prepares its bytes.
        with sqlite3.connect(collab.path, timeout=0.025) as independent:
            independent.execute("INSERT INTO context_independent_writes DEFAULT VALUES")
        return original(*args, **kwargs)

    monkeypatch.setattr(context_store, "seal_context_messages", seal)
    execution = owner(collab, execution_id=collab.lead)
    await prepare(execution, context_case)
    publication = await execution.conversation_publication(context_case.request.messages)
    assert await SessionStore(collab.path).mark_completed(**completion(collab, publication))
    async with open_shared_ctx(collab.path) as db, db.execute("SELECT COUNT(*) FROM context_independent_writes") as cursor:
        assert (await cursor.fetchone())[0] >= 3


async def test_pinned_context_uses_one_authoritative_connection_for_all_artifact_readers(collab, monkeypatch):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.context_attachments import ContextAttachment, _authorize

    from bowhead_core import artifact_store

    worker = collab.worker
    store = ArtifactStore(str(collab.path), LocalBackend(collab.path.parent / "context-pins"))
    await store.initialize()
    art = await store.put_bytes(
        content=b"retained evidence",
        filename="evidence.txt",
        mime="text/plain",
        created_by=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        created_by_turn=worker.turn_id,
        bind_chat_upload=True,
    )
    ctx = SimpleNamespace(
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        session_store=SimpleNamespace(db_path=collab.path),
        artifact_store=store,
    )
    files = [ContextAttachment(artifact_id=art.id, filename=art.filename, mime=art.mime, size=art.size)]
    source = SimpleNamespace(attachment_refs=[], attachment_ids=())

    def unexpected_connection(*args, **kwargs):
        raise AssertionError("Retained artifact validation must use the caller's current transaction")

    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        previous_factory = db.row_factory
        with monkeypatch.context() as patch:
            patch.setattr(artifact_store.aiosqlite, "connect", unexpected_connection)
            assert [item.id for item in await _authorize(ctx, db, source, files)] == [art.id]
            assert (await store.get(art.id, connection=db)).id == art.id
            await db.execute("UPDATE artifacts SET filename=? WHERE id=?", ("changed.txt", art.id))
            with pytest.raises(ValueError, match="context_attachment_source_changed"):
                await _authorize(ctx, db, source, files)
            assert (await store.get(art.id, connection=db)).filename == "changed.txt"
            assert db.row_factory is previous_factory
            assert db.in_transaction
        await db.rollback()
    assert (await store.get(art.id)).filename == art.filename
