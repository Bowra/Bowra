"""Current-source navigation, manual precedence and paid-work deduplication."""

from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError, bind_knowledge_scope, require_supported_knowledge_access
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.generations import KbGenerationStore
from bowhead_core.kb.source_navigation import list_sources, validate_navigation_dependencies
from bowhead_core.kb.source_navigation_store import NavigationConflict, SourceNavigationStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def sources(tmp_path, initialize_migrated_kb_files_database, monkeypatch):
    import base64

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", base64.urlsafe_b64encode(b"n" * 32).decode())
    path = tmp_path / "kb_files.db"
    initialize_migrated_kb_files_database(path)
    files = KbFilesStore(path)
    await files.initialize()
    nav = SourceNavigationStore(path)
    denied = set()

    async def authorize(file, *_args):
        return file.file_id not in denied

    ctx = SimpleNamespace(
        user_id="user_a",
        team_id="team_a",
        workspace_id="ws_a",
        kb_files_store=files,
        kb_file_authorizer=authorize,
        knowledge_scope=None,
        knowledge_navigation_uses={},
        knowledge_navigation_ancestors=(),
    )

    async def create(name="Policy.pdf"):
        identity = await files.create(
            artifact_id="art_" + name,
            filename=name,
            mime_type="application/pdf",
            size_bytes=100,
            scope="space",
            scope_id="ws_a",
            workspace_id="ws_a",
            team_id="team_a",
            uploaded_by="user_a",
        )
        file = await files.get(identity)
        generations = KbGenerationStore(path)
        generation = await generations.begin(file, "a" * 64, 1, "index-a")
        assert await generations.publish(file, generation, layer="L0", duration_ms=1, summary_json=None)
        return await files.get(identity)

    return ctx, nav, create, denied


async def test_manual_description_and_explicit_clear_restore_generated_text(sources):
    ctx, nav, create, _ = sources
    file = await create()
    revision = await nav.revision(file.file_id)
    identity = await nav.enqueue(
        file, fingerprint="model-a", admission_key="generate-1", actor_user_id=ctx.user_id, workspace_id=ctx.workspace_id, revision=revision
    )
    job = await nav.claim("worker-a")
    assert job["id"] == identity and await nav.dispatch(job, 2)
    await nav.adopt(
        job,
        file,
        text="Travel approval and reimbursement documents.",
        topics=["Travel"],
        chunks=[],
        provider="team-connection",
        model="text-a",
        usage={"input": 20},
    )
    assert (await list_sources(ctx)).entries[0]["description_kind"] == "standalone"
    revision = await nav.edit(file, revision=revision, actor_user_id=ctx.user_id, manual_text="Current travel process.", edit_text=True)
    page = await list_sources(ctx)
    assert page.entries[0]["description"] == "Current travel process."
    await nav.edit(file, revision=revision, actor_user_id=ctx.user_id, manual_text=None, edit_text=True)
    assert (await list_sources(ctx)).entries[0]["description_kind"] == "standalone"
    with pytest.raises(KnowledgeAccessError, match="source_changed"):
        await validate_navigation_dependencies(ctx, page.dependencies)


async def test_catalog_filters_before_limits_and_reauthorizes_retained_navigation(sources):
    ctx, nav, create, denied = sources
    allowed, hidden = await create("Readable.pdf"), await create("Hidden.pdf")
    denied.add(hidden.file_id)
    policy = require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "selected_files", "file_ids": [allowed.file_id]}})
    ctx.knowledge_scope = bind_knowledge_scope(ctx, policy, revision="arv_navigation")
    page = await list_sources(ctx, limit=1)
    assert [entry["file_id"] for entry in page.entries] == [allowed.file_id]
    assert "Hidden" not in str(page.payload())
    await validate_navigation_dependencies(ctx, page.dependencies)
    denied.add(allowed.file_id)
    with pytest.raises(KnowledgeAccessError, match="source_changed"):
        await validate_navigation_dependencies(ctx, page.dependencies)


