"""Agents that read selected knowledge bases (knowledge base organization phase 1, slice 5).

The policy (schema 2) and its normalization, the scope's second ceiling and nesting, release
review expansion, the migration and switch gates, and the call-time runtime: files added to a
selected knowledge base later are found, files the invoker cannot use are not, and a default
knowledge base's old chunks without the key still match.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.agent_knowledge_policy import (
    MAX_SELECTED_KNOWLEDGE_BASES,
    KnowledgeAccessError,
    KnowledgeScope,
    bind_knowledge_scope,
    knowledge_access_expanded,
    require_knowledge_admission,
    require_supported_knowledge_access,
)
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.knowledge_base_access import KnowledgeCaller, knowledge_base_selection
from bowhead_core.kb.knowledge_base_store import KnowledgeBaseStore, default_knowledge_base_id
from bowhead_core.knowledge_source_runtime import (
    KNOWLEDGE_BASES_OFF_HINT,
    NO_USABLE_KNOWLEDGE_BASE_HINT,
    finish_source_read,
    prepare_source_read,
    readable_source,
)
from bowhead_core.tool_registry import build_default_registry
from test_kb_search_tool import _make_ctx

from bowhead_core import migrations

TEAM = "team_a"
KB_A = "kbase_" + "a" * 24
KB_B = "kbase_" + "b" * 24
KB_C = "kbase_" + "c" * 24


def _policy(**fields):
    return {"knowledge_access": {"schema_version": 2, "mode": "selected_knowledge_bases", **fields}}


def _read(**fields):
    return require_supported_knowledge_access(_policy(**fields))


# ─── Policy: schema 2 and normalization at the shared entry point ─────────────


def test_selected_knowledge_bases_are_canonical_sorted_and_versioned():
    policy = _read(knowledge_base_ids=[KB_B, KB_A], defaults={"top_k": 4})
    assert policy.canonical_value() == {
        "schema_version": 2,
        "mode": "selected_knowledge_bases",
        "knowledge_base_ids": [KB_A, KB_B],
        "defaults": {"top_k": 4},
    }
    assert policy.file_ids is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, []),  # omitted: an explicit empty selection, which reads nothing
        ({"knowledge_base_ids": None}, []),
        ({"knowledge_base_ids": KB_A}, [KB_A]),  # one ID
        ({"knowledge_base_ids": [f" {KB_A} ", KB_A, KB_B, KB_A]}, [KB_A, KB_B]),  # stripped, deduplicated
        ({"knowledge_base_ids": [KB_A], "file_ids": None}, [KB_A]),  # an unusable empty field is dropped
        ({"knowledge_base_ids": [KB_A], "file_ids": []}, [KB_A]),
    ],
    ids=["omitted", "null", "single", "duplicates", "null-files", "empty-files"],
)
def test_equivalent_forms_normalize_into_the_narrow_policy(raw, expected):
    assert _read(**raw).canonical_value()["knowledge_base_ids"] == expected


@pytest.mark.parametrize("value", [None, []], ids=["null", "empty"])
def test_an_empty_knowledge_base_field_on_another_mode_is_dropped(value):
    policy = require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "context_readable", "knowledge_base_ids": value}})
    assert policy.canonical_value() == {"schema_version": 1, "mode": "context_readable"}


@pytest.mark.parametrize(
    "ids",
    [
        ["kbase_" + format(index, "024x") for index in range(MAX_SELECTED_KNOWLEDGE_BASES + 1)],
        ["kbase_short"],
        ["kbf_aaaaaaaaaaaa"],
        [7],
        [{"id": KB_A}],
        {"id": KB_A},
    ],
    ids=["over-limit", "short-id", "file-id", "number", "object-item", "object"],
)
def test_a_malformed_selection_names_its_field(ids):
    with pytest.raises(KnowledgeAccessError) as caught:
        _read(knowledge_base_ids=ids)
    assert caught.value.code == "agent_knowledge_access_invalid"
    assert caught.value.details["field"] == "knowledge_access.knowledge_base_ids"
    assert "32" in caught.value.details["next_step"]


@pytest.mark.parametrize(
    "policy",
    [
        {"schema_version": 1, "mode": "selected_knowledge_bases", "knowledge_base_ids": [KB_A]},
        {"schema_version": 2, "mode": "selected_files", "file_ids": ["kbf_a"]},
        {"schema_version": 2, "mode": "context_readable"},
        {"schema_version": 3, "mode": "selected_knowledge_bases", "knowledge_base_ids": [KB_A]},
        {"schema_version": 2, "mode": "selected_knowledge_bases", "knowledge_base_ids": [KB_A], "file_ids": ["kbf_a"]},
        {"schema_version": 1, "mode": "context_readable", "knowledge_base_ids": [KB_A]},
        {"schema_version": 1, "mode": "selected_files", "file_ids": [], "knowledge_base_ids": [KB_A]},
    ],
    ids=["v1-mode", "v2-files", "v2-context", "future", "both-selections", "context-with-bases", "files-with-bases"],
)
def test_ambiguous_or_mismatched_policies_are_refused_never_widened(policy):
    with pytest.raises(KnowledgeAccessError, match="agent_knowledge_access_invalid"):
        require_supported_knowledge_access({"knowledge_access": policy})


# ─── Fingerprints of existing policies and scopes stay the same ───────────────

# Computed with the code before schema 2 (origin/main c02369547c).
_POLICY_FINGERPRINTS = [
    ({"schema_version": 1, "mode": "context_readable"}, "sha256:07b2d33d502a3e2620f9fcb98b02aa20f0e4ba96b02e6cc6d1ea0aa34dfaa17e"),
    (
        {"schema_version": 1, "mode": "context_readable", "defaults": {"top_k": 3}},
        "sha256:29ea41a801b9cfb78107b19d67710450e27e7c248497f8ccc8c33b43d2095813",
    ),
    ({"schema_version": 1, "mode": "selected_files", "file_ids": []}, "sha256:c286d0e9192186d69fba74bf58cfcff410bd1c86bda85adfe0cb6efbe8b76a7f"),
    (
        {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_private"]},
        "sha256:3ebd8f49a7a1b5c3884735c6275c91939d9d3c151d3adfaee9d08047539bf908",
    ),
    (
        {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_b", "kbf_a"], "defaults": {"top_k": 7}},
        "sha256:17d123d014b72ced6da6d89bb48f0ef525760fbcc498ab48d42760338f3be9ee",
    ),
]
_SCOPE_FINGERPRINTS = [
    ({"file_ids": None, "bindings": ()}, "sha256:1725a452c2e3dab7a0d8002c3d00df5b8adf12e6a2528ca44c217f2bd5ebdb8b"),
    (
        {"file_ids": ("kbf_a",), "bindings": (("rev_1", "sha256:x"),), "top_k": 4},
        "sha256:62fd408af416441fac157d20c1a010d6146b1cf14e2ae441eb0dcca5a0c76cd0",
    ),
    ({"file_ids": (), "bindings": (("rev_1", "sha256:x"),)}, "sha256:e65abc11213d19e5ce936ffb3973769439dbd45da3aa93ebbb9b2b9c5165cca2"),
]


@pytest.mark.parametrize(("policy", "expected"), _POLICY_FINGERPRINTS, ids=lambda value: str(value)[:40])
def test_schema_1_policy_fingerprints_are_unchanged(policy, expected):
    assert require_supported_knowledge_access({"knowledge_access": policy}).fingerprint == expected


@pytest.mark.parametrize(("fields", "expected"), _SCOPE_FINGERPRINTS, ids=["context", "files", "empty"])
def test_scopes_without_a_knowledge_base_ceiling_keep_their_fingerprint_and_dump(fields, expected):
    scope = KnowledgeScope(schema_version=1, user_id="user_a", team_id="team_a", workspace_id="ws_a", **fields)
    assert scope.fingerprint == expected
    assert "knowledge_base_ids" not in scope.model_dump(mode="json")
    assert KnowledgeScope.model_validate_json(json.dumps(scope.model_dump(mode="json"))) == scope


def test_a_knowledge_base_ceiling_is_schema_2_and_survives_a_round_trip():
    scope = KnowledgeScope(schema_version=2, user_id="u", team_id="t", workspace_id="w", file_ids=None, bindings=(), knowledge_base_ids=(KB_A,))
    assert scope.model_dump(mode="json")["knowledge_base_ids"] == [KB_A]
    assert KnowledgeScope.model_validate_json(json.dumps(scope.model_dump(mode="json"))) == scope
    assert scope.fingerprint != KnowledgeScope(schema_version=1, user_id="u", team_id="t", workspace_id="w", file_ids=None, bindings=()).fingerprint
    with pytest.raises(ValueError):
        KnowledgeScope(schema_version=1, user_id="u", team_id="t", workspace_id="w", file_ids=None, bindings=(), knowledge_base_ids=(KB_A,))


# ─── permits and nesting ─────────────────────────────────────────────────────


def _file(file_id="kbf_one", *, scope="space", scope_id="ws_1", knowledge_base_id=None):
    return SimpleNamespace(file_id=file_id, scope=scope, scope_id=scope_id, workspace_id=scope_id, team_id=TEAM, knowledge_base_id=knowledge_base_id)


def _ctx(scope=None):
    return SimpleNamespace(user_id="u_mem", team_id=TEAM, workspace_id="ws_1", knowledge_scope=scope)


def test_permits_checks_both_ceilings_and_reads_a_missing_key_as_the_default():
    space_default = default_knowledge_base_id(TEAM, "space", "ws_1")
    parent = bind_knowledge_scope(
        _ctx(),
        require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_one", "kbf_two"]}}),
        revision="arv_parent",
    )
    scope = bind_knowledge_scope(_ctx(parent), _read(knowledge_base_ids=[KB_A, space_default]), revision="arv_child")
    assert scope.schema_version == 2 and scope.file_ids == ("kbf_one", "kbf_two") and scope.knowledge_base_ids == tuple(sorted([KB_A, space_default]))
    assert scope.permits(_file(knowledge_base_id=KB_A))
    assert scope.permits(_file())  # no stored key: the scope's default, which is selected
    assert not scope.permits(_file(knowledge_base_id=KB_B))
    assert not scope.permits(_file("kbf_three", knowledge_base_id=KB_A))  # outside the file ceiling
    assert not scope.permits(_file(scope="team", scope_id=TEAM))  # the Team default is not selected
    assert not scope.permits("kbf_one")  # a bare ID cannot show its knowledge base
    assert scope.permits_file_id("kbf_one")


def test_nesting_intersects_each_ceiling_separately():
    parent = bind_knowledge_scope(_ctx(), _read(knowledge_base_ids=[KB_A, KB_B]), revision="arv_parent")
    child = bind_knowledge_scope(_ctx(parent), _read(knowledge_base_ids=[KB_B, KB_C]), revision="arv_child")
    assert child.knowledge_base_ids == (KB_B,) and child.file_ids is None
    context = require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "context_readable"}})
    inherited = bind_knowledge_scope(_ctx(parent), context, revision="arv_skill")
    assert inherited.knowledge_base_ids == (KB_A, KB_B) and inherited.schema_version == 2
    files = require_supported_knowledge_access({"knowledge_access": {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_one"]}})
    both = bind_knowledge_scope(_ctx(parent), files, revision="arv_files")
    assert (both.file_ids, both.knowledge_base_ids) == (("kbf_one",), (KB_A, KB_B))
    assert parent.knowledge_base_ids == (KB_A, KB_B)  # the parent is never changed
    # A resumed helper keeps only what both its retained scope and the current one allow.
    assert child.narrowed_by(both).knowledge_base_ids == (KB_B,)
    assert child.narrowed_by(both).file_ids == ("kbf_one",)
    unrestricted = KnowledgeScope(schema_version=1, user_id="u_mem", team_id=TEAM, workspace_id="ws_1", file_ids=None, bindings=())
    assert unrestricted.narrowed_by(parent).knowledge_base_ids == (KB_A, KB_B)
    assert unrestricted.narrowed_by(None) == unrestricted


# ─── Release review ──────────────────────────────────────────────────────────

_CONTEXT = {"schema_version": 1, "mode": "context_readable"}
_FILES = {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_one"]}


@pytest.mark.parametrize(
    ("before", "after", "expanded"),
    [
        (_FILES, _policy(knowledge_base_ids=[KB_A])["knowledge_access"], True),  # files added later are read
        (_policy(knowledge_base_ids=[KB_A])["knowledge_access"], _policy(knowledge_base_ids=[KB_A, KB_B])["knowledge_access"], True),
        (_policy(knowledge_base_ids=[KB_A, KB_B])["knowledge_access"], _policy(knowledge_base_ids=[KB_A])["knowledge_access"], False),
        (_policy(knowledge_base_ids=[KB_A])["knowledge_access"], _CONTEXT, True),
        (_policy(knowledge_base_ids=[KB_A])["knowledge_access"], _FILES, True),
        (_policy(knowledge_base_ids=[KB_A])["knowledge_access"], None, True),
        (_CONTEXT, _policy(knowledge_base_ids=[KB_A])["knowledge_access"], False),  # a selection narrows
        (
            _policy(knowledge_base_ids=[KB_A])["knowledge_access"],
            _policy(knowledge_base_ids=[KB_A], defaults={"top_k": 3})["knowledge_access"],
            False,
        ),
    ],
    ids=["files-to-bases", "add", "remove", "to-context", "to-files", "removed-policy", "context-to-bases", "top-k"],
)
def test_release_review_marks_wider_knowledge_bases_as_an_expansion(before, after, expanded):
    candidate = {} if after is None else {"knowledge_access": after}
    assert knowledge_access_expanded({"knowledge_access": before}, candidate) is expanded


# ─── Migration and switch gates ──────────────────────────────────────────────


async def _db_at(revision: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute("CREATE TABLE alembic_version (version_num TEXT)")
    await db.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    return db


async def test_knowledge_bases_require_revision_0239_and_the_switch(monkeypatch):
    revision = importlib.import_module("bowhead_core.migrations.versions.0239_knowledge_bases")
    selected = _read(knowledge_base_ids=[KB_A])
    files = require_supported_knowledge_access({"knowledge_access": _FILES})
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "1")
    before = await _db_at(revision.down_revision)
    try:
        await require_knowledge_admission(before, files)  # schema 1 needs only 0199
        with pytest.raises(KnowledgeAccessError, match="agent_knowledge_access_migration_required"):
            await require_knowledge_admission(before, selected)
    finally:
        await before.close()
    current = await _db_at(revision.revision)
    try:
        await require_knowledge_admission(current, selected)
        monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "0")
        with pytest.raises(KnowledgeAccessError) as caught:
            await require_knowledge_admission(current, selected)
        assert caught.value.code == "agent_knowledge_bases_disabled"
        assert caught.value.details["field"] == "knowledge_access.mode" and "administrator" in caught.value.details["next_step"]
        await require_knowledge_admission(current, files)  # other modes are unaffected
    finally:
        await current.close()


# ─── Runtime: call-time listing on a real files database ─────────────────────


@pytest.fixture(scope="module")
def files_schema(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("agent-knowledge-bases") / "kb_files.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "files"
    command.upgrade(config, "head")
    return path


@pytest.fixture
def files_db(tmp_path, files_schema, monkeypatch) -> Path:
    import bowhead_core.sqlite_domain_runtime as runtime

    target = tmp_path / "kb_files.db"
    with closing(sqlite3.connect(files_schema)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
    original = runtime.DiagnosticConnection.__init__

    def init(self, *args, **kwargs):
        # The runtime's 0238 retention function, on a plain files database: nothing is trashed.
        original(self, *args, **kwargs)
        self.create_function("bowhead_file_retention_unavailable_v1", 1, lambda _artifact_id: 0)

    monkeypatch.setattr(runtime.DiagnosticConnection, "__init__", init)
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "1")
    return target


class _Authorizer:
    """The gateway authorizer's shape over the real resolver; members of ws_1 only, every listed file allowed."""

    def __init__(self, files: KbFilesStore) -> None:
        self.files = files
        self.batches = 0

    async def __call__(self, file, user_id, team_id, workspace_id, **_options):
        return True

    async def batch(self, files, user_id, team_id, workspace_id, **_options):
        self.batches += 1
        return {file.file_id: True for file in files}

    async def knowledge_levels(self, user_id, team_id, workspace_id):
        role = "member" if workspace_id == "ws_1" else ""
        return await self.files.resolve_knowledge_levels(KnowledgeCaller(user_id=user_id, team_id=team_id, space_id=workspace_id, space_role=role))


