"""kb-security-quickfix S1 (P0): kb_records routes must enforce workspace
membership, not trust a client-supplied workspace_id.

Before this fix, ``kb_records.py`` derived the workspace solely from the
caller-controlled ``X-Space-Id`` header / ``workspace_id`` query param and only
guarded with ``rec.workspace_id != ws`` — but ``ws`` was the attacker's own
input, so any authed user who knew a ``record_id``/``doc_type_id`` + a target
``workspace_id`` could read or mutate another tenant's extracted records.

These tests mount the router with a production-shaped ``user_store`` (so
``has_workspace_access_source`` is True and membership is enforced) and assert a
non-member is rejected on every records endpoint, while a genuine member is
served. A separate block locks the store-layer ``workspace_id`` predicate that
backs the route as defense-in-depth.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb_records
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_shared"
WS_MEMBER = "ws_member"
WS_VICTIM = "ws_victim"
ATTACKER = "u_attacker"
DT = "dt_contracts"


def _user() -> User:
    return User(id=ATTACKER, email="attacker@test.com", team_id=TEAM, tier="ultra", email_verified=True)


class _MembershipStore:
    """Fake user_store: attacker is a member of WS_MEMBER only. Both workspaces
    live in the same team, so a rejection isolates the *membership* check (not a
    cross-team short-circuit)."""

    async def get_user_team_id(self, user_id: str) -> str:
        return TEAM

    async def get_workspace_team_id(self, ws_id: str) -> str:
        return TEAM  # same team for both ws → non-membership is the only differentiator

    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        if ws_id == WS_MEMBER and user_id == ATTACKER:
            return "member"
        return None  # not a member of WS_VICTIM

    async def get_workspace(self, ws_id: str) -> dict | None:
        if ws_id in {WS_MEMBER, WS_VICTIM}:
            return {"id": ws_id, "status": "active"}
        return None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return None

    async def is_team_admin(self, user_id: str) -> bool:
        return False


def _space_file(file_id: str, workspace_id: str) -> KbFileDTO:
    return KbFileDTO(
        file_id=file_id,
        artifact_id=f"art_{file_id}",
        filename=f"{file_id}.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        scope="space",
        scope_id=workspace_id,
        workspace_id=workspace_id,
        team_id=TEAM,
        uploaded_by="system",
        uploaded_at="2026-01-01T00:00:00Z",
    )


class _SeededFiles:
    """The Space files the seeded records below come from (what the records routes read)."""

    def __init__(self) -> None:
        self._files = {f.file_id: f for f in (_space_file("f_m", WS_MEMBER), _space_file("f_v", WS_VICTIM), _space_file("f_v1", WS_MEMBER))}

    async def get(self, file_id: str) -> KbFileDTO | None:
        return self._files.get(file_id)

    async def list_access_rows_for_scopes(self, *, team_id: str, personal_owner_id: str | None, space_id: str | None, team_scope_id: str | None):
        return [f for f in self._files.values() if f.team_id == team_id and f.workspace_id == space_id]


@pytest.fixture
async def ctx(tmp_path: Path):
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    app = FastAPI()
    app.include_router(kb_records.router)
    app.dependency_overrides[_current_user] = _user
    app.state.kb_extracted_record_store = store
    app.state.kb_files_store = _SeededFiles()
    app.state.user_store = _MembershipStore()
    return TestClient(app, raise_server_exceptions=False), store


async def _seed(store: KbExtractedRecordStore, workspace_id: str, file_id: str) -> str:
    return await store.create(
        workspace_id=workspace_id,
        doc_type_id=DT,
        file_id=file_id,
        fields={},
        verify_status="ready",
        created_by="system",
    )


# ─── P0: non-member is rejected on every records endpoint ──────────────


async def test_get_record_non_member_rejected(ctx) -> None:
    client, store = ctx
    rec = await _seed(store, WS_VICTIM, "f_v")
    # Attacker supplies the victim ws they are NOT a member of.
    r = client.get(f"/api/v1/kb/records/{rec}", params={"workspace_id": WS_VICTIM})
    assert r.status_code == 403, r.text  # membership denied (was 200 leak before fix)


async def test_list_records_non_member_rejected(ctx) -> None:
    client, store = ctx
    await _seed(store, WS_VICTIM, "f_v")
    # Worst case: only doc_type_id + workspace_id needed to dump a whole table.
    r = client.get(f"/api/v1/kb/doc_types/{DT}/records", params={"workspace_id": WS_VICTIM})
    assert r.status_code == 403, r.text


async def test_patch_field_non_member_rejected(ctx) -> None:
    client, store = ctx
    rec = await _seed(store, WS_VICTIM, "f_v")
    r = client.patch(
        f"/api/v1/kb/records/{rec}/field",
        params={"workspace_id": WS_VICTIM},
        json={"field_name": "amount", "new_value": "999"},
    )
    assert r.status_code == 403, r.text
    # And the record was NOT mutated.
    fresh = await store.get(rec)
    assert fresh is not None
    assert "amount" not in fresh.edited_by_user


async def test_patch_verify_status_non_member_rejected(ctx) -> None:
    client, store = ctx
    rec = await _seed(store, WS_VICTIM, "f_v")
    r = client.patch(
        f"/api/v1/kb/records/{rec}/verify_status",
        params={"workspace_id": WS_VICTIM},
        json={"status": "approved"},
    )
    assert r.status_code == 403, r.text
    fresh = await store.get(rec)
    assert fresh is not None
    assert fresh.verify_status == "ready"  # unchanged


async def test_spoofed_header_does_not_bypass(ctx) -> None:
    """Even if the query param is the caller's own ws, an X-Space-Id header
    pointing at the victim ws must not grant access (header takes precedence but
    is still membership-checked)."""
    client, store = ctx
    rec = await _seed(store, WS_VICTIM, "f_v")
    r = client.get(
        f"/api/v1/kb/records/{rec}",
        params={"workspace_id": WS_MEMBER},
        headers={"X-Space-Id": WS_VICTIM},
    )
    assert r.status_code == 403, r.text


# ─── member happy-path still works ─────────────────────────────────────


async def test_member_can_read_own_record(ctx) -> None:
    client, store = ctx
    rec = await _seed(store, WS_MEMBER, "f_m")
    r = client.get(f"/api/v1/kb/records/{rec}", params={"workspace_id": WS_MEMBER})
    assert r.status_code == 200, r.text
    assert r.json()["record_id"] == rec


async def test_member_can_patch_verify_status(ctx) -> None:
    client, store = ctx
    rec = await _seed(store, WS_MEMBER, "f_m")
    r = client.patch(
        f"/api/v1/kb/records/{rec}/verify_status",
        params={"workspace_id": WS_MEMBER},
        json={"status": "approved"},
    )
    assert r.status_code == 200, r.text
    fresh = await store.get(rec)
    assert fresh is not None and fresh.verify_status == "approved"


# ─── store-layer defense-in-depth: workspace_id predicate ──────────────


async def test_store_get_scoped_by_workspace(tmp_path: Path) -> None:
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_VICTIM, "f_v")
    assert await store.get(rec, workspace_id=WS_VICTIM) is not None
    assert await store.get(rec, workspace_id=WS_MEMBER) is None  # wrong ws → not loaded
    assert await store.get(rec) is not None  # no scope → legacy behavior preserved


async def test_store_update_verify_status_scoped_by_workspace(tmp_path: Path) -> None:
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_VICTIM, "f_v")
    # Wrong ws → no-op (rowcount 0).
    assert await store.update_verify_status(rec, "approved", modified_by="x", workspace_id=WS_MEMBER) is False
    assert (await store.get(rec)).verify_status == "ready"
    # Correct ws → applies.
    assert await store.update_verify_status(rec, "approved", modified_by="x", workspace_id=WS_VICTIM) is True
    assert (await store.get(rec)).verify_status == "approved"


async def test_store_update_field_scoped_by_workspace(tmp_path: Path) -> None:
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_VICTIM, "f_v")
    # Wrong ws → pre-fetch returns None → update refuses.
    assert await store.update_field(record_id=rec, field_name="amount", new_value="1", modified_by="x", workspace_id=WS_MEMBER) is False
    # Correct ws → applies.
    assert await store.update_field(record_id=rec, field_name="amount", new_value="1", modified_by="x", workspace_id=WS_VICTIM) is True


# ─── kb-matrix M1: role floor — viewer is a member but read-only ────────


class _RoleStore(_MembershipStore):
    """Membership store where the caller IS a member of WS_MEMBER with a
    configurable role (viewer/member/admin), and optionally an team admin."""

    def __init__(self, role: str | None, *, team_admin: bool = False) -> None:
        self._role = role
        self._team_admin = team_admin

    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        if ws_id == WS_MEMBER and user_id == ATTACKER:
            return self._role
        return None

    async def is_team_admin(self, user_id: str) -> bool:
        return self._team_admin


def _mk_client(store: KbExtractedRecordStore, user_store: object, doc_type_store: object | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(kb_records.router)
    app.dependency_overrides[_current_user] = _user
    app.state.kb_extracted_record_store = store
    app.state.kb_files_store = _SeededFiles()
    app.state.user_store = user_store
    if doc_type_store is not None:
        app.state.kb_doc_type_store = doc_type_store
    return TestClient(app, raise_server_exceptions=False)


async def test_viewer_can_read_but_not_mutate(tmp_path: Path) -> None:
    """PRD FR-34: viewer (rank 1) is a legitimate member yet read-only — before
    this fix membership alone let viewers PATCH records."""
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_MEMBER, "f_m")
    client = _mk_client(store, _RoleStore("viewer"))

    # reads stay open
    assert client.get(f"/api/v1/kb/records/{rec}", params={"workspace_id": WS_MEMBER}).status_code == 200
    assert client.get(f"/api/v1/kb/doc_types/{DT}/records", params={"workspace_id": WS_MEMBER}).status_code == 200

    # writes are role-floored
    r = client.patch(
        f"/api/v1/kb/records/{rec}/field",
        params={"workspace_id": WS_MEMBER},
        json={"field_name": "amount", "new_value": "999"},
    )
    assert r.status_code == 403, r.text
    assert "workspace_member_role_required" in r.text
    r = client.patch(
        f"/api/v1/kb/records/{rec}/verify_status",
        params={"workspace_id": WS_MEMBER},
        json={"status": "approved"},
    )
    assert r.status_code == 403, r.text
    fresh = await store.get(rec)
    assert fresh is not None
    assert fresh.verify_status == "ready" and "amount" not in fresh.edited_by_user


async def test_readonly_alias_role_is_floored_too(tmp_path: Path) -> None:
    """'readonly'/'read_only' normalize to viewer (access_control aliases)."""
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_MEMBER, "f_m")
    client = _mk_client(store, _RoleStore("read_only"))
    r = client.patch(
        f"/api/v1/kb/records/{rec}/verify_status",
        params={"workspace_id": WS_MEMBER},
        json={"status": "approved"},
    )
    assert r.status_code == 403, r.text


async def test_ws_admin_can_mutate(tmp_path: Path) -> None:
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_MEMBER, "f_m")
    client = _mk_client(store, _RoleStore("admin"))  # alias → ws_admin
    r = client.patch(
        f"/api/v1/kb/records/{rec}/verify_status",
        params={"workspace_id": WS_MEMBER},
        json={"status": "approved"},
    )
    assert r.status_code == 200, r.text


async def test_team_admin_nonmember_still_rejected(tmp_path: Path) -> None:
    """S1 regression: is_team_admin must not bypass the membership check — deny
    happens before team-admin is even consulted."""
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_VICTIM, "f_v")
    client = _mk_client(store, _RoleStore(None, team_admin=True))  # not a member anywhere
    r = client.patch(
        f"/api/v1/kb/records/{rec}/verify_status",
        params={"workspace_id": WS_VICTIM},
        json={"status": "approved"},
    )
    assert r.status_code == 403, r.text


# ─── kb-matrix M1: routes pass doc_type_fields → V1 rows keep real types ─


class _FakeDocTypeStore:
    """Returns a doc_type whose `amount` field is currency — the type hint that
    lets the V1 lazy-wrap produce the raw_string residual sentinel."""

    def __init__(self, workspace_id: str = WS_MEMBER) -> None:
        from bowhead_core.kb.doc_type_store import ExtractField

        self._fields = [ExtractField(name="amount", type="currency")]
        self._workspace_id = workspace_id

    async def get(self, doc_type_id: str):  # noqa: ANN201
        if doc_type_id != DT:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(fields=self._fields, workspace_id=self._workspace_id)


async def _seed_v1_legacy_row(store: KbExtractedRecordStore, record_id: str) -> None:
    """Insert a schema_version=1 row directly (store.create always writes v2)."""
    import json

    import aiosqlite

    fields_v1 = {"amount": {"value": "面议（按季度结算）", "source_snippet": "面议", "confidence": 0.7}}
    async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001 - test fixture needs raw insert
        await db.execute(
            """INSERT INTO kb_extracted_records
               (record_id, doc_type_id, file_id, workspace_id, fields_json, verify_status,
                edited_by_user_json, extractor_model, extracted_at, last_modified_at,
                last_modified_by, schema_version)
               VALUES (?, ?, ?, ?, ?, 'ready', '[]', 'test', '2026-01-01T00:00:00', '2026-01-01T00:00:00', 'system', 1)""",
            (record_id, DT, "f_v1", WS_MEMBER, json.dumps(fields_v1, ensure_ascii=False)),
        )
        await db.commit()


async def test_v1_row_lazy_wraps_with_doc_type_fields(tmp_path: Path) -> None:
    """kb-matrix M1 tech design §4.5: without doc_type_fields the route degrades
    V1 rows to string-shape normalized and the raw_string sentinel (needs_review
    态) can never surface. With the doc_type store on app.state, currency fields
    that fail numeric coercion must come back as {"raw_string": ...}."""
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    await _seed_v1_legacy_row(store, "rec_v1")

    client = _mk_client(store, _RoleStore("member"), doc_type_store=_FakeDocTypeStore())
    r = client.get("/api/v1/kb/records/rec_v1", params={"workspace_id": WS_MEMBER})
    assert r.status_code == 200, r.text
    variant = r.json()["fields"]["amount"][0]
    assert variant["normalized"] == {"raw_string": "面议（按季度结算）"}, variant

    rl = client.get(f"/api/v1/kb/doc_types/{DT}/records", params={"workspace_id": WS_MEMBER})
    assert rl.status_code == 200, rl.text
    lv = rl.json()["items"][0]["fields"]["amount"][0]
    assert lv["normalized"] == {"raw_string": "面议（按季度结算）"}, lv


async def test_v1_row_degrades_without_doc_type_store(tmp_path: Path) -> None:
    """No kb_doc_type_store on app.state → historical degrade path preserved."""
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    await _seed_v1_legacy_row(store, "rec_v1")

    client = _mk_client(store, _RoleStore("member"))
    r = client.get("/api/v1/kb/records/rec_v1", params={"workspace_id": WS_MEMBER})
    assert r.status_code == 200, r.text
    variant = r.json()["fields"]["amount"][0]
    assert variant["normalized"] == {"value": "面议（按季度结算）"}, variant


async def test_foreign_ws_doc_type_fields_are_ignored(tmp_path: Path) -> None:
    """impl review P2-2: a doc_type belonging to another workspace must never
    influence this tenant's responses — the ws-scope guard drops its fields and
    the row degrades exactly as if the doc_type didn't exist."""
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    await _seed_v1_legacy_row(store, "rec_v1")

    client = _mk_client(store, _RoleStore("member"), doc_type_store=_FakeDocTypeStore(workspace_id=WS_VICTIM))
    r = client.get("/api/v1/kb/records/rec_v1", params={"workspace_id": WS_MEMBER})
    assert r.status_code == 200, r.text
    variant = r.json()["fields"]["amount"][0]
    assert variant["normalized"] == {"value": "面议（按季度结算）"}, variant  # degraded, NOT currency-typed