async def test_category_revision_and_explicit_unclassified_are_retained(sources):
    ctx, nav, create, _ = sources
    file = await create()
    category = await nav.save_category(
        team_id=ctx.team_id, scope="team", scope_id=ctx.team_id, name="Travel", aliases=[], parent_id=None, actor_user_id=ctx.user_id
    )
    await nav.edit(file, revision=0, actor_user_id=ctx.user_id, category_ids=[category["id"]])
    page = await list_sources(ctx, category_id=category["id"])
    assert page.categories[0]["count"] == 1
    await nav.save_category(
        team_id=ctx.team_id,
        scope="team",
        scope_id=ctx.team_id,
        category_id=category["id"],
        revision=1,
        name="Expenses",
        aliases=["Travel"],
        parent_id=None,
        actor_user_id=ctx.user_id,
    )
    with pytest.raises(KnowledgeAccessError, match="source_changed"):
        await validate_navigation_dependencies(ctx, page.dependencies)
    await nav.edit(file, revision=0, actor_user_id=ctx.user_id, category_ids=[])
    assert (await list_sources(ctx, category_id="unclassified")).entries[0]["categories"] == []


async def test_job_replays_and_expired_dispatch_do_not_issue_another_claim(sources):
    ctx, nav, create, _ = sources
    file = await create()
    args = {"fingerprint": "model-a", "admission_key": "publication-1", "actor_user_id": ctx.user_id, "workspace_id": ctx.workspace_id, "revision": 0}
    assert await nav.enqueue(file, **args) == await nav.enqueue(file, **args)
    job = await nav.claim("worker-a")
    assert await nav.claim("worker-b") is None
    assert await nav.dispatch(job, 2)
    async with open_shared_ctx(nav.path) as db:
        await db.execute("UPDATE kb_source_description_jobs SET lease_until=0 WHERE id=?", (job["id"],))
        await db.commit()
    assert await nav.claim("worker-b") is None
    assert (await nav.latest_job(file.file_id))["error_code"] == "outcome_unknown"


async def test_manual_revision_fences_in_flight_generation(sources):
    ctx, nav, create, _ = sources
    file = await create()
    await nav.enqueue(file, fingerprint="model-a", admission_key="generate-1", actor_user_id=ctx.user_id, workspace_id=ctx.workspace_id, revision=0)
    job = await nav.claim("worker-a")
    assert await nav.dispatch(job, 1)
    await nav.edit(file, revision=0, actor_user_id=ctx.user_id, manual_text="Reviewed source.", edit_text=True)
    with pytest.raises(NavigationConflict):
        await nav.adopt(job, file, text="Old model result.", topics=[], chunks=[], provider="connection", model="text-a", usage={})
    assert (await list_sources(ctx)).entries[0]["description"] == "Reviewed source."


async def test_cursor_is_bound_to_query_and_current_caller_scope(sources):
    ctx, _, create, _ = sources
    await create("First.pdf")
    await create("Second.pdf")
    page = await list_sources(ctx, limit=1)
    assert page.partial and page.next_cursor
    next_page = await list_sources(ctx, limit=1, cursor=page.next_cursor)
    assert next_page.entries[0]["file_id"] != page.entries[0]["file_id"]
    with pytest.raises(ValueError, match="cursor_invalid"):
        await list_sources(ctx, query="different", cursor=page.next_cursor)


async def test_equivalent_pending_jobs_do_not_dispatch_concurrently_or_resume_unknown_outcomes(sources):
    ctx, nav, create, _ = sources
    file = await create()
    args = {"fingerprint": "model-a", "actor_user_id": ctx.user_id, "workspace_id": ctx.workspace_id, "revision": 0}
    first = await nav.enqueue(file, admission_key="publication-1", **args)
    second = await nav.enqueue(file, admission_key="manual-1", **args)
    assert first != second
    job = await nav.claim("worker-a")
    assert await nav.claim("worker-b") is None
    assert await nav.dispatch(job, 2)
    await nav.finish(job, "failed", error="outcome_unknown")
    assert await nav.claim("worker-b") is None
    assert (await nav.latest_job(file.file_id))["state"] == "superseded"
    repair = await nav.enqueue(file, admission_key="explicit-repair-after-failure", **args)
    assert (await nav.claim("worker-b"))["id"] == repair