async def _add(files: KbFilesStore, name: str, *, scope="space", scope_id="ws_1", knowledge_base_id=None) -> str:
    file_id = await files.create(
        artifact_id=f"art_{name}",
        filename=f"{name}.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope=scope,
        scope_id=scope_id,
        workspace_id="" if scope == "team" else scope_id,
        team_id=TEAM,
        uploaded_by="u_owner",
        initial_parse_status="done",
    )
    if knowledge_base_id:
        assert await files.set_knowledge_base(
            file_id, knowledge_base_id, expected=default_knowledge_base_id(TEAM, "space" if scope == "space" else scope, scope_id)
        )
    return file_id


async def _library(files_db: Path):
    files, bases = KbFilesStore(files_db), KnowledgeBaseStore(str(files_db))
    shared = await bases.create(
        team_id=TEAM,
        home="space",
        home_id="ws_1",
        name="Store manual",
        description=None,
        audience="space",
        audience_level="use",
        owner_user_id="u_owner",
    )
    secret = await bases.create(
        team_id=TEAM, home="space", home_id="ws_1", name="Board", description=None, audience="members", audience_level="use", owner_user_id="u_owner"
    )
    ids = {
        "manual": await _add(files, "manual", knowledge_base_id=shared.id),
        "board": await _add(files, "board", knowledge_base_id=secret.id),
        "space": await _add(files, "space"),
        "team": await _add(files, "team", scope="team", scope_id=TEAM),
    }
    return files, shared, secret, ids


