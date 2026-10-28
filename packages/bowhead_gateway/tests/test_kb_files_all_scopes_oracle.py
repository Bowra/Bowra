"""Independent black-box oracle for GET /api/v1/kb/files?scope=all, written from the permission rules, not the implementation."""

from __future__ import annotations

import base64
import json
import random
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.user_store import User
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Exhaustive authorization cross-checks; excluded from the fast unit run.
pytestmark = pytest.mark.slow

TEAM, OTHER = "team_main", "team_other"
WA, WB, WARCH = "ws_a", "ws_b", "ws_arch"
USERS = ["u_mem", "u_sadmin", "u_tadmin", "u_owner", "u_out"]
SPACE_ROLE = {
    (WA, "u_mem"): "member",
    (WA, "u_sadmin"): "ws_admin",
    (WA, "u_tadmin"): "member",
    (WARCH, "u_mem"): "member",
    (WARCH, "u_sadmin"): "ws_admin",
    (WB, "u_out"): "member",
    (WB, "u_sadmin"): "member",
}
TEAM_ROLE = {"u_mem": "member", "u_sadmin": "member", "u_tadmin": "team_admin", "u_owner": "owner", "u_out": "member"}
HEADERS = [None, WA, WB, WARCH, "ws_forged"]


class _Users:
    status = {WA: "active", WB: "active", WARCH: "archived"}

    async def get_user_team_id(self, user_id):
        return TEAM

    async def get_workspace_team_id(self, ws):
        return TEAM if ws in self.status else None

    async def get_member_role(self, ws, user_id):
        return SPACE_ROLE.get((ws, user_id))

    async def get_workspace(self, ws):
        s = self.status.get(ws)
        return {"id": ws, "status": s} if s else None

    async def get_team_role(self, user_id, team_id):
        return TEAM_ROLE.get(user_id) if team_id == TEAM else None


@pytest.fixture
async def env(initialize_migrated_kb_files_database, tmp_path):
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(kb.router)
    app.state.kb_files_store = files
    app.state.access_control_store = access
    app.state.user_store = _Users()
    cur = {"u": "u_mem"}
    app.dependency_overrides[_current_user] = lambda: User(id=cur["u"], email="x@example.test", team_id=TEAM, tier="ultra", email_verified=True)
    return SimpleNamespace(
        c=TestClient(app, raise_server_exceptions=False), files=files, access=access, fdb=tmp_path / "files.db", adb=tmp_path / "access.db", cur=cur
    )


