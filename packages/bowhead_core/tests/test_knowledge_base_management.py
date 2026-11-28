"""Knowledge base management in core (phase 1 slice 3): store writes, switch-off levels, targets and call sites."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
from bowhead_core.kb.eligibility import KbUseEligibility
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.ingestion import KnowledgeAction, KnowledgeWriteError
from bowhead_core.kb.knowledge_base_access import KNOWLEDGE_EDIT, KNOWLEDGE_USE, KnowledgeBaseSearch, KnowledgeCaller, KnowledgeLevels
from bowhead_core.kb.knowledge_base_store import (
    KnowledgeBaseConflict,
    KnowledgeBaseStore,
    default_knowledge_base_id,
    is_retention_abort,
)
from bowhead_core.kb.management_tools import input_model, management_schema, register_management_tools
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.store import KbStore
from bowhead_core.tool_registry import ToolRegistry

from bowhead_core import migrations

pytestmark = pytest.mark.integration
TEAM = "team_a"


@pytest.fixture(scope="module")
def files_schema(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("kb-management") / "kb_files.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "files"
    command.upgrade(config, "head")
    return path


@pytest.fixture
def path(tmp_path, files_schema) -> Path:
    target = tmp_path / "kb_files.db"
    with closing(sqlite3.connect(files_schema)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
    return target


def _values(artifact="art_1", *, scope="space", scope_id="ws_1", workspace="ws_1", user="u_mem"):
    return {
        "artifact_id": artifact,
        "filename": f"{artifact}.md",
        "mime_type": "text/markdown",
        "size_bytes": 1,
        "scope": scope,
        "scope_id": scope_id,
        "workspace_id": workspace,
        "team_id": TEAM,
        "uploaded_by": user,
    }


@pytest.fixture
def trashed(monkeypatch) -> set[str]:
    """Stand in for the runtime's 0238 retention function on a plain files database: these originals are trashed."""
    import bowhead_core.sqlite_domain_runtime as runtime

    unavailable: set[str] = set()
    original = runtime.DiagnosticConnection.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.create_function("bowhead_file_retention_unavailable_v1", 1, lambda artifact_id: int(artifact_id in unavailable))

    monkeypatch.setattr(runtime.DiagnosticConnection, "__init__", init)
    return unavailable


async def test_named_targets_join_receipts_and_must_be_live_in_the_same_home(path, trashed):
    files, bases = KbFilesStore(path), KnowledgeBaseStore(str(path))
    finance = await bases.create(
        team_id=TEAM, home="space", home_id="ws_1", name="Finance", description=None, audience="members", audience_level="use", owner_user_id="u_mem"
    )
    action = KnowledgeAction("upload:art_1", "kb_ui", "u_mem", TEAM, "ws_1")
    file_id, created, operation = await files.create_admitted(action=action, source_fingerprint="v1", knowledge_base_id=finance.id, **_values())
    assert created and (await files.get(file_id)).knowledge_base_id == finance.id
    receipt = await files.get_operation(operation)
    assert json.loads(receipt["destination_json"]) == {"scope": "space", "scope_id": "ws_1", "team_id": TEAM, "knowledge_base_id": finance.id}
    # The same action replays; naming another destination for the same original conflicts.
    assert (await files.create_admitted(action=action, source_fingerprint="v1", knowledge_base_id=finance.id, **_values()))[:2] == (file_id, False)
    with pytest.raises(KnowledgeWriteError, match="knowledge_destination_mismatch"):
        await files.create_admitted(action=KnowledgeAction("upload:other", "kb_ui", "u_mem", TEAM, "ws_1"), source_fingerprint="v1", **_values())
    # A knowledge base of another home, or a deleted one, takes no files.
    with pytest.raises(KnowledgeWriteError, match="knowledge_base_unavailable"):
        await files.create_admitted(
            action=KnowledgeAction("upload:art_2", "kb_ui", "u_mem", TEAM, "ws_2"),
            source_fingerprint="v1",
            knowledge_base_id=finance.id,
            **_values("art_2", scope_id="ws_2", workspace="ws_2"),
        )
    # The retention guard's abort is reported as the trashed original it is.
    trashed.add("art_3")
    with pytest.raises(KnowledgeWriteError, match="knowledge_source_in_trash"):
        await files.create_admitted(
            action=KnowledgeAction("upload:art_3", "kb_ui", "u_mem", TEAM, "ws_1"), source_fingerprint="v1", **_values("art_3")
        )
    assert is_retention_abort(sqlite3.IntegrityError("Restore the file before adding this reference"))
    # Deleting refuses while files remain, then takes the member rows with it.
    with pytest.raises(KnowledgeBaseConflict) as caught:
        await bases.delete(finance.id, expected_revision=finance.revision)
    assert (caught.value.code, caught.value.files) == ("knowledge_base_files_changed", 1)
    await files.soft_delete(file_id)
    await bases.apply_member_changes(finance, [("user", "u_mem2", "use")], added_by="u_mem")
    await bases.delete(finance.id, expected_revision=finance.revision)
    assert (await bases.get(finance.id)).deleted_at and await bases.members(finance.id) == []
    with pytest.raises(KnowledgeWriteError, match="knowledge_base_unavailable"):
        await files.create_admitted(
            action=KnowledgeAction("upload:art_4", "kb_ui", "u_mem", TEAM, "ws_1"),
            source_fingerprint="v1",
            knowledge_base_id=finance.id,
            **_values("art_4"),
        )