def _agent_ctx(files: KbFilesStore, knowledge_base_ids, *, user="u_mem", workspace="ws_1"):
    ctx = _make_ctx(kb_files_store=files, kb_file_authorizer=_Authorizer(files), user_id=user, workspace_id=workspace, team_id=TEAM)
    ctx.knowledge_scope = bind_knowledge_scope(ctx, _read(knowledge_base_ids=list(knowledge_base_ids)), revision="arv_agent")
    return ctx


async def test_files_added_later_are_found_and_files_the_invoker_cannot_use_are_not(files_db):
    files, shared, secret, ids = await _library(files_db)
    ctx = _agent_ctx(files, [shared.id, secret.id])
    prepared = await prepare_source_read(ctx, "kb_query_records")
    assert prepared.knowledge_readable_file_ids == (ids["manual"],)  # the members-only one is not usable
    assert prepared.knowledge_base_filter == (shared.id,) and prepared.knowledge_ceiling_hint is None
    assert ctx.kb_file_authorizer.batches == 1  # one batch for every listed file
    later = await _add(files, "later", knowledge_base_id=shared.id)  # added after the Agent was saved
    again = await prepare_source_read(ctx, "kb_query_records")
    assert set(again.knowledge_readable_file_ids) == {ids["manual"], later}
    assert await readable_source(ctx, later) and not await readable_source(ctx, ids["space"])
    # The owner, who can use both, reads both; the selection never adds the default's files.
    owner = await prepare_source_read(_agent_ctx(files, [shared.id, secret.id], user="u_owner"), "kb_list_tables")
    assert set(owner.knowledge_readable_file_ids) == {ids["manual"], ids["board"], later}


