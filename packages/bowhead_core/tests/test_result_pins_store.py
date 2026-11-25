"""Result pins: revision 0232, the store, and the artifact lookups the sidebar list relies on.

docs/sprints/result-pins-2026-11-24/tech-design.md §3–§4. One migrated domain layout is built per module
and copied for each test, so every case runs on the real execution/content split.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES, domain_database_path
from bowhead_core.user_store import UserStore

_FERNET = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory) -> Path:
    from bowhead_cli.migrate import upgrade

    core = tmp_path_factory.mktemp("result-pins-template") / "sqlite" / "bowhead.db"
    upgrade(f"sqlite:///{core}")
    return core.parent


@pytest.fixture
async def ctx(tmp_path, migrated_template, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", _FERNET)
    directory = tmp_path / "sqlite"
    shutil.copytree(migrated_template, directory, ignore=shutil.ignore_patterns("*.lock"))
    core = directory / "bowhead.db"
    users = UserStore(str(core))
    await users.initialize()
    team = await users.create_team(name="Pins", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="pins-store@example.com", password="Passw0rd!")
    ws = await users.create_local_workspace(team_id=team.id, name="Pins", creator_user_id=owner.id)
    sessions = SessionStore(str(core))
    await sessions.initialize()
    session = await sessions.ensure_session(session_id=None, workspace_id=ws, user_id=owner.id)
    await sessions.record_turn(turn_id="t_pinstore0001", session_id=session.id, user_text="make a deck")
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "files"), db_path=str(core))
    await artifacts.initialize()
    return SimpleNamespace(core=core, users=users, sessions=sessions, artifacts=artifacts, owner=owner.id, ws=ws, session_id=session.id)


async def _deck(c, content: bytes, **kw):
    return await c.artifacts.put_bytes(
        content=content,
        filename="deck.pptx",
        mime=_PPTX,
        source="sandbox_output",
        created_by=c.owner,
        session_id=c.session_id,
        kind="deliverable",
        created_by_turn="t_pinstore0001",
        workspace_id=c.ws,
        **kw,
    )


def test_fresh_upgrade_adds_the_table_to_the_execution_file_only(migrated_template) -> None:
    from bowhead_core.account_erasure import _POLICY

    for domain, filename in DOMAIN_TARGET_FILES.items():
        with closing(sqlite3.connect(migrated_template / filename)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert ("user_result_pins" in tables) is (domain == "execution"), domain
    with closing(sqlite3.connect(migrated_template / DOMAIN_TARGET_FILES["execution"])) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(user_result_pins)")]
        objects = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE tbl_name='user_result_pins'")}
    # Account erasure reviews the exact column list.
    assert columns == _POLICY["tables"]["user_result_pins"]["columns"]
    assert {"idx_user_result_pins_target", "trg_account_retired_user_result_pins_insert"} <= objects


async def test_a_retired_account_cannot_receive_a_pin(ctx) -> None:
    from bowhead_core.sqlite_domain_runtime import connect_sync

    with closing(connect_sync(str(ctx.core))) as db:
        db.execute(
            "INSERT INTO account_deletions(user_id,operation_id,requested_at,purge_after,time_source) VALUES('u_gone','op_1','t','2099','request')"
        )
        db.commit()
    with (
        closing(connect_sync(domain_database_path(str(ctx.core), "execution"))) as db,
        pytest.raises(sqlite3.DatabaseError, match="account is deleted"),
    ):
        db.execute("INSERT INTO user_result_pins(user_id,target_kind,target_id,pinned_at) VALUES('u_gone','file','art_x','t')")


async def test_the_frozen_legacy_layout_has_no_pins(tmp_path) -> None:
    store = SessionStore(tmp_path / "legacy.db")
    await store.initialize()
    assert not await store.result_pins_supported()
    assert await store.list_result_pins(user_id="u") == []
    assert await store.result_pin_targets(target_kind="file") == []
    assert await store.delete_result_pins(target_kind="file", target_ids=["art_x"]) == 0


async def test_pin_is_idempotent_and_unpin_removes_it(ctx) -> None:
    store = ctx.sessions
    assert await store.result_pins_supported()
    await store.pin_result(user_id=ctx.owner, target_kind="file", target_id="art_a")
    first = await store.list_result_pins(user_id=ctx.owner)
    await store.pin_result(user_id=ctx.owner, target_kind="file", target_id="art_a")
    await store.pin_result(user_id=ctx.owner, target_kind="report", target_id="cv_a")
    rows = await store.list_result_pins(user_id=ctx.owner)
    assert first[0] in rows
    assert sorted((kind, target) for kind, target, _ in rows) == [("file", "art_a"), ("report", "cv_a")]
    assert await store.result_pin_targets(target_kind="file") == ["art_a"]
    await store.unpin_result(user_id=ctx.owner, target_kind="file", target_id="art_a")
    assert [(kind, target) for kind, target, _ in await store.list_result_pins(user_id=ctx.owner)] == [("report", "cv_a")]
    assert await store.delete_result_pins(target_kind="report", target_ids=["cv_a", "cv_other"]) == 1
    assert await store.list_result_pins(user_id=ctx.owner) == []


async def test_newest_live_version_survives_pruning_of_the_first(ctx) -> None:
    versions = [await _deck(ctx, b"v1")]
    for n in range(2, 5):
        versions.append(await _deck(ctx, f"v{n}".encode(), version_of_target=versions[-1].id))
    root = versions[0].id
    other = await _deck(ctx, b"other")
    assert await ctx.artifacts.latest_live_versions([root, other.id, "art_missing"]) == {root: versions[-1].id, other.id: other.id}
    # Pruning waits for the conversation's running Turn; a pin does not stop it.
    await ctx.sessions.complete_turn(turn_id="t_pinstore0001", intent=None, final_answer="done", tokens_input=0, tokens_output=0, cost_usd=0.0)
    await ctx.sessions.pin_result(user_id=ctx.owner, target_kind="file", target_id=root)
    # Keep-newest-3 pruning removes version 1, whose ID is the lineage root the pin stores.
    assert await ctx.artifacts.prune_version_candidates([root]) == [root]
    assert await ctx.artifacts.prune_versions([root], kb_protected=set()) == 1
    assert await ctx.artifacts.latest_live_versions([root]) == {root: versions[-1].id}


async def test_purged_lineages_are_reported_gone_but_trashed_ones_are_not(ctx) -> None:
    kept = await _deck(ctx, b"kept")
    purged = await _deck(ctx, b"purged")
    await ctx.artifacts.soft_delete(purged.id, user_id=ctx.owner)
    await ctx.artifacts.hard_delete_old_soft_deleted(days=0)
    trashed = await _deck(ctx, b"trashed")
    await ctx.artifacts.soft_delete(trashed.id, user_id=ctx.owner)
    assert await ctx.artifacts.lineage_roots_without_rows([kept.id, trashed.id, purged.id]) == [purged.id]


async def test_conversation_ladder_covers_turns_attachments_and_upload_bindings(ctx) -> None:
    by_turn = await _deck(ctx, b"turn")
    attached = await ctx.artifacts.put_bytes(
        content=b"attached", filename="notes.txt", mime="text/plain", source="upload", created_by=ctx.owner, kind="user_upload", workspace_id=ctx.ws
    )
    await ctx.sessions.pin_session_attachments(session_id=ctx.session_id, artifact_ids=[attached.id])
    loose = await ctx.artifacts.put_bytes(
        content=b"loose", filename="loose.txt", mime="text/plain", source="upload", created_by=ctx.owner, kind="user_upload", workspace_id=ctx.ws
    )
    found = await ctx.artifacts.conversation_ids([by_turn.id, attached.id, loose.id])
    assert found == {by_turn.id: ctx.session_id, attached.id: ctx.session_id}
    assert await ctx.sessions.live_session_owners([ctx.session_id, "s_missing"]) == {ctx.session_id: ctx.owner}


async def test_a_pinned_run_data_file_is_kept_by_the_30_day_cleanup(ctx) -> None:
    files = []
    for name in ("pinned.csv", "unpinned.csv"):
        art = await ctx.artifacts.put_bytes(
            content=name.encode(),
            filename=name,
            mime="text/csv",
            source="sandbox_output",
            created_by=ctx.owner,
            kind="tool_data",
            workspace_id=ctx.ws,
        )
        files.append(art)
    from bowhead_core.sqlite_domain_runtime import connect_sync

    with closing(connect_sync(domain_database_path(str(ctx.core), "content"))) as db:
        db.executemany("UPDATE artifacts SET created_at='2026-09-01T00:00:00+00:00' WHERE id=?", [(art.id,) for art in files])
        db.commit()
    await ctx.sessions.pin_result(user_id=ctx.owner, target_kind="file", target_id=files[0].id)
    stale = await ctx.artifacts.stale_run_data_ids(days=30)
    assert files[1].id in stale
    assert files[0].id not in stale
    assert await ctx.artifacts.expire_run_data_ids([files[0].id]) == 0
    await ctx.sessions.unpin_result(user_id=ctx.owner, target_kind="file", target_id=files[0].id)
    assert files[0].id in await ctx.artifacts.stale_run_data_ids(days=30)