async def test_moves_and_settings_are_conditional_writes(path, trashed):
    files, bases = KbFilesStore(path), KnowledgeBaseStore(str(path))
    finance = await bases.create(
        team_id=TEAM, home="space", home_id="ws_1", name="Finance", description=None, audience="members", audience_level="use", owner_user_id="u_mem"
    )
    legal = await bases.create(
        team_id=TEAM, home="team", home_id=TEAM, name="Legal", description=None, audience="members", audience_level="use", owner_user_id="u_owner"
    )
    file_id, _, _ = await files.create_admitted(
        action=KnowledgeAction("upload:art_1", "kb_ui", "u_mem", TEAM, "ws_1"), source_fingerprint="v1", **_values()
    )
    default = default_knowledge_base_id(TEAM, "space", "ws_1")
    assert not await files.set_knowledge_base(file_id, finance.id, expected="kbase_stale")
    assert await files.set_knowledge_base(file_id, finance.id, expected=default)
    with pytest.raises(KnowledgeWriteError, match="knowledge_base_unavailable"):
        await files.set_knowledge_base(file_id, legal.id, expected=finance.id)  # another home: a scope change, not a metadata move
    await files.update_scope(file_id, "team", TEAM, workspace_id="", team_id=TEAM, knowledge_base_id=legal.id)
    moved = await files.get(file_id)
    assert (moved.scope, moved.knowledge_base_id) == ("team", legal.id)
    listed = await files.list_for_scope_filtered("team", TEAM, knowledge_base_id=legal.id, team_id=TEAM)
    assert [row.file_id for row in listed] == [file_id]
    assert await files.list_for_scope_filtered("team", TEAM, knowledge_base_id=default_knowledge_base_id(TEAM, "team", TEAM), team_id=TEAM) == []
    counts = await bases.file_counts(TEAM, [("team", TEAM), ("space", "ws_1")])
    assert counts[legal.id].files == 1 and finance.id not in counts
    renamed = await bases.update(finance, expected_revision=1, changes={"name": "Finance 2026"})
    assert (renamed.name, renamed.revision) == ("Finance 2026", 2)
    with pytest.raises(KnowledgeBaseConflict, match="knowledge_base_revision_conflict"):
        await bases.update(finance, expected_revision=1, changes={"name": "Lost update"})
    handed = await bases.transfer(legal, new_owner="u_mem2", expected_revision=1, keep_previous_owner=True, added_by="u_owner")
    assert handed.owner_user_id == "u_mem2"
    assert [(row.principal_id, row.level) for row in await bases.members(legal.id)] == [("u_owner", "manage")]