async def test_a_selected_default_takes_old_files_without_a_key_but_not_named_ones(files_db):
    files, shared, _secret, ids = await _library(files_db)
    legacy = await _add(files, "legacy")
    with closing(sqlite3.connect(files_db)) as db:
        db.execute("UPDATE kb_files SET knowledge_base_id=NULL WHERE file_id=?", (legacy,))
        db.commit()
    space_default = default_knowledge_base_id(TEAM, "space", "ws_1")
    prepared = await prepare_source_read(_agent_ctx(files, [space_default]), "kb_get_record")
    assert set(prepared.knowledge_readable_file_ids) == {ids["space"], legacy}
    search = await prepare_source_read(_agent_ctx(files, [space_default, shared.id]), "kb_search")
    assert search.knowledge_readable_file_ids is None
    space = search.knowledge_base_selection.space
    # The default matches the home's chunks except those of its unselected named knowledge bases.
    assert space.default and space.excluded == tuple(sorted({_secret.id}))
    assert search.knowledge_base_selection.team is None and search.knowledge_base_selection.personal is None


async def test_a_space_knowledge_base_applies_only_in_its_space_and_my_knowledge_only_to_me(files_db):
    files, shared, _secret, _ids = await _library(files_db)
    elsewhere = await prepare_source_read(_agent_ctx(files, [shared.id], workspace="ws_2"), "kb_query_records")
    assert elsewhere.knowledge_readable_file_ids == () and elsewhere.knowledge_ceiling_hint == NO_USABLE_KNOWLEDGE_BASE_HINT
    mine = default_knowledge_base_id(TEAM, "personal", "u_owner")
    other = await prepare_source_read(_agent_ctx(files, [mine], user="u_mem"), "kb_search")
    assert other.knowledge_readable_file_ids == ()
    other.kb_retriever = SimpleNamespace(invoke=AsyncMock(side_effect=AssertionError("nothing is searched")), default_top_k=5)
    result = await build_default_registry()._specs["kb_search"].handler({"query": "opening"}, other)
    assert result["chunks"] == [] and result["hint"] == NO_USABLE_KNOWLEDGE_BASE_HINT


