"""Store primitives behind the cross-scope KB file list.

- ``KbFilesStore.list_for_scopes_filtered`` returns exactly the union of the
  single-scope ``list_for_scope_filtered`` results, in one global order.
  Other teams' rows never come back, even if they match a scope predicate.
- ``AccessControlStore.explain_decisions`` gives, per id, the same decision as
  ``explain_decision`` (deny wins, allowlists, revoked and expired grants).
- ``record_runtime_denial_summary`` writes one capped summary row per list request.
"""

from __future__ import annotations

import json
import random

import aiosqlite
import pytest
from bowhead_core.access_control import DENIAL_SUMMARY_SAMPLE_SIZE, AccessContext, AccessControlStore
from bowhead_core.audit_actions import AuditAction
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.user_store import UserStore

TEAM = "team_1"
FILTERS = (
    {},
    {"q": "2", "order_by": "filename", "order_dir": "asc"},
    {"statuses": ["done"], "mime_prefixes": ["text/"], "order_by": "size_bytes"},
)


@pytest.fixture
async def files(initialize_migrated_kb_files_database, tmp_path):
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    store = KbFilesStore(tmp_path / "kb_files.db")
    await store.initialize()
    return store


@pytest.mark.parametrize("seed", [3, 17])
async def test_cross_scope_listing_is_the_ordered_union_of_single_scopes(files: KbFilesStore, seed: int) -> None:
    rng = random.Random(seed)
    for i in range(60):
        scope = rng.choice(["personal", "space", "workspace", "team"])
        space = rng.choice(["ws_a", "ws_b"])
        scope_id = {"personal": rng.choice(["u_a", "u_b"]), "team": rng.choice([TEAM, "team_2"])}.get(scope, space)
        fid = await files.create(
            artifact_id=f"art_{i}",
            filename=f"f{i % 5}.{rng.choice(['md', 'pdf'])}",
            mime_type=rng.choice(["text/markdown", "application/pdf"]),
            size_bytes=rng.randint(1, 3),
            scope=scope,
            scope_id=scope_id,
            workspace_id=space,
            team_id=rng.choice([TEAM, TEAM, TEAM, "team_2"]),
            uploaded_by="u_a",
            initial_parse_status=rng.choice(["done", "failed"]),
        )
        async with aiosqlite.connect(files._db_path) as db:
            await db.execute("UPDATE kb_files SET uploaded_at=? WHERE file_id=?", (f"2026-09-0{rng.randint(1, 3)}", fid))
            await db.commit()
        if rng.random() < 0.1:
            await files.soft_delete(fid)

    for filters in FILTERS:
        expected: list = []
        for scope, scope_id in (("personal", "u_a"), ("space", "ws_a"), ("team", TEAM)):
            expected.extend(r for r in await files.list_for_scope_filtered(scope, scope_id, **filters) if r.team_id == TEAM)
        column = filters.get("order_by", "uploaded_at")
        reverse = filters.get("order_dir", "desc") == "desc"
        expected.sort(key=lambda r: (getattr(r, column), r.file_id), reverse=reverse)

        rows = await files.list_for_scopes_filtered(team_id=TEAM, personal_owner_id="u_a", space_id="ws_a", team_scope_id=TEAM, **filters)

        assert [r.file_id for r in rows] == [r.file_id for r in expected]
        assert expected, filters
    assert await files.list_for_scopes_filtered(team_id=TEAM, personal_owner_id=None, space_id=None, team_scope_id=None) == []
    assert await files.list_for_scopes_filtered(team_id="", personal_owner_id="u_a", space_id="ws_a", team_scope_id=TEAM) == []


async def test_other_team_rows_are_excluded_in_sql(files: KbFilesStore) -> None:
    """Another team's rows that match every scope predicate never come back from the store."""
    for scope, scope_id in (("personal", "u_a"), ("space", "ws_a"), ("team", TEAM)):
        await files.create(
            artifact_id=f"art_{scope}",
            filename="x.md",
            mime_type="text/markdown",
            size_bytes=1,
            scope=scope,
            scope_id=scope_id,
            workspace_id="ws_a",
            team_id="team_2",
            uploaded_by="u_a",
        )

    rows = await files.list_for_scopes_filtered(team_id=TEAM, personal_owner_id="u_a", space_id="ws_a", team_scope_id=TEAM)

    assert rows == []


async def test_batch_decisions_match_single_decisions(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    await store.initialize()
    ctx = AccessContext(user_id="u_a", team_id=TEAM, workspace_id="ws_a", workspace_role="member")
    ids = [f"kbf_{i}" for i in range(450)]  # spans more than one IN chunk
    rng = random.Random(9)
    for rid in rng.sample(ids, 120):
        principal_type, principal_id = rng.choice([("user", "u_a"), ("user", "u_b"), ("team", TEAM), ("workspace", "ws_a"), ("workspace", "ws_b")])
        grant = await store.create_or_update_resource_grant(
            team_id=TEAM,
            resource_kind="kb_file",
            resource_id=rid,
            principal_type=principal_type,
            principal_id=principal_id,
            permission=rng.choice(["read", "write"]),
            effect=rng.choice(["allow", "deny"]),
            expires_at=rng.choice([None, None, "2000-01-01T00:00:00+00:00"]),
        )
        if rng.random() < 0.2:
            await store.revoke_resource_grant(TEAM, grant.id)

    batch = await store.explain_decisions(ctx, "kb_file", ids, "read")

    single = {rid: await store.explain_decision(ctx, "kb_file", rid, "read") for rid in ids}
    assert batch == single
    assert {d["reason"] for d in batch.values()} >= {"denied_by_grant", "allowed_by_grant", "not_in_allowlist", "allowed_default_no_allowlist"}


@pytest.mark.parametrize(("enforce", "mode"), [("1", "enforce"), ("0", "shadow")])
async def test_denial_summary_is_one_capped_row(tmp_path, monkeypatch, enforce: str, mode: str) -> None:
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", enforce)
    path = tmp_path / "bowhead.db"
    await UserStore(path).initialize()
    store = AccessControlStore(path)
    await store.initialize()
    ctx = AccessContext(user_id="u_a", team_id=TEAM, workspace_id="ws_a", workspace_role="member")
    denied = [(f"kbf_{i:02d}", "denied_by_grant" if i % 3 else "not_in_allowlist") for i in range(25)]

    await store.record_runtime_denial_summary(
        ctx,
        resource_kind="kb_file",
        permission="read",
        denied=denied,
        target_kind="kb_file_list",
        target_id="kb.list_files",
        payload={"scopes": ["team"]},
    )
    await store.record_runtime_denial_summary(
        ctx, resource_kind="kb_file", permission="read", denied=[], target_kind="kb_file_list", target_id="kb.list_files"
    )

    async with aiosqlite.connect(path) as db, db.execute("SELECT action, target_kind, target_id, payload_json FROM admin_audit_log") as cur:
        rows = list(await cur.fetchall())
    assert len(rows) == 1
    action, target_kind, target_id, payload_json = rows[0]
    assert (action, target_kind, target_id) == (AuditAction.RESOURCE_LIST_ACCESS_FILTERED, "kb_file_list", "kb.list_files")
    payload = json.loads(payload_json)
    assert payload["denied_count"] == 25
    assert payload["reasons"] == {"denied_by_grant": 16, "not_in_allowlist": 9}
    assert payload["sample_resource_ids"] == [fid for fid, _ in denied[:DENIAL_SUMMARY_SAMPLE_SIZE]]
    assert payload["sample_truncated"] is True
    assert (payload["mode"], payload["scopes"], payload["workspace_id"]) == (mode, ["team"], "ws_a")