async def test_switch_off_levels_hide_named_knowledge_bases_and_ignore_entries(path):
    bases = KnowledgeBaseStore(str(path))
    finance = await bases.create(
        team_id=TEAM, home="space", home_id="ws_1", name="Finance", description=None, audience="space", audience_level="edit", owner_user_id="u_mem"
    )
    team_default = default_knowledge_base_id(TEAM, "team", TEAM)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,audience,audience_level,created_at,updated_at) VALUES (?,?,'team',?,1,'team','use','t','t')",
            (team_default, TEAM, TEAM),
        )
        db.execute("INSERT INTO kb_knowledge_base_members VALUES (?,'user','u_x','manage','u_owner','t')", (team_default,))
        db.commit()
    caller = KnowledgeCaller(user_id="u_x", team_id=TEAM, space_id="ws_1", space_role="member")
    files = KbFilesStore(path)
    on = await files.resolve_knowledge_levels(caller)
    assert on[finance.id] == KNOWLEDGE_EDIT and on[team_default] == 3
    assert on.named_in("space", "ws_1") == (finance.id,) and on.is_default(team_default)
    off = await files.resolve_knowledge_levels(caller, named=False)
    assert finance.id not in off and off.excluded_in("space", "ws_1") == (finance.id,)
    assert off[team_default] == KNOWLEDGE_USE  # entries raise nobody while the switch is off


async def test_a_knowledge_base_deep_search_keeps_its_own_chunks(tmp_path):
    store = KbStore(str(tmp_path / "chroma"))
    default = default_knowledge_base_id(TEAM, "space", "ws_1")

    async def index(file_id, knowledge_base_id, seed):
        file = SimpleNamespace(
            file_id=file_id, scope="space", scope_id="ws_1", workspace_id="ws_1", team_id=TEAM, knowledge_base_id=knowledge_base_id
        )
        metadata = ChunkMetadata(
            file_id=file_id,
            file_name=file_id,
            scope="space",
            scope_owner="ws_1",
            workspace_id="ws_1",
            team_id=TEAM,
            chunk_index=1,
            knowledge_base_id=knowledge_base_id,
        )
        chunk = KbChunk(
            chunk_id=make_chunk_id(file_id, 1, file_id),
            file_id=file_id,
            content=file_id,
            embedding=[(seed + i) % 5 / 5 for i in range(8)],
            metadata=metadata,
        )
        await store.add_chunks(file, [chunk])

    await index("kbf_named", "kbase_finance", 1)
    await index("kbf_default", default, 2)
    await index("kbf_old", "", 3)  # written before chunks carried the key: it belongs to the default
    levels = KnowledgeLevels(
        {"kbase_finance": 3, default: 2}, {"kbase_finance": ("space", "ws_1"), default: ("space", "ws_1")}, defaults=frozenset({default})
    )

    async def found(selected):
        ctx = ScopeContext(user_id="u_mem", workspace_id="ws_1", team_id=TEAM, knowledge_levels=levels, knowledge_base_search=selected)
        return {hit["metadata"]["file_id"] for hit in await store.search(ctx, [0.5] * 8, top_k=10)}

    assert await found(None) == {"kbf_named", "kbf_default", "kbf_old"}
    assert await found(KnowledgeBaseSearch(knowledge_base_id="kbase_finance", scope="space", only="kbase_finance")) == {"kbf_named"}
    assert await found(KnowledgeBaseSearch(knowledge_base_id=default, scope="space", excluded=("kbase_finance",))) == {"kbf_default", "kbf_old"}
    assert await found(KnowledgeBaseSearch(knowledge_base_id="kbase_mine", scope="personal", only="kbase_mine")) == set()


async def test_source_checks_resolve_levels_once_for_named_files_only(path):
    calls: list[str] = []

    async def resolve():
        calls.append("levels")
        return KnowledgeLevels({"kbase_finance": KNOWLEDGE_USE}, {"kbase_finance": ("space", "ws_1")})

    named = SimpleNamespace(
        file_id="kbf_n",
        scope="space",
        scope_id="ws_1",
        team_id=TEAM,
        soft_deleted_at=None,
        knowledge_base_id="kbase_finance",
        active_generation_id=None,
    )
    seen = []

    class _Files:
        _db_path = str(path)

        async def get(self, _file_id):
            return named

    async def authorize(file):
        seen.append(file.file_id)
        return True

    scope = ScopeContext(user_id="u_mem", workspace_id="ws_1", team_id=TEAM)
    without = await KbUseEligibility(_Files(), None, None).check("kbf_n", scope, authorize)
    assert without.reason == "permission_or_file_unavailable" and seen == []  # fail-closed without levels
    with_levels = await KbUseEligibility(_Files(), None, None).check("kbf_n", scope, authorize, resolve_levels=resolve)
    # Past the permission stage: the missing generation is what stops it now.
    assert with_levels.reason == "generation_unverified" and calls == ["levels"] and seen == ["kbf_n"]