async def test_matrix_text_is_not_loaded_until_origin_authority_is_checked(sources, monkeypatch):
    from unittest.mock import AsyncMock

    ctx, nav, create, _ = sources
    file = await create()
    await nav.cache_matrix(
        file,
        text="Private Matrix description",
        topics=["Private"],
        actor_user_id=ctx.user_id,
        chunks=["chunk-a"],
        provider=None,
        model=None,
        matrix={
            "workspace_id": ctx.workspace_id,
            "matrix_id": "matrix-a",
            "column_id": "column-a",
            "structure_seq": 1,
            "prompt_version": 1,
            "cell_seq": 1,
            "role": "description",
        },
    )
    # Inspect arguments instead of returning any private body before authorization.
    monkeypatch.setattr(SourceNavigationStore, "matrix_bodies", AsyncMock(return_value={}))
    ctx.kb_file_authorizer.navigation = AsyncMock(return_value=set())
    page = await list_sources(ctx)
    assert page.entries[0]["description_kind"] == "name"
    assert "Private" not in str(page.payload())
    SourceNavigationStore.matrix_bodies.assert_awaited_once_with([])


async def test_compacted_navigation_dependencies_are_rechecked_after_access_changes(sources):
    from bowhead_core.context_dependencies import capture_context_dependencies, validate_context_dependencies
    from bowhead_core.kb.source_navigation import record_navigation_uses

    ctx, _, create, denied = sources
    file = await create()
    ctx.knowledge_source_uses = {}
    record_navigation_uses(ctx, (await list_sources(ctx)).dependencies)
    retained = capture_context_dependencies(ctx)
    assert retained[0].name == "context_navigation_dependencies_v1"
    assert file.filename not in retained[0].content
    await validate_context_dependencies(ctx, retained)
    denied.add(file.file_id)
    with pytest.raises(KnowledgeAccessError):
        await validate_context_dependencies(ctx, retained)


async def test_nonempty_generation_strings_do_not_qualify_as_native_publication_proof(sources):
    ctx, nav, create, _ = sources
    file = await create()
    async with open_shared_ctx(nav.path) as db:
        await db.execute("UPDATE kb_files SET active_generation_id=? WHERE file_id=?", ("b" * 64, file.file_id))
        await db.commit()
    file = await ctx.kb_files_store.get(file.file_id)
    with pytest.raises(NavigationConflict, match="generation_unverified"):
        await nav.enqueue(
            file,
            fingerprint="model-a",
            admission_key="hash-is-not-a-generation",
            actor_user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            revision=0,
        )
    assert await nav.published_generations([file]) == set()


async def test_long_category_vocabularies_preserve_assigned_labels_and_two_level_depth(sources):
    ctx, nav, create, _ = sources
    file = await create()
    for index in range(101):
        await nav.save_category(
            team_id=ctx.team_id, scope="team", scope_id=ctx.team_id, name=f"A{index:03d}", aliases=[], parent_id=None, actor_user_id=ctx.user_id
        )
    root = await nav.save_category(
        team_id=ctx.team_id, scope="team", scope_id=ctx.team_id, name="Z parent", aliases=[], parent_id=None, actor_user_id=ctx.user_id
    )
    child = await nav.save_category(
        team_id=ctx.team_id, scope="team", scope_id=ctx.team_id, name="Z child", aliases=[], parent_id=root["id"], actor_user_id=ctx.user_id
    )
    await nav.edit(file, revision=0, actor_user_id=ctx.user_id, category_ids=[child["id"]])
    assert (await list_sources(ctx)).entries[0]["categories"][0]["name"] == "Z child"
    other = (await nav.categories(ctx.team_id, ctx.user_id, ctx.workspace_id))[0]
    with pytest.raises(NavigationConflict, match="parent_invalid"):
        await nav.save_category(
            team_id=ctx.team_id,
            scope="team",
            scope_id=ctx.team_id,
            name="Z parent",
            aliases=[],
            parent_id=other["id"],
            actor_user_id=ctx.user_id,
            category_id=root["id"],
            revision=1,
        )


