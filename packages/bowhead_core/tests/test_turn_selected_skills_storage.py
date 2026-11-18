"""Selected Skills retained with an ordinary Turn's canonical source (migration 0229).

docs/sprints/skill-invocation-2026-11-17/tech-design.md §10.2. The column is
appended to the execution file after the domain split; the frozen legacy
single-file schema has no column and accepts only sources without Skills.
"""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.chat_turn_source import MAX_SELECTED_SKILLS, ChatTurnUserSource, chat_turn_user_source_projection
from bowhead_core.session_store import SessionStore
from bowhead_core.skill_read_closure import MAX_SELECTED_SKILLS as CLOSURE_MAX_SELECTED_SKILLS
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_core.sqlite_domains import APPENDED_COLUMNS
from test_chat_input_document_storage import NOW, _config

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0229_turn_selected_skills")
SKILL = SkillResourceRef(kind="skill", source_kind="user", id="sk_c029829564ae", version="sha256:" + "b" * 64)
TEXT = "/comprehensive-business-report 分析2026年1-10月的经营情况"


def _source(**overrides) -> ChatTurnUserSource:
    fields = {
        "turn_id": "t_1",
        "session_id": "s_1",
        "user_id": "u_1",
        "workspace_id": "ws_1",
        "user_text": TEXT,
        "attachment_ids": (),
        "attachment_refs": (),
        "parts": None,
        "selected_skills": (SKILL,),
    }
    return ChatTurnUserSource(**(fields | overrides))


def _legacy(path: Path) -> Path:
    command.upgrade(_config(path), SOURCE_REVISION)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at) VALUES(?,?,?,?,?)", ("s_1", "ws_1", "u_1", NOW, NOW))
    return path


def _migrated(path: Path) -> Path:
    _legacy(path)
    with sqlite3.connect(path) as db:
        for statement in MIGRATION._DDL:
            db.execute(statement)
    return path


async def _record(path: Path, source: ChatTurnUserSource) -> None:
    store = SessionStore(str(path))
    await store.record_turn(turn_id=source.turn_id, session_id=source.session_id, user_text=source.user_text, user_source=source)


async def _read(path: Path) -> ChatTurnUserSource | None:
    return await SessionStore(str(path)).get_chat_turn_user_source(turn_id="t_1", user_id="u_1", workspace_id="ws_1")


def test_bound_matches_the_skill_read_kernel_and_column_is_registered():
    assert MAX_SELECTED_SKILLS == CLOSURE_MAX_SELECTED_SKILLS
    assert APPENDED_COLUMNS["chat_turn_user_sources"] == (("selected_skills_json",), MIGRATION.revision)


@pytest.mark.asyncio
async def test_selected_skills_round_trip_and_replay_is_idempotent(tmp_path: Path):
    path = _migrated(tmp_path / "core.db")
    source = _source()
    await _record(path, source)
    assert await _read(path) == source
    await _record(path, source)  # exact replay
    with pytest.raises(ValueError, match="chat_turn_user_source_conflict"):
        await _record(path, _source(selected_skills=(SKILL.model_copy(update={"version": "sha256:" + "c" * 64}),)))


@pytest.mark.asyncio
async def test_sources_without_skills_keep_their_projection_and_column_stays_null(tmp_path: Path):
    path = _migrated(tmp_path / "core.db")
    source = _source(selected_skills=None)
    await _record(path, source)
    assert "selected_skills" not in chat_turn_user_source_projection(source)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT selected_skills_json FROM chat_turn_user_sources").fetchone() == (None,)


@pytest.mark.asyncio
async def test_frozen_legacy_schema_accepts_only_sources_without_skills(tmp_path: Path):
    path = _legacy(tmp_path / "core.db")
    with pytest.raises(sqlite3.OperationalError, match="selected_skills_json"):
        await _record(path, _source())
    await _record(path, _source(selected_skills=None))
    assert (await _read(path)).selected_skills is None


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        "[]",
        '[{"kind":"skill","source_kind":"user","id":"sk_a"}]',
        '[{"kind":"agent","source_kind":"user","id":"sk_a","version":"sha256:' + "a" * 64 + '"}]',
        '[{"kind":"skill","source_kind":"user","id":"sk_a","version":"md5:x"}]',
        '[{"kind":"skill","source_kind":"user","id":"sk_a","version":"sha256:' + "a" * 64 + '","extra":1}]',
        "[" + ",".join(f'{{"kind":"skill","source_kind":"user","id":"sk_{i}","version":"sha256:{"a" * 64}"}}' for i in range(4)) + "]",
    ],
)
def test_insert_guard_rejects_malformed_values(tmp_path: Path, value: str):
    path = _migrated(tmp_path / "core.db")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES(?,?,?,?,?)", ("t_1", "s_1", 1, TEXT, NOW))
        with pytest.raises(sqlite3.IntegrityError, match="chat_selected_skills_invalid"):
            db.execute(
                "INSERT INTO chat_turn_user_sources(turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,attachment_refs_json,created_at,selected_skills_json)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                ("t_1", "s_1", "u_1", "ws_1", TEXT, "[]", "[]", NOW, value),
            )


@pytest.mark.asyncio
async def test_selected_skills_are_immutable(tmp_path: Path):
    path = _migrated(tmp_path / "core.db")
    await _record(path, _source())
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError, match="chat_selected_skills_immutable"):
        db.execute("UPDATE chat_turn_user_sources SET selected_skills_json=NULL")


def test_source_rejects_combinations_and_repeats():
    with pytest.raises(ValueError):
        _source(selected_skills=())
    with pytest.raises(ValueError):
        _source(selected_skills=(SKILL, SKILL))
    with pytest.raises(ValueError):
        _source(selected_skills=(SKILL,) * (MAX_SELECTED_SKILLS + 1))