async def test_call_sites_that_refused_named_files_now_resolve_the_callers_levels(path, monkeypatch):
    """Slice 2 left the use guard, matrix source checks and the source-hint preview refusing them (the scan: gateway tests)."""
    from unittest.mock import AsyncMock

    from bowhead_core.kb.eligibility import KbUseDecision
    from bowhead_core.kb.matrix_engine import KbMatrixEngine
    from bowhead_core.kb.turn_guard import recheck_turn_sources
    from bowhead_core.knowledge_source_hints import _preview

    levels = KnowledgeLevels({"kbase_finance": KNOWLEDGE_USE}, {"kbase_finance": ("space", "ws_1")})
    resolved: dict[str, object] = {}

    async def check(self, file_id, scope, authorize, *, expected_generation=None, resolve_levels=None):
        resolved[file_id] = await resolve_levels() if resolve_levels is not None else None
        return KbUseDecision("eligible", "verified_binding", expected_generation, "t")

    monkeypatch.setattr(KbUseEligibility, "check", check)
    monkeypatch.setenv("FEATURE_KB_TRUST_V2", "1")
    monkeypatch.setenv("FEATURE_KB_USE_GUARD", "1")
    authorizer = AsyncMock(return_value=True)
    authorizer.knowledge_levels = AsyncMock(return_value=levels)
    ctx = SimpleNamespace(
        user_id="u_mem",
        team_id=TEAM,
        workspace_id="ws_1",
        language_context=None,
        kb_source_uses={"kbf_turn": {"file_id": "kbf_turn", "generation_id": "gen_1", "check_state": "verified_binding"}},
        kb_files_store=KbFilesStore(path),
        kb_retriever=SimpleNamespace(_store=None),
        artifact_store=None,
        kb_file_authorizer=authorizer,
    )
    assert (await recheck_turn_sources(ctx))[0] is True
    assert resolved["kbf_turn"] is levels and authorizer.knowledge_levels.await_args.args == ("u_mem", TEAM, "ws_1")

    engine = KbMatrixEngine.__new__(KbMatrixEngine)
    engine._file_authorizer, engine._files_store, engine._artifact_store = authorizer, KbFilesStore(path), None
    await engine._source_check("kbf_matrix", "u_mem", TEAM, "ws_1", None)
    assert resolved["kbf_matrix"] is levels

    named = SimpleNamespace(
        file_id="kbf_n",
        scope="space",
        scope_id="ws_1",
        workspace_id="ws_1",
        team_id=TEAM,
        knowledge_base_id="kbase_finance",
        active_generation_id="gen_1",
    )
    seen = []

    async def preview_source(file, caller, authorize):
        seen.append(caller.knowledge_levels)
        return []

    await _preview(SimpleNamespace(**{**vars(ctx), "kb_retriever": SimpleNamespace(preview_source=preview_source)}), named)
    assert seen == [levels]


def test_management_tools_name_a_knowledge_base_only_while_the_switch_is_on(monkeypatch):
    registry = ToolRegistry()
    register_management_tools(registry)
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "0")
    off = {schema.name: schema for schema in registry.to_schemas(["kb_add_file", "kb_remove_file"])}
    assert "knowledge_base_id" not in off["kb_add_file"].input_schema["properties"]
    assert input_model("kb_add_file").__name__ == "AddFile"
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "1")
    on = {schema.name: schema for schema in registry.to_schemas(["kb_add_file", "kb_remove_file", "kb_operation_status"])}
    assert "knowledge_base_id" in on["kb_add_file"].input_schema["properties"]
    assert "knowledge_base_id" in on["kb_remove_file"].input_schema["properties"]
    assert "knowledge_base_id" not in on["kb_operation_status"].input_schema.get("properties", {})
    assert management_schema("kb_search", on["kb_add_file"]) is on["kb_add_file"]
    with pytest.raises(ValueError):
        input_model("kb_add_file").model_validate({"artifact_id": "art_1", "scope": "space", "knowledge_base_id": "not-an-id"})