async def test_switch_off_reads_nothing_and_says_why(files_db, monkeypatch):
    files, shared, _secret, _ids = await _library(files_db)
    ctx = _agent_ctx(files, [shared.id])
    monkeypatch.setenv("FEATURE_KNOWLEDGE_BASES", "0")
    for tool in ("kb_search", "kb_query_records", "kb_list_sources"):
        prepared = await prepare_source_read(ctx, tool)
        assert prepared.knowledge_readable_file_ids == () and prepared.knowledge_ceiling_hint == KNOWLEDGE_BASES_OFF_HINT
    retriever = SimpleNamespace(invoke=AsyncMock(side_effect=AssertionError("nothing is searched")), default_top_k=5)
    prepared = await prepare_source_read(ctx, "kb_search")
    prepared.kb_retriever = retriever
    result = await build_default_registry()._specs["kb_search"].handler({"query": "opening hours"}, prepared)
    assert result["chunks"] == [] and result["hint"] == KNOWLEDGE_BASES_OFF_HINT


async def test_kb_search_pre_filters_by_chunk_key_and_post_filters_by_the_current_row(files_db):
    from bowhead_core.kb.schemas import KbSearchResult

    files, shared, _secret, ids = await _library(files_db)
    ctx = _agent_ctx(files, [shared.id])
    retriever = SimpleNamespace(
        invoke=AsyncMock(return_value=KbSearchResult(chunks=[], total_candidates=0, rerank_used=False, latency_ms=0)), default_top_k=5
    )
    prepared = await prepare_source_read(ctx, "kb_search")
    prepared.kb_retriever = retriever
    await build_default_registry()._specs["kb_search"].handler({"query": "opening hours"}, prepared)
    call = retriever.invoke.await_args.kwargs
    assert call["scope_ctx"].knowledge_base_selection.space.only == (shared.id,)
    assert call["allowed_file_ids"] is None and call["enforce_source_access"] is True
    permit = call["permit_file"]
    assert permit(await files.get(ids["manual"])) and not permit(await files.get(ids["space"]))
    # Delivery re-checks each chunk's file against its current knowledge base.
    moved = await files.get(ids["manual"])
    assert await files.set_knowledge_base(moved.file_id, default_knowledge_base_id(TEAM, "space", "ws_1"), expected=shared.id)
    delivered = await finish_source_read(prepared, "kb_search", {"chunks": [{"file_id": ids["manual"], "generation_id": None, "content": "x"}]})
    assert delivered["chunks"] == [] and delivered["partial"] is True


def test_the_selection_lists_each_home_once():
    from bowhead_core.kb.knowledge_base_access import KnowledgeLevels

    space_default = default_knowledge_base_id(TEAM, "space", "ws_1")
    team_default = default_knowledge_base_id(TEAM, "team", TEAM)
    homes = {KB_A: ("space", "ws_1"), KB_B: ("space", "ws_1"), KB_C: ("team", TEAM), space_default: ("space", "ws_1"), team_default: ("team", TEAM)}
    levels = KnowledgeLevels(
        {KB_A: 1, KB_C: 1, space_default: 2, team_default: 1}, homes, frozenset({KB_B}), frozenset({space_default, team_default})
    )
    selection = knowledge_base_selection(levels, [KB_A, space_default, KB_C], user_id="u_mem", space_id="ws_1", team_id=TEAM)
    assert (selection.space.default, selection.space.excluded) == (True, (KB_B,))
    assert (selection.team.default, selection.team.only) == (False, (KB_C,))
    assert selection.personal is None