async def _seed(env, rng):
    rows = []
    g1 = await env.access.create_group(team_id=TEAM, name="g1")
    with sqlite3.connect(env.adb) as db:
        for u in ("u_mem", "u_sadmin"):
            db.execute("INSERT INTO group_memberships (team_id, group_id, principal_id, created_at) VALUES (?,?,?,?)", (TEAM, g1.id, u, "2026-01-01"))
    for i in range(120):
        kind = rng.choice(["personal", "space", "workspace", "team", "weird"])
        owner = rng.choice(USERS + ["u_stranger"])
        ws = rng.choice([WA, WB, WARCH, "ws_forged", ""])
        team_id = rng.choice([TEAM] * 5 + [OTHER])
        if kind == "personal":
            scope, sid, wsid = "personal", owner, ws
        elif kind in ("space", "workspace"):
            scope, sid, wsid = kind, ws or WA, ws or WA
            if rng.random() < 0.15:  # inconsistent row
                sid = rng.choice([WA, WB])
        elif kind == "team":
            scope, sid, wsid = "team", rng.choice([TEAM, TEAM, OTHER]), ""
        else:
            scope, sid, wsid = rng.choice(["team", "space"]), rng.choice([TEAM, WA, owner]), rng.choice(["", WA])
        fid = await env.files.create(
            artifact_id=f"art{i}",
            filename=f"n{i % 9}.md",
            mime_type="text/markdown",
            size_bytes=rng.randint(1, 4),
            scope=scope,
            scope_id=sid,
            workspace_id=wsid,
            team_id=team_id,
            uploaded_by=owner,
            initial_parse_status="done",
        )
        rows.append({"fid": fid, "scope": scope, "sid": sid, "wsid": wsid, "team": team_id})
    with sqlite3.connect(env.fdb) as db:
        for r in rows:
            db.execute("UPDATE kb_files SET uploaded_at=? WHERE file_id=?", (f"2026-09-0{rng.randint(1, 3)}T00:00:00", r["fid"]))
    deleted = set()
    for r in rng.sample(rows, 12):
        await env.files.soft_delete(r["fid"])
        deleted.add(r["fid"])
    grants = []
    principals = [("user", u) for u in USERS] + [("group", g1.id), ("workspace", WA), ("workspace", WB), ("team", TEAM)]
    with sqlite3.connect(env.adb) as db:
        for r in rng.sample(rows, 40):
            for _ in range(rng.randint(1, 2)):
                p = rng.choice(principals)
                g = {
                    "fid": r["fid"],
                    "kind": "kb_file",
                    "ptype": p[0],
                    "pid": p[1],
                    "perm": rng.choice(["read", "write", "admin", "use"]),
                    "effect": rng.choice(["allow", "deny"]),
                    "team": rng.choice([TEAM] * 4 + [OTHER]),
                    "exp": rng.choice([None, None, "2000-01-01T00:00:00+00:00"]),
                    "rev": rng.choice([None, None, "2026-01-01"]),
                }
                try:
                    db.execute(
                        "INSERT INTO resource_grants VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
                        (
                            f"rg_{uuid.uuid4().hex}",
                            g["team"],
                            g["kind"],
                            g["fid"],
                            g["ptype"],
                            g["pid"],
                            g["perm"],
                            g["effect"],
                            "2026-01-01",
                            "2026-01-01",
                            g["exp"],
                            g["rev"],
                        ),
                    )
                    grants.append(g)
                except sqlite3.IntegrityError:
                    pass
        # scope grants: deny team scope to u_out, deny space WA to u_sadmin
        db.execute(
            "INSERT INTO resource_grants VALUES (?,?,?,?,?,?,?,?,NULL,?,?,NULL,NULL)",
            ("rg_s1", TEAM, "kb_scope", f"team:{TEAM}", "user", "u_out", "read", "deny", "2026-01-01", "2026-01-01"),
        )
        db.execute(
            "INSERT INTO resource_grants VALUES (?,?,?,?,?,?,?,?,NULL,?,?,NULL,NULL)",
            ("rg_s2", TEAM, "kb_scope", f"workspace:{WA}", "user", "u_sadmin", "read", "deny", "2026-01-01", "2026-01-01"),
        )
    return rows, deleted, grants, g1.id


def _covers(perm):
    return perm in ("read", "write", "admin")


def _file_ok(fid, user, header, grants, gid):
    me = {("user", user), ("team", TEAM)}
    if user in ("u_mem", "u_sadmin"):
        me.add(("group", gid))
    if header:
        me.add(("workspace", header))
    active = [g for g in grants if g["fid"] == fid and g["team"] == TEAM and g["rev"] is None and g["exp"] is None]
    mine = [g for g in active if (g["ptype"], g["pid"]) in me and _covers(g["perm"])]
    if any(g["effect"] == "deny" for g in mine):
        return False
    if any(g["effect"] == "allow" for g in mine):
        return True
    return not any(g["effect"] == "allow" for g in active)


