from __future__ import annotations

import sqlite3

import pytest
from bowhead_core.openapi_store import OpenApiStore


def _schema(path: str) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE team_openapi_keys (id TEXT PRIMARY KEY, team_id TEXT NOT NULL, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, token_prefix TEXT NOT NULL, scopes_json TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT);
        CREATE TABLE team_openapi_call_logs (id TEXT PRIMARY KEY, team_id TEXT, key_id TEXT, key_name TEXT, key_prefix TEXT, http_method TEXT NOT NULL, path TEXT NOT NULL, status_code INTEGER NOT NULL, request_summary TEXT, response_summary TEXT, created_at TEXT NOT NULL);
        """)


@pytest.mark.anyio
async def test_openapi_key_is_one_time_hmac_authenticated_and_log_snapshot_survives_delete(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "test-root")
    db_path = str(tmp_path / "bowhead.db")
    _schema(db_path)
    store = OpenApiStore(db_path)
    key, token = await store.create_key(team_id="team_1", name="Sync", scopes=["users:write"], expires_at=None)
    assert token.startswith("bhk_")
    authenticated = await store.authenticate(token)
    assert authenticated is not None and authenticated["id"] == key["id"]
    await store.log(credential=authenticated, method="POST", path="/api/openapi/v1/users", status_code=201, request_summary="user:create")
    await store.set_status("team_1", key["id"], "deleted")
    assert await store.authenticate(token) is None
    logs = await store.list_logs("team_1")
    assert logs[0]["key_name"] == "Sync"
    assert logs[0]["key_prefix"] == token[:12]


@pytest.mark.anyio
async def test_openapi_key_update_is_team_bound_and_revalidates_scopes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "test-root")
    db_path = str(tmp_path / "bowhead.db")
    _schema(db_path)
    store = OpenApiStore(db_path)
    key, _ = await store.create_key(team_id="team_1", name="Sync", scopes=["users:read"], expires_at=None)
    updated = await store.update_key(team_id="team_1", key_id=key["id"], scopes=["spaces:write"], expires_at="2030-01-01T00:00:00+00:00")
    assert updated["scopes"] == ["spaces:write"]
    assert updated["expires_at"] == "2030-01-01T00:00:00+00:00"
    with pytest.raises(KeyError):
        await store.update_key(team_id="team_2", key_id=key["id"], scopes=["spaces:read"], expires_at=None)
    with pytest.raises(ValueError):
        await store.update_key(team_id="team_1", key_id=key["id"], scopes=["invalid:scope"], expires_at=None)


@pytest.mark.anyio
async def test_openapi_log_retention_prunes_old_rows(tmp_path) -> None:
    db_path = str(tmp_path / "bowhead.db")
    _schema(db_path)
    store = OpenApiStore(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO team_openapi_call_logs VALUES ('old','team_1',NULL,NULL,NULL,'GET','/x',200,NULL,NULL,'2000-01-01T00:00:00+00:00')")
    assert await store.prune_logs(30) == 1
