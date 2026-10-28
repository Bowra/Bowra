"""迁移 0062 + FeedbackCaseworkStore（B 线批 3a）契约。

覆盖：core-only 建表/非 core no-op/重跑幂等；行级互斥 claim；failed 计 attempts；blocked 只在
「翻到 blocked」时报 fresh；日配额按 UTC 日计 started_at；redraft 槽每日 ≤3；过期回执
（sweep_expired 谁翻的都补、mark_receipt 后不再返回）。全部走真迁移建库。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.feedback_candidate_store import FeedbackCandidateStore
from bowhead_core.feedback_casework_store import (
    REDRAFTS_PER_DAY,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    FeedbackCaseworkStore,
)

from bowhead_core import migrations

pytestmark = pytest.mark.integration


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    return str(path)


def test_0062_creates_sidecar_only_on_core_and_is_repeatable(tmp_path: Path) -> None:
    core = tmp_path / "core.db"
    command.upgrade(_config(core), "head")
    command.upgrade(_config(core), "head")  # Idempotent rerun (matching column sets cause no drift error)
    with sqlite3.connect(core) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(feedback_casework)")}
        assert {"candidate_id", "status", "reason_code", "prefill_json", "receipt_at", "redraft_day"} <= cols
        idx = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_fbcw_team_status", "idx_fbcw_status_updated"} <= idx
        # Candidate columns stay untouched (0059 checks exact equality; migration history is immutable).
        assert "receipt_at" not in {r[1] for r in db.execute("PRAGMA table_info(feedback_candidates)")}
    kb = tmp_path / "kb.db"
    command.upgrade(_config(kb, target="files"), "head")
    with sqlite3.connect(kb) as db:
        assert "feedback_casework" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


async def test_claim_is_row_level_mutual_exclusion_with_stale_reclaim(db_path: str) -> None:
    store = FeedbackCaseworkStore(db_path)
    assert await store.claim("fbc_1", "t1", trigger="cron", stale_after_s=900) is True
    assert await store.claim("fbc_1", "t1", trigger="cron", stale_after_s=900) is False  # Cannot be claimed by others while running
    row = await store.get("fbc_1")
    assert row is not None and row.status == STATUS_RUNNING
    # A timed-out running row can be reclaimed.
    assert await store.claim("fbc_1", "t1", trigger="cron", stale_after_s=0) is True


async def test_failed_counts_attempts_and_blocked_reports_fresh_once(db_path: str) -> None:
    store = FeedbackCaseworkStore(db_path)
    await store.claim("fbc_2", "t1", trigger="cron", stale_after_s=900)
    await store.fail("fbc_2", reason_code="coach_unavailable")
    row = await store.get("fbc_2")
    assert row is not None and row.status == STATUS_FAILED and row.attempts == 1
    assert await store.block("fbc_2", reason_code="credit_blocked") is True  # Transition to blocked: fresh
    assert await store.block("fbc_2", reason_code="credit_blocked") is False  # Same blocking reason again: not fresh
    row = await store.get("fbc_2")
    assert row is not None and row.status == STATUS_BLOCKED and row.attempts == 1  # Blocking does not count toward attempts.


async def test_daily_cap_counts_only_coach_calls_per_utc_day(db_path: str) -> None:
    store = FeedbackCaseworkStore(db_path)
    now = datetime.now(UTC)
    await store.claim("fbc_a", "t1", trigger="cron", stale_after_s=900)
    await store.mark_coaching("fbc_a")
    await store.claim("fbc_b", "t1", trigger="cron", stale_after_s=900)
    await store.mark_coaching("fbc_b")
    await store.claim("fbc_c", "t1", trigger="cron", stale_after_s=900)  # Claimed but never reached the model (surface failure, etc.): not counted
    await store.ensure_row("fbc_d", "t1", trigger="cron")
    await store.block("fbc_d", reason_code="credit_blocked")  # blocked does not count
    assert await store.count_started_today("t1", now) == 2
    assert await store.count_started_today("t1", now + timedelta(days=1)) == 0


async def test_worker_writes_never_clobber_an_explicit_cache(db_path: str) -> None:
    """running 期间采纳路由 replace_cache 覆盖了行 → worker 的 finish/fail(running_only) 写不进去。"""

    store = FeedbackCaseworkStore(db_path)
    await store.claim("fbc_w", "t1", trigger="cron", stale_after_s=900)
    await store.replace_cache("fbc_w", "t1", report={"verdict": "edit"}, prefill={"edited_content": "路由稿"}, base={"definition_sha256": "x"})
    assert await store.fail("fbc_w", reason_code="coach_unavailable", running_only=True) is False
    assert await store.finish("fbc_w", report={"verdict": "edit"}, prefill={"edited_content": "后台稿"}, base={}, running_only=True) is False
    row = await store.get("fbc_w")
    assert row is not None and row.status == STATUS_DONE and row.prefill == {"edited_content": "路由稿"} and row.attempts == 0
    # block also leaves running rows untouched.
    await store.claim("fbc_x", "t1", trigger="cron", stale_after_s=900)
    assert await store.block("fbc_x", reason_code="credit_blocked") is False
    assert (await store.get("fbc_x")).status == STATUS_RUNNING


async def test_invalidate_only_voids_done_cache_without_burning_attempts(db_path: str) -> None:
    store = FeedbackCaseworkStore(db_path)
    await store.claim("fbc_i", "t1", trigger="cron", stale_after_s=900)
    await store.invalidate("fbc_i")  # running stays unchanged
    assert (await store.get("fbc_i")).status == STATUS_RUNNING
    await store.finish("fbc_i", report={"verdict": "edit"}, prefill={"edited_content": "v"}, base={"definition_sha256": "x"})
    await store.invalidate("fbc_i")
    row = await store.get("fbc_i")
    assert row.status == "queued" and row.reason_code == "redraft_requested" and row.prefill is None and row.attempts == 0
    pub = row.public(reviewer=True)
    assert pub["verdict"] is None and pub["drafted_at"] is None  # Invalidated projection no longer carries the old verdict.


async def test_redraft_slot_is_capped_per_day(db_path: str) -> None:
    store = FeedbackCaseworkStore(db_path)
    now = datetime.now(UTC)
    for _ in range(REDRAFTS_PER_DAY):
        assert await store.take_redraft_slot("fbc_r", "t1", now) is True
    assert await store.take_redraft_slot("fbc_r", "t1", now) is False
    assert await store.take_redraft_slot("fbc_r", "t1", now + timedelta(days=1)) is True  # Reset on a new day


async def test_finish_stores_cache_and_public_projection_hides_prefill(db_path: str) -> None:
    store = FeedbackCaseworkStore(db_path)
    await store.claim("fbc_d", "t1", trigger="major_case", stale_after_s=900)
    await store.finish(
        "fbc_d",
        report={"verdict": "reroute", "reroute": {"reason": "r", "target_kind": "team_skill", "target_ref": "sk"}},
        prefill=None,
        base={"definition_sha256": "x"},
    )
    row = await store.get("fbc_d")
    assert row is not None and row.status == STATUS_DONE
    pub = row.public(reviewer=True)
    assert pub["verdict"] == "reroute" and pub["reroute_target"]["target_ref"] == "sk" and "prefill" not in pub
    assert pub["reroute_target"]["reason"] == "r"
    assert row.public()["reroute_target"]["reason"] is None  # Proposer view omits coaching prose.


async def test_sweep_expired_backfills_receipts_even_for_lazily_expired_rows(db_path: str) -> None:
    cands = FeedbackCandidateStore(db_path, fingerprint_key="k")
    kw = {"team_id": "t1", "target_kind": "semantic", "target_ref": "gmv", "proposed": {"text": "x"}, "rubric_text": "r", "contributor_id": "u_a"}
    lazy, _ = await cands.propose(sig_text="lazy one", ttl_days=-1, **kw)
    await cands.list_open("t1")  # Lazy transition, not a sweep transition
    assert (await cands.get(lazy.id)).state == "expired"
    other, _ = await cands.propose(sig_text="sweep one", ttl_days=-1, **kw)
    rows = await cands.sweep_expired()
    assert {c.id for c in rows} == {lazy.id, other.id}  # Backfill regardless of who transitioned it.
    await cands.mark_receipt(lazy.id)
    await cands.mark_receipt(other.id)
    assert await cands.sweep_expired() == []  # One-time
