"""Sprint α schema + new method tests (Phase 1 deliverable).

Covers:
- ALTER user.verify_token_jti + datarc_member_uid idempotent
- CREATE smtp_outbox + rate_limit_buckets idempotent
- set_team_creator returns bool (PRD r4 G6)
- find_default_team_for_domain
- get_user_team_id (Sec P0-S-6 fresh re-select)
- verify token jti get/set/clear
- mark_email_verified_atomic (jti match + jti None compat)
- enqueue_smtp + claim_pending_smtp + mark_sent/failed
- rate_limit_check window math
- update_user_datarc_member_uid
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _now


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "test.db")
    await s.initialize()
    return s


async def _seed_team(s: UserStore, *, team_id: str = "team_a", domain: str | None = "test.com") -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, "
            "default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, f"Team {team_id}", domain, 10, "ultra", 0, 0, '{"pro":5000,"ultra":30000}', "active", _now()),
        )
        await db.commit()


async def _seed_user(s: UserStore, *, user_id: str, team_id: str, email: str) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, tier, "
            "credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, 0, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, _now()),
        )
        await db.commit()


# ─────────────────────────────────────────────────────────
# Schema migration idempotency
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_idempotent_second_call(tmp_path):
    s = UserStore(tmp_path / "x.db")
    await s.initialize()
    await s.initialize()  # 2nd call should not error
    # Verify columns + tables present
    async with aiosqlite.connect(s.db_path) as db, db.execute("PRAGMA table_info(users)") as cur:
        cols = [r[1] async for r in cur]
    assert "verify_token_jti" in cols
    assert "datarc_member_uid" in cols
    assert "email_verified" in cols


@pytest.mark.asyncio
async def test_smtp_outbox_table_present(store):
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='smtp_outbox'") as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_rate_limit_buckets_table_present(store):
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rate_limit_buckets'") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None


# ─────────────────────────────────────────────────────────
# set_team_creator returns bool
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_team_creator_returns_true_first_time(store):
    await _seed_team(store, team_id="team_x")
    await _seed_user(store, user_id="u1", team_id="team_x", email="u1@test.com")
    became_creator = await store.set_team_creator("team_x", "u1")
    assert became_creator is True


@pytest.mark.asyncio
async def test_set_team_creator_returns_false_when_already_set(store):
    await _seed_team(store, team_id="team_x")
    await _seed_user(store, user_id="u1", team_id="team_x", email="u1@test.com")
    await _seed_user(store, user_id="u2", team_id="team_x", email="u2@test.com")
    assert (await store.set_team_creator("team_x", "u1")) is True
    # Second user tries — already set
    assert (await store.set_team_creator("team_x", "u2")) is False
    # Verify u1 still creator
    assert await store.is_team_admin("u1", "team_x") is True
    assert await store.is_team_admin("u2", "team_x") is False


@pytest.mark.asyncio
async def test_set_team_creator_concurrent_only_one_wins(store):
    await _seed_team(store, team_id="team_x")
    for i in range(3):
        await _seed_user(store, user_id=f"u{i}", team_id="team_x", email=f"u{i}@test.com")
    # Concurrent set_team_creator — exactly 1 wins
    results = await asyncio.gather(
        store.set_team_creator("team_x", "u0"),
        store.set_team_creator("team_x", "u1"),
        store.set_team_creator("team_x", "u2"),
    )
    assert sum(results) == 1  # exactly one True


# ─────────────────────────────────────────────────────────
# find_default_team_for_domain
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_default_team_for_domain_matches(store):
    await _seed_team(store, team_id="team_d", domain="datarc.cn")
    team = await store.find_default_team_for_domain("user@datarc.cn")
    assert team is not None
    assert team.id == "team_d"


@pytest.mark.asyncio
async def test_find_default_team_for_domain_case_insensitive(store):
    await _seed_team(store, team_id="team_d", domain="DataRC.cn")
    team = await store.find_default_team_for_domain("user@datarc.cn")
    assert team is not None


@pytest.mark.asyncio
async def test_find_default_team_for_domain_none_when_no_match(store):
    await _seed_team(store, team_id="team_d", domain="datarc.cn")
    team = await store.find_default_team_for_domain("user@other.com")
    assert team is None


@pytest.mark.asyncio
async def test_find_default_team_for_domain_returns_oldest_when_dupes(store):
    await _seed_team(store, team_id="team_old", domain="datarc.cn")
    await asyncio.sleep(0.01)
    await _seed_team(store, team_id="team_new", domain="datarc.cn")
    team = await store.find_default_team_for_domain("user@datarc.cn")
    assert team is not None
    assert team.id == "team_old"


# ─────────────────────────────────────────────────────────
# get_user_team_id (Sec P0-S-6)
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user_team_id_returns_team(store):
    await _seed_team(store, team_id="team_a")
    await _seed_user(store, user_id="u1", team_id="team_a", email="u1@test.com")
    assert await store.get_user_team_id("u1") == "team_a"


@pytest.mark.asyncio
async def test_get_user_team_id_none_for_unknown(store):
    assert await store.get_user_team_id("nonexistent") is None


# ─────────────────────────────────────────────────────────
# verify token jti
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_token_jti_set_get_clear(store):
    await _seed_team(store, team_id="team_a")
    await _seed_user(store, user_id="u1", team_id="team_a", email="u1@test.com")
    assert await store.get_verify_token_jti("u1") is None
    await store.update_verify_token_jti("u1", "jti_abc")
    assert await store.get_verify_token_jti("u1") == "jti_abc"
    await store.update_verify_token_jti("u1", None)
    assert await store.get_verify_token_jti("u1") is None


@pytest.mark.asyncio
async def test_mark_email_verified_atomic_match_jti(store):
    await _seed_team(store, team_id="team_a")
    await _seed_user(store, user_id="u1", team_id="team_a", email="u1@test.com")
    await store.update_verify_token_jti("u1", "jti_v1")

    # Matching jti — succeeds
    ok = await store.mark_email_verified_atomic("u1", "jti_v1")
    assert ok is True
    # Verify state
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT email_verified, verify_token_jti FROM users WHERE id='u1'") as cur:
        row = await cur.fetchone()
    assert row[0] == 1  # verified
    assert row[1] is None  # jti cleared


@pytest.mark.asyncio
async def test_mark_email_verified_atomic_wrong_jti_rejected(store):
    await _seed_team(store, team_id="team_a")
    await _seed_user(store, user_id="u1", team_id="team_a", email="u1@test.com")
    await store.update_verify_token_jti("u1", "jti_v1")

    # Wrong jti — fails
    ok = await store.mark_email_verified_atomic("u1", "jti_v2_wrong")
    assert ok is False


@pytest.mark.asyncio
async def test_mark_email_verified_atomic_already_verified(store):
    await _seed_team(store, team_id="team_a")
    await _seed_user(store, user_id="u1", team_id="team_a", email="u1@test.com")
    await store.update_verify_token_jti("u1", "jti_v1")

    # First verify — succeeds
    assert (await store.mark_email_verified_atomic("u1", "jti_v1")) is True
    # Replay — fails (already verified + jti cleared)
    assert (await store.mark_email_verified_atomic("u1", "jti_v1")) is False


@pytest.mark.asyncio
async def test_mark_email_verified_atomic_jti_none_compat(store):
    """Q8 backward compat: old token without jti claim still verifies."""
    await _seed_team(store, team_id="team_a")
    await _seed_user(store, user_id="u1", team_id="team_a", email="u1@test.com")
    # No jti set
    ok = await store.mark_email_verified_atomic("u1", None)
    assert ok is True


# ─────────────────────────────────────────────────────────
# update_user_datarc_member_uid
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_user_datarc_member_uid(store):
    await _seed_team(store, team_id="team_a")
    await _seed_user(store, user_id="u1", team_id="team_a", email="u1@datarc.cn")

    ok = await store.update_user_datarc_member_uid("u1", "mem_abc123")
    assert ok is True

    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT datarc_member_uid FROM users WHERE id='u1'") as cur:
        row = await cur.fetchone()
    assert row[0] == "mem_abc123"


# ─────────────────────────────────────────────────────────
# SMTP outbox
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_smtp_creates_pending_row(store):
    oid = await store.enqueue_smtp(
        to_email="u@test.com",
        subject="hi",
        body="content",
        purpose="verify",
    )
    assert oid.startswith("smtp_")
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT status, attempts FROM smtp_outbox WHERE id=?", (oid,)) as cur:
        row = await cur.fetchone()
    assert row[0] == "pending"
    assert row[1] == 0


@pytest.mark.asyncio
async def test_claim_pending_smtp_atomic(store):
    oid = await store.enqueue_smtp(
        to_email="u@test.com",
        subject="s",
        body="b",
        purpose="verify",
    )
    batch = await store.claim_pending_smtp(batch_size=10)
    assert len(batch) == 1
    assert batch[0]["id"] == oid
    # Second claim — not returned (status='sending')
    batch2 = await store.claim_pending_smtp(batch_size=10)
    assert len(batch2) == 0


@pytest.mark.asyncio
async def test_mark_smtp_sent_marks_sent(store):
    oid = await store.enqueue_smtp(
        to_email="u@test.com",
        subject="s",
        body="b",
        purpose="verify",
    )
    await store.claim_pending_smtp(batch_size=10)
    ok = await store.mark_smtp_sent(oid)
    assert ok is True
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT status, sent_at FROM smtp_outbox WHERE id=?", (oid,)) as cur:
        row = await cur.fetchone()
    assert row[0] == "sent"
    assert row[1] is not None


@pytest.mark.asyncio
async def test_mark_smtp_failed_retry_then_dead(store):
    oid = await store.enqueue_smtp(
        to_email="u@test.com",
        subject="s",
        body="b",
        purpose="verify",
    )
    await store.claim_pending_smtp(batch_size=10)
    # First failure — retry
    await store.mark_smtp_failed(oid, "smtp timeout", "2026-05-26T00:01:00+00:00")
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT status FROM smtp_outbox WHERE id=?", (oid,)) as cur:
        row = await cur.fetchone()
    assert row[0] == "failed_retry"
    # Final failure — dead
    await store.mark_smtp_failed(oid, "smtp timeout", None)
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT status FROM smtp_outbox WHERE id=?", (oid,)) as cur:
        row = await cur.fetchone()
    assert row[0] == "dead"


# ─────────────────────────────────────────────────────────
# Rate limit check
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_check_allows_under_limit(store):
    for i in range(3):
        ok = await store.rate_limit_check("test:bucket", limit=3, window_s=60)
        assert ok is True, f"call {i} should pass"


@pytest.mark.asyncio
async def test_rate_limit_check_rejects_over_limit(store):
    for _ in range(3):
        await store.rate_limit_check("test:bucket", limit=3, window_s=60)
    # 4th — over
    ok = await store.rate_limit_check("test:bucket", limit=3, window_s=60)
    assert ok is False


@pytest.mark.asyncio
async def test_rate_limit_buckets_isolated_by_key(store):
    await store.rate_limit_check("bucket_A", limit=1, window_s=60)
    await store.rate_limit_check("bucket_A", limit=1, window_s=60)  # over
    # bucket_B not affected
    ok = await store.rate_limit_check("bucket_B", limit=1, window_s=60)
    assert ok is True