async def test_member_can_patch_field(tmp_path: Path) -> None:
    """impl review P2-4: direct happy-path lock for PATCH /field (was only
    transitively covered via verify_status sharing the same rank gate)."""
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_MEMBER, "f_m")
    client = _mk_client(store, _RoleStore("member"))
    r = client.patch(
        f"/api/v1/kb/records/{rec}/field",
        params={"workspace_id": WS_MEMBER},
        json={"field_name": "amount", "new_value": "123"},
    )
    assert r.status_code == 200, r.text
    fresh = await store.get(rec)
    assert fresh is not None and "amount" in fresh.edited_by_user


async def test_record_errors_use_finite_codes_without_store_prose(
    tmp_path: Path,
) -> None:
    store = KbExtractedRecordStore(tmp_path / "records.db")
    await store.initialize()
    rec = await _seed(store, WS_MEMBER, "f_m")
    client = _mk_client(store, _RoleStore("member"))

    missing = client.get(
        "/api/v1/kb/records/rec_missing",
        params={"workspace_id": WS_MEMBER},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"]["code"] == "kb_record_not_found"

    invalid = client.patch(
        f"/api/v1/kb/records/{rec}/field",
        params={"workspace_id": WS_MEMBER},
        json={"field_name": "bad field name", "new_value": "secret"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["error"]["code"] == "invalid_kb_record_value"
    assert "非法字符" not in invalid.text


# ─── Records follow their file's readability, not only the Space ─────────
# The worker stores a personal file's records under its upload Space, so the Space
# filter alone served them to every member of that Space.

OWNER = "u_owner"  # Owns the personal file; member of WS_MEMBER like ATTACKER.


class _SharedSpaceMembers(_MembershipStore):
    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        return "member" if ws_id == WS_MEMBER and user_id in {ATTACKER, OWNER} else None


@pytest.fixture
async def shared_space(tmp_path: Path, initialize_migrated_kb_files_database):
    """OWNER's personal file, a Space file and a Team file, each with one record in WS_MEMBER."""
    records = KbExtractedRecordStore(tmp_path / "records.db")
    await records.initialize()
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files = KbFilesStore(tmp_path / "kb_files.db")
    await files.initialize()
    with sqlite3.connect(tmp_path / "kb_files.db") as db:
        for file_id, scope, scope_id, workspace_id in (
            ("kbf_personal", "personal", OWNER, WS_MEMBER),
            ("kbf_space", "space", WS_MEMBER, WS_MEMBER),
            ("kbf_team", "team", TEAM, ""),
        ):
            db.execute(
                "INSERT INTO kb_files(file_id,artifact_id,filename,mime_type,size_bytes,scope,scope_id,workspace_id,team_id,uploaded_by,uploaded_at,parse_status) "
                "VALUES (?,?,?,'application/pdf',1,?,?,?,?,?,'2026-11-27T00:00:00Z','done')",
                (file_id, f"art_{file_id}", f"{file_id}.pdf", scope, scope_id, workspace_id, TEAM, OWNER),
            )
    ids = {file_id: await _seed(records, WS_MEMBER, file_id) for file_id in ("kbf_personal", "kbf_space", "kbf_team")}

    def client_for(user_id: str) -> TestClient:
        app = FastAPI()
        app.include_router(kb_records.router)
        app.dependency_overrides[_current_user] = lambda: User(
            id=user_id, email=f"{user_id}@test.com", team_id=TEAM, tier="ultra", email_verified=True
        )
        app.state.kb_extracted_record_store = records
        app.state.kb_files_store = files
        app.state.user_store = _SharedSpaceMembers()
        return TestClient(app, raise_server_exceptions=False)

    return client_for, records, ids


async def test_member_cannot_list_or_count_another_members_personal_records(shared_space) -> None:
    client_for, _records, _ids = shared_space
    listed = client_for(ATTACKER).get(f"/api/v1/kb/doc_types/{DT}/records", params={"workspace_id": WS_MEMBER})
    assert listed.status_code == 200, listed.text
    assert sorted(item["file_id"] for item in listed.json()["items"]) == ["kbf_space", "kbf_team"]
    assert listed.json()["total"] == 2

    owner = client_for(OWNER).get(f"/api/v1/kb/doc_types/{DT}/records", params={"workspace_id": WS_MEMBER})
    assert owner.status_code == 200, owner.text
    assert sorted(item["file_id"] for item in owner.json()["items"]) == ["kbf_personal", "kbf_space", "kbf_team"]
    assert owner.json()["total"] == 3


async def test_unreadable_record_answers_like_a_missing_record(shared_space) -> None:
    client_for, records, ids = shared_space
    client = client_for(ATTACKER)
    params = {"workspace_id": WS_MEMBER}
    requests = [
        ("get", "", None),
        ("patch", "/field", {"field_name": "amount", "new_value": "1"}),
        ("patch", "/verify_status", {"status": "approved"}),
    ]
    for method, suffix, body in requests:
        options = {"json": body} if body else {}
        hidden = getattr(client, method)(f"/api/v1/kb/records/{ids['kbf_personal']}{suffix}", params=params, **options)
        missing = getattr(client, method)(f"/api/v1/kb/records/rec_missing{suffix}", params=params, **options)
        assert hidden.status_code == missing.status_code == 404, (suffix, hidden.text)
        assert hidden.json() == missing.json() == {"detail": {"error": {"code": "kb_record_not_found"}}}
    fresh = await records.get(ids["kbf_personal"])
    assert fresh is not None and fresh.verify_status == "ready" and fresh.edited_by_user == []

    for file_id in ("kbf_space", "kbf_team"):
        assert client.get(f"/api/v1/kb/records/{ids[file_id]}", params=params).status_code == 200
    assert client.patch(f"/api/v1/kb/records/{ids['kbf_space']}/verify_status", params=params, json={"status": "approved"}).status_code == 200
    owner = client_for(OWNER)
    assert owner.get(f"/api/v1/kb/records/{ids['kbf_personal']}", params=params).status_code == 200
    assert owner.patch(f"/api/v1/kb/records/{ids['kbf_personal']}/verify_status", params=params, json={"status": "approved"}).status_code == 200
