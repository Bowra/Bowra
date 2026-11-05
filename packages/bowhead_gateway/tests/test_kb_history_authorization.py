"""Current KB authority on a split execution snapshot and a separate file reader."""

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_pooled_read_ctx, open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
from bowhead_core.context_dependencies import ContextSourceValidator
from bowhead_core.kb import source_navigation
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.generations import KbGenerationStore
from bowhead_core.kb.source_navigation import list_sources
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.user_store import UserStore
from bowhead_router.types import Message
from cryptography.fernet import Fernet

from bowhead_core import access_control, user_store
from bowhead_gateway import kb_authorization


@pytest.mark.integration
async def test_history_uses_one_current_authority_and_file_reader_then_honors_revocation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    core_path = tmp_path / "bowhead.db"
    await anyio.to_thread.run_sync(upgrade, f"sqlite:///{core_path}")
    users = UserStore(core_path)
    team = await users.create_team(name="Knowledge History")
    owner = await users.create_user(team_id=team.id, email="history-owner@example.test")
    member = await users.create_user(team_id=team.id, email="history-member@example.test")
    workspace_id = await users.create_local_workspace(team_id=team.id, name="History Space", creator_user_id=owner.id)
    await users.add_member(workspace_id=workspace_id, user_id=member.id, role="member")
    access = AccessControlStore(core_path)
    files_path = tmp_path / "kb_files.db"
    files = KbFilesStore(files_path)
    file_id = await files.create(
        artifact_id="art_history_source",
        filename="Policy.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        scope="space",
        scope_id=workspace_id,
        workspace_id=workspace_id,
        team_id=team.id,
        uploaded_by=owner.id,
    )
    file = await files.get(file_id)
    generations = KbGenerationStore(files_path)
    generation = await generations.begin(file, "a" * 64, 1, "history-index")
    assert await generations.publish(file, generation, layer="L0", duration_ms=1, summary_json=None)
    ctx = SimpleNamespace(
        user_id=member.id,
        team_id=team.id,
        workspace_id=workspace_id,
        knowledge_scope=None,
        ordinary_lead_state=None,
        knowledge_source_uses={},
        knowledge_source_ancestors=(),
        knowledge_navigation_uses={},
        knowledge_navigation_ancestors=(),
        kb_files_store=files,
        kb_file_authorizer=kb_authorization.kb_file_authorizer(users, access),
    )
    page = await list_sources(ctx)
    assert set(page.dependencies) == {file_id}
    messages = [
        Message(role="tool", tool_call_id="history-list", content=json.dumps({"knowledge_navigation_provenance": page.dependencies})),
        Message(role="user", name="context_navigation_dependencies_v1", content=json.dumps({"knowledge_navigation": page.dependencies})),
    ]

    async with (
        open_pooled_read_ctx(files_path) as reader,
        open_shared_ctx(str(domain_database_path(core_path, "execution"))) as writer,
    ):
        await writer.execute("BEGIN IMMEDIATE")
        builds = []
        checks = []
        build = kb_authorization.build_access_context
        check = source_navigation.validate_navigation_dependencies

        async def count_build(**kwargs):
            builds.append(kwargs["connection"])
            return await build(**kwargs)

        async def count_check(context, dependencies, **kwargs):
            checks.append(kwargs["connection"])
            return await check(context, dependencies, **kwargs)

        def unexpected_reader(*_args, **_kwargs):
            pytest.fail("Adoption opened a second metadata or authority connection")

        with monkeypatch.context() as scoped:
            scoped.setattr(kb_authorization, "build_access_context", count_build)
            scoped.setattr(source_navigation, "validate_navigation_dependencies", count_check)
            scoped.setattr(user_store, "open_pooled_read_ctx", unexpected_reader)
            scoped.setattr(access_control, "open_pooled_read_ctx", unexpected_reader)
            scoped.setattr(source_navigation, "open_pooled_read_ctx", unexpected_reader)
            await ContextSourceValidator(ctx, knowledge_connection=reader).validate(messages, read_bytes=False, connection=writer)
        assert builds == [writer]
        assert checks == [reader]
        assert not reader.in_transaction
        await writer.rollback()
    assert ctx.knowledge_navigation_uses == page.dependencies

    await access.create_or_update_resource_grant(
        team_id=team.id,
        resource_kind="kb_file",
        resource_id=file_id,
        principal_type="user",
        principal_id=member.id,
        permission="read",
        effect="deny",
        created_by=owner.id,
    )
    ctx.knowledge_navigation_uses.clear()
    with pytest.raises(KnowledgeAccessError, match="knowledge_source_changed"):
        await ContextSourceValidator(ctx).validate(messages, read_bytes=False)
    assert ctx.knowledge_navigation_uses == {}