def _expected(rows, deleted, grants, gid, user, header, enforce):
    header_ok = header is None or SPACE_ROLE.get((header, user)) is not None
    if not header_ok:
        return None
    out = set()
    for r in rows:
        if r["fid"] in deleted or r["team"] != TEAM:
            continue
        if r["scope"] == "personal":
            ok = r["sid"] == user
        elif r["scope"] in ("space", "workspace"):
            ok = header is not None and r["sid"] == header and r["wsid"] == header
            if enforce and user == "u_sadmin" and header == WA:
                ok = False
        else:
            ok = r["sid"] == TEAM
            if enforce and user == "u_out":
                ok = False
        if ok and enforce and not _file_ok(r["fid"], user, header, grants, gid):
            ok = False
        if ok:
            out.add(r["fid"])
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("enforce", ["1", "0"])
async def test_all_matches_independent_oracle(env, monkeypatch, seed, enforce):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", enforce)
    rng = random.Random(seed)
    rows, deleted, grants, gid = await _seed(env, rng)
    all_fids = {r["fid"] for r in rows}
    nonempty = 0
    for user in USERS:
        env.cur["u"] = user
        for header in HEADERS:
            h = {"X-Space-Id": header} if header else {}
            resp = env.c.get("/api/v1/kb/files", params={"scope": "all", "limit": 1000}, headers=h)
            exp = _expected(rows, deleted, grants, gid, user, header, enforce == "1")
            assert resp.status_code != 500, resp.text
            if exp is None:
                assert resp.status_code in (403, 404), (user, header, resp.status_code)
                assert not any(fid in resp.text for fid in all_fids)
                continue
            assert resp.status_code == 200, (user, header, resp.text)
            got = {f["file_id"] for f in resp.json()["files"]}
            scopes = {f["file_id"]: f["scope"] for f in resp.json()["files"]}
            assert got == exp, (user, header, enforce, got - exp, exp - got)
            nonempty += bool(got)
            # items carry canonical scope
            for r in rows:
                if r["fid"] in got:
                    assert scopes[r["fid"]] == ("space" if r["scope"] == "workspace" else r["scope"])
            if header:
                for r in rows:
                    st = env.c.get(f"/api/v1/kb/files/{r['fid']}", headers=h).status_code
                    if r["fid"] in got:
                        assert st == 200, (user, header, r)
                    else:
                        # Listing may under-report (team file with ws principal etc.) but never over-report.
                        pass
    assert nonempty > 5


def _cur(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


@pytest.mark.parametrize("enforce", ["1", "0"])
async def test_tampered_cursor_never_500_and_never_leaks(env, monkeypatch, enforce):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", enforce)
    rows, *_ = await _seed(env, random.Random(9))
    env.cur["u"] = "u_mem"
    h = {"X-Space-Id": WA}
    cases = []
    for col, d in [("uploaded_at", "desc"), ("uploaded_at", "asc"), ("filename", "asc"), ("size_bytes", "desc")]:
        base = {"order_by": col, "order_dir": d}
        full = {f["file_id"] for f in env.c.get("/api/v1/kb/files", params={"scope": "all", "limit": 1000, **base}, headers=h).json()["files"]}
        for v in ["", "zzzz", "0", -1, 10**30, True, None, [], {}, 1.5, "' OR 1=1 --", "\x00", "9" * 10000]:
            for fid in ["", "kbf_x", "' OR 1=1 --", 5, None]:
                cases.append((base, full, _cur({"c": col, "d": d, "v": v, "id": fid})))
        for raw in ["", "=", "!!!", "a" * 5, base64.urlsafe_b64encode(b"\xff\xfe").decode(), _cur([1, 2]), _cur("s"), _cur(None), _cur({"c": col})]:
            cases.append((base, full, raw))
    for base, full, cursor in cases:
        resp = env.c.get("/api/v1/kb/files", params={"scope": "all", "limit": 1000, "cursor": cursor, **base}, headers=h)
        assert resp.status_code in (200, 400), (cursor, resp.status_code, resp.text[:300])
        if resp.status_code == 200:
            got = {f["file_id"] for f in resp.json()["files"]}
            assert got <= full
        else:
            assert resp.json()["detail"]["error"]["code"] == "invalid_kb_pagination"