async def test_publication_admission_retains_original_actor_and_current_rebuild_actor(sources):
    from bowhead_core.kb.ingestion import KnowledgeAction

    ctx, nav, _, _ = sources
    identity, _, _ = await ctx.kb_files_store.create_admitted(
        action=KnowledgeAction("source-admission", "kb_ui", ctx.user_id, ctx.team_id, ctx.workspace_id),
        source_fingerprint="source-one",
        artifact_id="art_admitted",
        filename="Policy.md",
        mime_type="text/markdown",
        size_bytes=10,
        scope="space",
        scope_id=ctx.workspace_id,
        workspace_id=ctx.workspace_id,
        team_id=ctx.team_id,
        uploaded_by=ctx.user_id,
    )
    file = await ctx.kb_files_store.get(identity)
    generations = KbGenerationStore(nav.path)
    generation = await generations.begin(file, "a" * 64, 1, "index-a")
    assert await generations.publish(file, generation, layer="L0", duration_ms=1, summary_json=None, description_fingerprint="model-a")
    job = await nav.latest_job(identity)
    published = await ctx.kb_files_store.get(identity)
    assert (job["actor_user_id"], job["workspace_id"], job["source_version"]) == (ctx.user_id, ctx.workspace_id, published.version)
    generation = await generations.begin(published, "a" * 64, 1, "index-b")
    rebuild = KnowledgeAction("rebuild-a", "kb_ui", "rebuild-maintainer", ctx.team_id, ctx.workspace_id)
    assert await generations.publish(
        published, generation, layer="L0", duration_ms=1, summary_json=None, description_fingerprint="model-a", description_action=rebuild
    )
    assert (await nav.latest_job(identity))["actor_user_id"] == "rebuild-maintainer"


async def test_late_automatic_enrichment_does_not_interrupt_a_current_basic_source_snapshot(sources):
    ctx, nav, create, _ = sources
    file = await create()
    basic = await list_sources(ctx)
    await nav.enqueue(
        file, fingerprint="model-a", admission_key="late-description", actor_user_id=ctx.user_id, workspace_id=ctx.workspace_id, revision=0
    )
    job = await nav.claim("worker-a")
    assert await nav.dispatch(job, 2)
    await nav.adopt(job, file, text="Travel procedures and approvals.", topics=["Travel"], chunks=[], provider="team", model="text-a", usage={})
    await validate_navigation_dependencies(ctx, basic.dependencies)
    assert (await list_sources(ctx)).entries[0]["description_kind"] == "standalone"
    await nav.edit(file, revision=0, actor_user_id=ctx.user_id, manual_text="Corrected source scope.", edit_text=True)
    with pytest.raises(KnowledgeAccessError, match="source_changed"):
        await validate_navigation_dependencies(ctx, basic.dependencies)


async def test_initial_hint_prioritizes_maintained_sources_in_a_large_library(sources, monkeypatch):
    from bowhead_core.kb import files_store
    from bowhead_core.knowledge_source_hints import build_source_hint

    ctx, nav, create, denied = sources
    identities = iter(
        [f"{index:012x}" for index in range(100)] + ["ffffffffffff"] + [f"{index:012x}" for index in range(100, 113)] + ["fffffffffffe"]
    )
    monkeypatch.setattr(files_store, "uuid4", lambda: SimpleNamespace(hex=next(identities)))
    for index in range(100):
        await create(f"Historical source {index}.pdf")
    target = await create("opaque-current-source.pdf")
    await nav.edit(target, revision=0, actor_user_id=ctx.user_id, manual_text="North Lantern handoff procedures.", edit_text=True)
    for index in range(13):
        await create(f"Recent basic source {index}.pdf")
    hidden = await create("Restricted maintained source.pdf")
    await nav.edit(hidden, revision=0, actor_user_id=ctx.user_id, manual_text="Restricted handoff details.", edit_text=True)
    denied.add(hidden.file_id)

    initial = await list_sources(ctx, limit=12, initial=True)
    assert initial.entries[0]["file_id"] == target.file_id
    hint = await build_source_hint(ctx)
    assert hint is not None
    assert "North Lantern handoff procedures." in hint[0]
    assert target.file_id in hint[1]
    assert "Restricted maintained source" not in hint[0]
    assert hidden.file_id not in hint[1]
    assert len(hint[1]) <= 12
