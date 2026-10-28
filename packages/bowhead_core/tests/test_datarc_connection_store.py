"""workspace-datarc-decouple P2a (R3.1) — DatarcConnectionStore tests."""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest
from bowhead_core.datarc_connection_store import (
    DatarcConnectionStore,
    mask_secret,
    redact_datarc_config,
)
from cryptography.fernet import Fernet


def _set_fernet(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())


async def _store(tmp_path) -> DatarcConnectionStore:
    s = DatarcConnectionStore(str(tmp_path / "bowhead.db"))
    await s.initialize()
    return s


async def test_create_and_get_roundtrip(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    s = await _store(tmp_path)
    c = await s.create_connection(
        team_id="team_a",
        name="datarc.cn",
        base_url="https://demo.datarc.cn",
        auth_key="de0c000000000000abcd",
        account_uid="acc1",
    )
    assert c.id.startswith("dc_") and c.status == "active"
    assert c.key_hint == "abcd"  # last 4
    # the DTO never carries the secret
    assert not hasattr(c, "auth_key") and not hasattr(c, "auth_ref")
    got = await s.get_connection(c.id, "team_a")
    assert got is not None
    assert got.base_url == "https://demo.datarc.cn" and got.account_uid == "acc1"


async def test_get_connection_auth_decrypts(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    s = await _store(tmp_path)
    c = await s.create_connection(team_id="team_a", name="x", base_url="https://d", auth_key="secret-key-123")
    assert await s.get_connection_auth(c.id, "team_a") == "secret-key-123"


async def test_f14_guard_refuses_plaintext_without_fernet(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    s = await _store(tmp_path)
    with pytest.raises(RuntimeError, match="BOWHEAD_FERNET_KEY required"):
        await s.create_connection(team_id="team_a", name="x", base_url="https://d", auth_key="k")


async def test_team_bound_reads_fail_closed(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    s = await _store(tmp_path)
    c = await s.create_connection(team_id="team_a", name="x", base_url="https://d", auth_key="k123")
    assert await s.get_connection(c.id, "team_b") is None  # cross-team → None
    assert await s.get_connection_auth(c.id, "team_b") is None


async def test_list_active_only_and_set_status(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    s = await _store(tmp_path)
    c1 = await s.create_connection(team_id="team_a", name="a", base_url="https://1", auth_key="k1aa")
    c2 = await s.create_connection(team_id="team_a", name="b", base_url="https://2", auth_key="k2bb")
    # Until workspace/data-source bindings exist, an team has one active datarc connection.
    assert {c.id for c in await s.list_connections("team_a")} == {c2.id}
    archived_c1 = await s.get_connection(c1.id, "team_a")
    assert archived_c1 is not None and archived_c1.status == "archived"
    assert await s.set_status(c2.id, "team_a", "archived") is True
    assert await s.list_connections("team_a") == []  # archived excluded
    assert await s.set_status(c1.id, "team_a", "active") is True
    assert {c.id for c in await s.list_connections("team_a")} == {c1.id}
    everything = await s.list_connections("team_a", include_archived=True)
    assert {c.id for c in everything} == {c1.id, c2.id}


async def test_create_archived_connection_preserves_existing_active(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    s = await _store(tmp_path)
    active = await s.create_connection(team_id="team_a", name="active", base_url="https://1", auth_key="k1aa")
    archived = await s.create_connection(
        team_id="team_a",
        name="saved-only",
        base_url="https://2",
        auth_key="k2bb",
        status="archived",
    )

    assert archived.status == "archived"
    assert {c.id for c in await s.list_connections("team_a")} == {active.id}
    assert {c.id for c in await s.list_connections("team_a", include_archived=True)} == {active.id, archived.id}


async def test_initialize_archives_duplicate_active_connections(tmp_path) -> None:
    # Legacy (pre-invariant) DB: table WITHOUT the partial-unique index, two active team_a rows.
    # initialize() must heal the duplicates (keep newest) THEN create the index without crashing.
    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "CREATE TABLE datarc_connection (id TEXT PRIMARY KEY, team_id TEXT NOT NULL, "
            "name TEXT NOT NULL, base_url TEXT NOT NULL, account_uid TEXT NOT NULL DEFAULT '', "
            "auth_ref TEXT, key_hint TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')), "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        for sid, ts in (("dc_old", "2001-01-01T00:00:00+00:00"), ("dc_new", "2002-01-01T00:00:00+00:00")):
            await db.execute(
                "INSERT INTO datarc_connection(id, team_id, name, base_url, account_uid, auth_ref, "
                "key_hint, status, created_at, updated_at) VALUES (?, 'team_a', ?, 'https://x', '', "
                "NULL, '', 'active', ?, ?)",
                (sid, sid, ts, ts),
            )
        await db.commit()
    s = DatarcConnectionStore(str(db_path))
    await s.initialize()
    assert {c.id for c in await s.list_connections("team_a")} == {"dc_new"}  # heal keeps newest
    assert len(await s.list_connections("team_a", include_archived=True)) == 2


async def test_active_unique_index_blocks_direct_dual_active(tmp_path, monkeypatch) -> None:
    # DB-level backstop: even a raw INSERT cannot leave an team with a second active connection.
    _set_fernet(monkeypatch)
    s = await _store(tmp_path)
    await s.create_connection(team_id="team_a", name="a", base_url="https://1", auth_key="k1aa")
    async with aiosqlite.connect(str(tmp_path / "bowhead.db")) as db:
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO datarc_connection(id, team_id, name, base_url, account_uid, auth_ref, "
                "key_hint, status, created_at, updated_at) VALUES "
                "('dc_dup','team_a','dup','https://2','',NULL,'','active',"
                "'2999-01-01T00:00:00+00:00','2999-01-01T00:00:00+00:00')"
            )


async def test_update_connection_auth(tmp_path, monkeypatch) -> None:
    _set_fernet(monkeypatch)
    s = await _store(tmp_path)
    c = await s.create_connection(team_id="team_a", name="x", base_url="https://d", auth_key="old-key-1111")
    assert await s.update_connection_auth(c.id, "team_a", auth_key="new-key-2222") is True
    assert await s.get_connection_auth(c.id, "team_a") == "new-key-2222"
    refreshed = await s.get_connection(c.id, "team_a")
    assert refreshed is not None and refreshed.key_hint == "2222"


async def test_create_empty_auth_no_fernet_ok(tmp_path, monkeypatch) -> None:
    # empty auth_key → no encryption attempted (no F14 raise even without a key), no secret
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    s = await _store(tmp_path)
    c = await s.create_connection(team_id="team_a", name="x", base_url="https://d", auth_key="")
    assert c.key_hint == ""
    assert await s.get_connection_auth(c.id, "team_a") is None


@pytest.mark.unit
def test_mask_secret_shapes() -> None:
    assert mask_secret("de0c-supersecret-abcd") == "de0c***abcd"
    assert mask_secret("short") == "***"  # ≤8 → fully masked
    assert mask_secret("") == ""


@pytest.mark.unit
def test_redact_datarc_config_masks_auth_key_and_ref() -> None:
    cfg = {"base_url": "https://d", "auth_key": "de0c-supersecret-abcd", "name": "prod"}
    out = redact_datarc_config(cfg)
    assert out["auth_key"] == "de0c***abcd"
    assert "supersecret" not in repr(out)
    assert out["base_url"] == "https://d"  # non-secret fields untouched
    assert cfg["auth_key"] == "de0c-supersecret-abcd"  # input not mutated

    # at-rest ciphertext shape → opaque marker, never the token itself
    out2 = redact_datarc_config({"auth_ref": "FERNET:gAAAAA...long-ciphertext"})
    assert out2["auth_ref"] == "[encrypted]"
    assert "gAAAAA" not in repr(out2)
