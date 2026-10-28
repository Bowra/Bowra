"""email-config sprint M1b — store: team email config 回环 + outbox team_id 迁移
+ reissue_invite_token + requeue_dead_smtp。

设计:docs/sprints/email-config-2026-06-12/tech-design.md §1/§5/§10。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore

# Relative to real time: invite expiry (consume/is_active/reissue) is checked against the store's
# wall-clock _now(), so a hardcoded NOW becomes a time-bomb once NOW+expiry passes (it did on 2026-06-19).
NOW = datetime.now(UTC)


@pytest.fixture
async def store(tmp_path):
    us = UserStore(str(tmp_path / "email.db"))
    await us.initialize()
    return us


def _iso(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat()


# ── Team email config round trip ───────────────────────────────────────


async def test_email_config_roundtrip(store: UserStore, monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)  # PLAIN: path
    team = await store.create_team(name="Acme")
    cfg = {
        "host": "smtp.acme.com",
        "port": 587,
        "user": "u",
        "password": "secret",
        "from_addr": "no-reply@acme.com",
        "from_name": "Acme",
        "tls": True,
    }
    await store.update_team_email_config(team.id, cfg)
    got = await store.get_team_email_config_decrypted(team.id)
    assert got["host"] == "smtp.acme.com"
    assert got["password"] == "secret"


async def test_email_config_empty_by_default(store: UserStore) -> None:
    team = await store.create_team(name="Empty")
    assert await store.get_team_email_config_decrypted(team.id) == {}


async def test_email_config_version_increments(store: UserStore) -> None:
    team = await store.create_team(name="Ver")
    _, v0 = await store.get_team_email_config_with_version(team.id)
    assert v0 == 0
    await store.update_team_email_config(team.id, {"host": "a"})
    _, v1 = await store.get_team_email_config_with_version(team.id)
    assert v1 == 1
    await store.clear_team_email_config(team.id)
    cfg, v2 = await store.get_team_email_config_with_version(team.id)
    assert cfg == {} and v2 == 2


async def test_email_config_fernet_roundtrip(store: UserStore, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    team = await store.create_team(name="Crypto")
    await store.update_team_email_config(team.id, {"host": "h", "password": "p"})
    # Persisted ciphertext must have a FERNET: prefix.
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT email_config_encrypted FROM teams WHERE id=?",
            (team.id,),
        ) as cur:
            row = await cur.fetchone()
    assert row["email_config_encrypted"].startswith("FERNET:")
    assert (await store.get_team_email_config_decrypted(team.id))["password"] == "p"


# ── Outbox team_id migration + enqueue passthrough ─────────────────────


async def test_enqueue_smtp_with_team_id(store: UserStore) -> None:
    oid = await store.enqueue_smtp(
        to_email="a@x.com",
        subject="s",
        body="b",
        purpose="test",
        team_id="team_42",
    )
    rows = await store.claim_pending_smtp()
    row = next(r for r in rows if r["id"] == oid)
    assert row["team_id"] == "team_42"  # Column exists and claim SELECT * includes it.


async def test_enqueue_smtp_null_team_default(store: UserStore) -> None:
    oid = await store.enqueue_smtp(to_email="a@x.com", subject="s", body="b", purpose="test")
    rows = await store.claim_pending_smtp()
    assert next(r for r in rows if r["id"] == oid)["team_id"] is None


# ── requeue_dead_smtp ───────────────────────────────────────────


async def _mark_dead(store: UserStore, oid: str) -> None:
    await store.mark_smtp_failed(oid, "boom", next_attempt_at=None)


async def test_requeue_dead_smtp_team_scoped(store: UserStore) -> None:
    a = await store.enqueue_smtp(to_email="a@x.com", subject="s", body="b", purpose="test", team_id="team_1")
    b = await store.enqueue_smtp(to_email="b@x.com", subject="s", body="b", purpose="test", team_id="team_2")
    await store.claim_pending_smtp()  # attempts++ → 'sending'
    await _mark_dead(store, a)
    await _mark_dead(store, b)
    n = await store.requeue_dead_smtp("team_1")
    assert n == 1
    revived = {r["id"] for r in await store.claim_pending_smtp()}
    assert a in revived and b not in revived  # Revive team_1 only.


async def test_requeue_dead_smtp_null_team(store: UserStore) -> None:
    a = await store.enqueue_smtp(to_email="a@x.com", subject="s", body="b", purpose="test")
    await store.claim_pending_smtp()
    await _mark_dead(store, a)
    assert await store.requeue_dead_smtp(None) == 1


async def test_requeue_skips_rows_older_than_7d(store: UserStore) -> None:
    # review-fix: do not revive dead letters (dead links) older than 7d.
    a = await store.enqueue_smtp(to_email="a@x.com", subject="s", body="b", purpose="test", team_id="team_1")
    await store.claim_pending_smtp()
    await _mark_dead(store, a)
    old = (NOW - timedelta(days=10)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE smtp_outbox SET created_at = ? WHERE id = ?", (old, a))
        await db.commit()
    assert await store.requeue_dead_smtp("team_1") == 0  # Too old: skip.


# ── reissue_invite_token ────────────────────────────────────────


async def test_reissue_invalidates_old_activates_new(store: UserStore) -> None:
    team = await store.create_team(name="Inv")
    inv = await store.record_invite(
        workspace_id="ws_1",
        team_id=team.id,
        role="member",
        invited_email="new@x.com",
        raw_token="OLD_TOKEN",
        expires_at=_iso(7),
    )
    assert await store.is_invite_active("OLD_TOKEN") is True
    ok = await store.reissue_invite_token(inv, team.id, "NEW_TOKEN", _iso(7))
    assert ok is True
    assert await store.is_invite_active("OLD_TOKEN") is False  # Old token inactive.
    assert await store.is_invite_active("NEW_TOKEN") is True  # New token active.


async def test_reissue_cross_team_rejected(store: UserStore) -> None:
    team = await store.create_team(name="A")
    inv = await store.record_invite(
        workspace_id="ws_1",
        team_id=team.id,
        role="member",
        invited_email="n@x.com",
        raw_token="T",
        expires_at=_iso(7),
    )
    assert await store.reissue_invite_token(inv, "team_other", "T2", _iso(7)) is False
    assert await store.is_invite_active("T") is True  # Unchanged


async def test_reissue_consumed_rejected(store: UserStore) -> None:
    team = await store.create_team(name="C")
    inv = await store.record_invite(
        workspace_id="ws_1",
        team_id=team.id,
        role="member",
        invited_email="n@x.com",
        raw_token="T",
        expires_at=_iso(7),
    )
    await store.consume_invite("T")
    assert await store.reissue_invite_token(inv, team.id, "T2", _iso(7)) is False


async def test_reissue_null_team_row_allowed(store: UserStore) -> None:
    inv = await store.record_invite(
        workspace_id="ws_1",
        team_id=None,
        role="member",
        invited_email="n@x.com",
        raw_token="T",
        expires_at=_iso(7),
    )
    assert await store.reissue_invite_token(inv, "team_any", "T2", _iso(7)) is True
