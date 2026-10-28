"""GET /api/v1/kb/files?scope=all — permission review for the cross-scope file list.

The cross-scope list must equal, as a set, the union of the three single-scope lists
(personal, space, team) for every caller, space header, filter and grant state: it
never shows a file that a single-scope list or a per-id route would hide. Pagination
over the global (sort column, file_id) order must visit every row exactly once.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.access_control import DENIAL_SUMMARY_SAMPLE_SIZE, AccessControlStore
from bowhead_core.audit_actions import AuditAction
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_main"
OTHER_TEAM = "team_other"
WS_A = "ws_alpha"
WS_B = "ws_beta"
WS_ARCHIVED = "ws_archived"

MEMBER = "u_member"
SPACE_ADMIN = "u_space_admin"
TEAM_ADMIN = "u_team_admin"
OWNER = "u_owner"
OUTSIDER = "u_outsider"  # WS_B member only.
ROLES = (MEMBER, SPACE_ADMIN, TEAM_ADMIN, OWNER, OUTSIDER)

_SPACE_ROLES = {
    (WS_A, MEMBER): "member",
    (WS_A, SPACE_ADMIN): "ws_admin",
    (WS_A, TEAM_ADMIN): "member",
    (WS_A, OWNER): "member",
    (WS_ARCHIVED, MEMBER): "member",
    (WS_ARCHIVED, SPACE_ADMIN): "ws_admin",
    (WS_B, OUTSIDER): "member",
}
_TEAM_ROLES = {MEMBER: "member", SPACE_ADMIN: "member", TEAM_ADMIN: "team_admin", OWNER: "owner", OUTSIDER: "member"}
_HEADERS: tuple[str | None, ...] = (None, WS_A, WS_B, WS_ARCHIVED)


class _UserStore:
    """Membership source for the production RBAC snapshot (build_access_context)."""

    def __init__(self) -> None:
        self.space_status = {WS_A: "active", WS_B: "active", WS_ARCHIVED: "archived"}

    async def get_user_team_id(self, user_id: str) -> str:
        return TEAM

    async def get_workspace_team_id(self, workspace_id: str) -> str | None:
        return TEAM if workspace_id in self.space_status else None

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        return _SPACE_ROLES.get((workspace_id, user_id))

    async def get_workspace(self, workspace_id: str) -> dict[str, str] | None:
        status = self.space_status.get(workspace_id)
        return {"id": workspace_id, "status": status} if status else None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return _TEAM_ROLES.get(user_id) if team_id == TEAM else None


@pytest.fixture
async def env(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    await UserStore(tmp_path / "access.db").initialize()  # admin_audit_log lives in the core DB
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(kb.router)
    app.state.kb_files_store = files
    app.state.access_control_store = access
    app.state.user_store = _UserStore()
    current = {"user": MEMBER}
    app.dependency_overrides[_current_user] = lambda: User(
        id=current["user"], email=f"{current['user']}@example.test", team_id=TEAM, tier="ultra", email_verified=True
    )

    def as_user(user_id: str) -> None:
        current["user"] = user_id

    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        files=files,
        access=access,
        db=tmp_path / "files.db",
        audit_db=tmp_path / "access.db",
        as_user=as_user,
    )


async def _create(env: Any, *, scope: str, scope_id: str, workspace_id: str, uploaded_by: str, team_id: str = TEAM, **extra: Any) -> str:
    return await env.files.create(
        artifact_id=f"art_{scope}_{scope_id}",
        filename=extra.pop("filename", f"{scope}-{scope_id}.md"),
        mime_type=extra.pop("mime_type", "text/markdown"),
        size_bytes=extra.pop("size_bytes", 10),
        scope=scope,
        scope_id=scope_id,
        workspace_id=workspace_id,
        team_id=team_id,
        uploaded_by=uploaded_by,
        initial_parse_status=extra.pop("initial_parse_status", "done"),
    )


def _headers(space: str | None) -> dict[str, str]:
    return {"X-Space-Id": space} if space else {}


def _list(env: Any, scope: str, space: str | None, **params: Any) -> tuple[int, list[str]]:
    response = env.client.get("/api/v1/kb/files", params={"scope": scope, "limit": 1000, **params}, headers=_headers(space))
    if response.status_code != 200:
        return response.status_code, []
    return 200, [f["file_id"] for f in response.json()["files"]]


def _assert_union(env: Any, space: str | None, **params: Any) -> list[str]:
    status_all, all_ids = _list(env, "all", space, **params)
    singles = [_list(env, scope, space, **params) for scope in ("personal", "space", "team")]
    if status_all != 200:
        # A refused space header refuses every listing alike; nothing leaks through "all".
        assert all(status == status_all for status, _ in singles), (status_all, singles)
        return []
    union = {fid for status, ids in singles if status == 200 for fid in ids}
    assert set(all_ids) == union
    assert len(all_ids) == len(set(all_ids))
    return all_ids


async def _seed_random(env: Any, rng: random.Random, count: int) -> list[str]:
    """Random files across scopes, owners, spaces and teams, with tied sort values."""
    owners = [*ROLES, "u_stranger"]
    spaces = [WS_A, WS_B, WS_ARCHIVED, "ws_unknown"]
    ids: list[str] = []
    for i in range(count):
        kind = rng.choice(["personal", "space", "team", "other_team"])
        owner = rng.choice(owners)
        space = rng.choice(spaces)
        if kind == "personal":
            fid = await _create(env, scope="personal", scope_id=owner, workspace_id=space, uploaded_by=owner, filename=f"f{i % 7}.md")
        elif kind == "space":
            fid = await _create(
                env, scope=rng.choice(["space", "workspace"]), scope_id=space, workspace_id=space, uploaded_by=owner, filename=f"f{i % 7}.md"
            )
        elif kind == "team":
            fid = await _create(
                env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=owner, filename=f"f{i % 7}.pdf", mime_type="application/pdf"
            )
        else:
            fid = await _create(
                env,
                scope=rng.choice(["team", "personal"]),
                scope_id=rng.choice([OTHER_TEAM, owner]),
                workspace_id="",
                uploaded_by=owner,
                team_id=OTHER_TEAM,
            )
        ids.append(fid)
    with sqlite3.connect(env.db) as db:
        for fid in ids:
            db.execute(
                "UPDATE kb_files SET uploaded_at=?, size_bytes=?, parse_status=? WHERE file_id=?",
                (f"2026-09-{rng.randint(1, 3):02d}T00:00:00", rng.choice([1, 2, 3]), rng.choice(["done", "failed", "parsing"]), fid),
            )
    for fid in rng.sample(ids, k=count // 10):
        await env.files.soft_delete(fid)
    for fid in rng.sample(ids, k=count // 8):
        effect, principal = rng.choice([("deny", MEMBER), ("deny", SPACE_ADMIN), ("allow", OWNER), ("deny", TEAM)])
        grant = await env.access.create_or_update_resource_grant(
            team_id=TEAM,
            resource_kind="kb_file",
            resource_id=fid,
            principal_type="team" if principal == TEAM else "user",
            principal_id=principal,
            permission="read",
            effect=effect,
        )
        if rng.random() < 0.3:
            await env.access.revoke_resource_grant(TEAM, grant.id)
    return ids


_FILTERS: tuple[dict[str, str], ...] = (
    {},
    {"q": "f3", "order_by": "filename", "order_dir": "asc"},
    {"status": "done,failed", "mime": "text/", "order_by": "size_bytes"},
    {"uploaded_after": "2026-09-02T00:00:00", "order_dir": "asc"},
)


@pytest.mark.parametrize("seed", [7, 23, 101])
async def test_all_scopes_equals_union_of_single_scope_lists_for_every_role(env, seed: int) -> None:
    seeded = await _seed_random(env, random.Random(seed), 80)
    listed_somewhere = 0

    for role in ROLES:
        env.as_user(role)
        for space in _HEADERS:
            for filters in _FILTERS:
                ids = _assert_union(env, space, **filters)
                listed_somewhere += len(ids)
                if filters or space is None or not ids:
                    continue
                # Independent oracle: the per-id route (single-id grant decision) agrees with
                # the batched list for every seeded id, readable or not.
                for fid in seeded:
                    status = env.client.get(f"/api/v1/kb/files/{fid}", headers=_headers(space)).status_code
                    assert status == (200 if fid in ids else 404), (role, space, fid)
    assert listed_somewhere > 0


@pytest.mark.parametrize("seed", [5, 11])
async def test_cursor_pages_visit_each_row_once_in_global_order(env, seed: int) -> None:
    rng = random.Random(seed)
    await _seed_random(env, rng, 90)
    env.as_user(SPACE_ADMIN)

    for filters in _FILTERS:
        full = _assert_union(env, WS_A, **filters)
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params = {"scope": "all", "limit": rng.randint(1, 9), **filters}
            if cursor:
                params["cursor"] = cursor
            body = env.client.get("/api/v1/kb/files", params=params, headers=_headers(WS_A)).json()
            assert body["total"] == len(full)
            seen.extend(f["file_id"] for f in body["files"])
            cursor = body["next_cursor"]
            assert (cursor is not None) == body["has_more"]
            if cursor is None:
                break
        assert seen == full
        offset_pages = [
            f["file_id"]
            for off in range(0, len(full), 7)
            for f in env.client.get("/api/v1/kb/files", params={"scope": "all", "limit": 7, "offset": off, **filters}, headers=_headers(WS_A)).json()[
                "files"
            ]
        ]
        assert offset_pages == full


async def test_items_carry_their_scope_and_hide_other_members_and_spaces(env) -> None:
    mine = await _create(env, scope="personal", scope_id=SPACE_ADMIN, workspace_id=WS_A, uploaded_by=SPACE_ADMIN)
    members_personal = await _create(env, scope="personal", scope_id=MEMBER, workspace_id=WS_A, uploaded_by=MEMBER)
    space_file = await _create(env, scope="workspace", scope_id=WS_A, workspace_id=WS_A, uploaded_by=MEMBER)
    other_space = await _create(env, scope="space", scope_id=WS_B, workspace_id=WS_B, uploaded_by=OUTSIDER)
    team_file = await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=TEAM_ADMIN)
    other_team = await _create(env, scope="team", scope_id=OTHER_TEAM, workspace_id="", uploaded_by=OUTSIDER, team_id=OTHER_TEAM)
    env.as_user(SPACE_ADMIN)

    response = env.client.get("/api/v1/kb/files", params={"scope": "all"}, headers=_headers(WS_A))

    assert response.status_code == 200, response.text
    scopes = {f["file_id"]: f["scope"] for f in response.json()["files"]}
    assert scopes == {mine: "personal", space_file: "space", team_file: "team"}
    assert not {members_personal, other_space, other_team} & set(scopes)


async def test_other_team_rows_matching_every_scope_predicate_are_never_listed(env) -> None:
    """Another team's rows keyed to this caller's user, space and team ids stay out of scope=all.

    Holds by the SQL team filter alone as well as by the route's own team check
    (verified by removing the route check: this test still passes).
    """
    for scope, scope_id, workspace_id in (("personal", MEMBER, WS_A), ("space", WS_A, WS_A), ("team", TEAM, "")):
        await _create(env, scope=scope, scope_id=scope_id, workspace_id=workspace_id, uploaded_by=MEMBER, team_id=OTHER_TEAM)
    mine = await _create(env, scope="personal", scope_id=MEMBER, workspace_id=WS_A, uploaded_by=MEMBER)

    assert _list(env, "all", WS_A) == (200, [mine])


async def test_without_space_header_lists_personal_and_team_only(env) -> None:
    personal = await _create(env, scope="personal", scope_id=MEMBER, workspace_id=WS_A, uploaded_by=MEMBER)
    await _create(env, scope="space", scope_id=WS_A, workspace_id=WS_A, uploaded_by=MEMBER)
    team_file = await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)

    status, ids = _list(env, "all", None)

    assert status == 200
    assert set(ids) == {personal, team_file}


@pytest.mark.parametrize("role", [TEAM_ADMIN, OWNER, OUTSIDER])
async def test_non_member_space_header_is_refused_for_every_role(env, role: str) -> None:
    await _create(env, scope="space", scope_id=WS_ARCHIVED, workspace_id=WS_ARCHIVED, uploaded_by=MEMBER)
    env.as_user(role)

    response = env.client.get("/api/v1/kb/files", params={"scope": "all"}, headers=_headers(WS_ARCHIVED))

    assert response.status_code == 403
    assert "space-" not in response.text


async def test_archived_space_stays_readable_like_its_single_scope_list(env) -> None:
    archived = await _create(env, scope="space", scope_id=WS_ARCHIVED, workspace_id=WS_ARCHIVED, uploaded_by=MEMBER)

    ids = _assert_union(env, WS_ARCHIVED)

    assert archived in ids


async def test_deleted_file_is_never_listed(env) -> None:
    gone = await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)
    await env.files.soft_delete(gone)

    assert gone not in _assert_union(env, WS_A)


async def test_denied_then_revoked_grant_hides_then_restores_the_file(env) -> None:
    team_file = await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)
    grant = await env.access.create_or_update_resource_grant(
        team_id=TEAM, resource_kind="kb_file", resource_id=team_file, principal_type="user", principal_id=MEMBER, permission="read", effect="deny"
    )
    assert team_file not in _assert_union(env, WS_A)
    assert env.client.get(f"/api/v1/kb/files/{team_file}", headers=_headers(WS_A)).status_code == 404

    await env.access.revoke_resource_grant(TEAM, grant.id)

    assert team_file in _assert_union(env, WS_A)


async def test_allowlist_for_someone_else_hides_the_file(env) -> None:
    team_file = await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)
    await env.access.create_or_update_resource_grant(
        team_id=TEAM, resource_kind="kb_file", resource_id=team_file, principal_type="user", principal_id=OWNER, permission="read", effect="allow"
    )

    assert team_file not in _assert_union(env, WS_A)
    env.as_user(OWNER)
    assert team_file in _assert_union(env, WS_A)


async def test_denied_team_scope_grant_drops_team_files_from_all(env) -> None:
    personal = await _create(env, scope="personal", scope_id=MEMBER, workspace_id=WS_A, uploaded_by=MEMBER)
    await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)
    await env.access.create_or_update_resource_grant(
        team_id=TEAM,
        resource_kind="kb_scope",
        resource_id=f"team:{TEAM}",
        principal_type="user",
        principal_id=MEMBER,
        permission="read",
        effect="deny",
    )

    assert _list(env, "team", WS_A)[0] == 403
    assert _assert_union(env, WS_A) == [personal]


async def test_cursor_must_match_the_requested_sort(env) -> None:
    for _ in range(3):
        await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)
    cursor = env.client.get("/api/v1/kb/files", params={"scope": "all", "limit": 1}).json()["next_cursor"]

    for params in ({"cursor": cursor, "order_by": "filename"}, {"cursor": "not-a-cursor"}, {"cursor": cursor, "offset": 1}):
        response = env.client.get("/api/v1/kb/files", params={"scope": "all", **params})
        assert response.status_code == 400, params
        assert response.json()["detail"]["error"]["code"] == "invalid_kb_pagination"


# ─── denial audit: one summary row per list request, per-id rows unchanged ───


def _audit(env: Any) -> list[tuple[str, str, str, dict]]:
    with sqlite3.connect(env.audit_db) as db:
        rows = db.execute("SELECT action, target_kind, target_id, payload_json FROM admin_audit_log ORDER BY rowid").fetchall()
    return [(action, kind, target, json.loads(payload)) for action, kind, target, payload in rows]


async def _team_files_with_grants(env: Any, *, denied: int, allowlisted: int) -> list[str]:
    ids = []
    for i in range(denied + allowlisted):
        fid = await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER, filename=f"g{i}.md")
        principal, effect = (MEMBER, "deny") if i < denied else (OWNER, "allow")
        await env.access.create_or_update_resource_grant(
            team_id=TEAM, resource_kind="kb_file", resource_id=fid, principal_type="user", principal_id=principal, permission="read", effect=effect
        )
        ids.append(fid)
    return ids


@pytest.mark.parametrize(("scope", "scopes"), [("all", ["personal", "space", "team"]), ("team", ["team"])])
async def test_list_writes_one_summary_row_with_counts(env, scope: str, scopes: list[str]) -> None:
    hidden = await _team_files_with_grants(env, denied=3, allowlisted=2)
    visible = await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)

    status, ids = _list(env, scope, WS_A)

    assert (status, ids) == (200, [visible])
    rows = _audit(env)
    assert [(action, kind, target) for action, kind, target, _ in rows] == [
        (AuditAction.RESOURCE_LIST_ACCESS_FILTERED, "kb_file_list", "kb.list_files")
    ]
    payload = rows[0][3]
    assert payload["denied_count"] == 5
    assert payload["reasons"] == {"denied_by_grant": 3, "not_in_allowlist": 2}
    assert sorted(payload["sample_resource_ids"]) == sorted(hidden)
    assert payload["sample_truncated"] is False
    assert (payload["scopes"], payload["mode"], payload["resource_kind"]) == (scopes, "enforce", "kb_file")


async def test_list_summary_caps_the_id_sample(env) -> None:
    await _team_files_with_grants(env, denied=DENIAL_SUMMARY_SAMPLE_SIZE + 5, allowlisted=0)

    _list(env, "all", None)

    ((action, _, _, payload),) = _audit(env)
    assert action == AuditAction.RESOURCE_LIST_ACCESS_FILTERED
    assert payload["denied_count"] == DENIAL_SUMMARY_SAMPLE_SIZE + 5
    assert len(payload["sample_resource_ids"]) == DENIAL_SUMMARY_SAMPLE_SIZE
    assert payload["sample_truncated"] is True
    assert payload["scopes"] == ["personal", "team"]


async def test_list_without_denials_writes_no_audit_row(env) -> None:
    await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)

    _list(env, "all", WS_A)

    assert _audit(env) == []


async def test_per_id_route_keeps_writing_one_row_per_file(env) -> None:
    (denied,) = await _team_files_with_grants(env, denied=1, allowlisted=0)

    for path in (f"/api/v1/kb/files/{denied}", f"/api/v1/kb/files/{denied}/download"):
        assert env.client.get(path, headers=_headers(WS_A)).status_code == 404

    rows = _audit(env)
    assert [(action, kind, target) for action, kind, target, _ in rows] == [(AuditAction.RESOURCE_ACCESS_DENIED, "kb_file", denied)] * 2


async def test_failed_summary_write_is_logged_and_the_list_still_answers(env, monkeypatch, caplog) -> None:
    await _team_files_with_grants(env, denied=2, allowlisted=0)
    visible = await _create(env, scope="team", scope_id=TEAM, workspace_id="", uploaded_by=OWNER)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(env.access, "record_runtime_denial_summary", broken)
    with caplog.at_level(logging.WARNING, logger=kb.logger.name):
        assert _list(env, "all", WS_A) == (200, [visible])

    assert any("kb_file_list_audit_failed" in record.getMessage() and "count=2" in record.getMessage() for record in caplog.records)
