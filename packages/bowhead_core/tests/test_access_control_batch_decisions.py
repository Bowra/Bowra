"""explain_decisions (batch) equals per-id explain_decision across kinds, permissions, principals and grant states."""

from __future__ import annotations

import contextlib
import random
import sqlite3
import uuid

import pytest
from bowhead_core.access_control import AccessContext, AccessControlStore

# Exhaustive authorization cross-checks; excluded from the fast unit run.
pytestmark = pytest.mark.slow

TEAM = "t1"
KINDS = ["kb_file", "skill", "kb_scope"]
PERMS = ["read", "write", "use", "invoke", "admin"]
PRINCIPALS = [
    ("user", "u1"),
    ("user", "u2"),
    ("group", "g1"),
    ("group", "g2"),
    ("workspace", "w1"),
    ("workspace", "w2"),
    ("team", TEAM),
    ("team", "t2"),
]


def _seed(db_path, rng: random.Random, ids: list[str], n: int, distinct_ts: bool) -> None:
    with sqlite3.connect(db_path) as db:
        for i in range(n):
            ptype, pid = rng.choice(PRINCIPALS)
            team = rng.choice([TEAM, TEAM, TEAM, "t2"])
            kind = rng.choice(KINDS)
            rid = rng.choice(ids)
            perm = rng.choice(["read", "write", "use", "invoke", "admin", "READ"])
            effect = rng.choice(["allow", "deny"])
            ts = f"2026-01-01T00:00:{i:02d}.{i:06d}+00:00" if distinct_ts else "2026-01-01T00:00:00+00:00"
            expires = rng.choice([None, None, "2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"])
            revoked = rng.choice([None, None, None, "2026-01-02T00:00:00+00:00"])
            with contextlib.suppress(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO resource_grants (id, team_id, resource_kind, resource_id, principal_type, principal_id, permission, effect,"
                    " created_by, created_at, updated_at, expires_at, revoked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"rg_{uuid.uuid4().hex}", team, kind, rid, ptype, pid, perm, effect, None, ts, ts, expires, revoked),
                )


CTXS = [
    AccessContext(user_id="u1", team_id=TEAM, workspace_id="w1", workspace_role="member", group_ids=("g1",)),
    AccessContext(user_id="u2", team_id=TEAM, workspace_id="", workspace_role="member"),
    AccessContext(user_id="u1", team_id=TEAM, workspace_id="w2", workspace_role="ws_admin", group_ids=("g1", "g2")),
    AccessContext(user_id="", team_id=TEAM, workspace_id="", workspace_role="member"),
    AccessContext(user_id="u3", team_id="t2", workspace_id="w1", workspace_role="member"),
]


def _granted_ids(db_path) -> set[str]:
    with sqlite3.connect(db_path) as db:
        return {row[0] for row in db.execute("SELECT DISTINCT resource_id FROM resource_grants")}


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("enforce", ["0", "1"])
@pytest.mark.parametrize("distinct_ts", [True, False])
async def test_batch_equals_single(tmp_path, monkeypatch, seed, enforce, distinct_ts):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", enforce)
    store = AccessControlStore(tmp_path / "a.db")
    await store.initialize()
    rng = random.Random(seed)
    ids = [f"f{i}" for i in range(rng.choice([5, 30, 450]))]
    _seed(tmp_path / "a.db", rng, ids, 400 if len(ids) > 100 else 120, distinct_ts)
    # The batch call always covers every id (450 ids span two IN chunks); the per-id
    # oracle runs on ids that carry grants plus a few without, which is where decisions differ.
    checked = sorted(_granted_ids(tmp_path / "a.db") & set(ids))[:16] + ids[:3]
    compared = 0
    for ctx in CTXS:
        for as_dict in (False, True):
            c = ctx
            if as_dict:
                c = {"user_id": ctx.user_id, "team_id": ctx.team_id, "workspace_id": ctx.workspace_id, "group_ids": list(ctx.group_ids)}
            for kind in KINDS:
                for perm in PERMS:
                    query = ids + ids[:3] + [""]
                    batch = await store.explain_decisions(c, kind, query, perm)
                    assert set(batch) == set(ids)
                    for rid in checked:
                        single = await store.explain_decision(c, kind, rid, perm)
                        b = batch[rid]
                        if distinct_ts:
                            assert b == single, (ctx, kind, perm, rid, b, single)
                        else:
                            assert {k: b[k] for k in ("allowed", "reason", "shadow")} == {k: single[k] for k in ("allowed", "reason", "shadow")}
                        compared += 1
    assert compared > 1000
