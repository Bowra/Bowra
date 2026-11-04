"""Billing handoff tests; domain schema admission is validated separately."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.terminal_protocol import EXPLICIT_STOP_CANCELLED_REASON, TerminalProtocolMalformedError
from bowhead_core.terminal_protocol_store import TerminalProtocolFaultPoint, TerminalProtocolStore
from cryptography.fernet import Fernet
from test_terminal_finalizer import _NOW
from test_terminal_local_billing import _balances, _execution_with_usage
from test_terminal_local_billing import db_path as _billing_db_fixture


@pytest.fixture
def db_path(tmp_path):
    return _billing_db_fixture.__wrapped__(tmp_path)


def _split_store(path: str, *, fault=None):
    core_path = str(Path(path).with_name("core-billing.db"))
    with sqlite3.connect(path) as source, sqlite3.connect(core_path) as target:
        source.backup(target)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    return TerminalProtocolStore(path, billing_db_path=core_path, billing_cipher=cipher, _clock=lambda: _NOW, _fault_injector=fault), core_path


@pytest.mark.asyncio
async def test_split_billing_commits_core_before_execution_confirmation(db_path, monkeypatch):
    store, core_path = _split_store(db_path)
    execution = await _execution_with_usage(store)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    import bowhead_core.terminal_protocol_store as module

    original = module._deduct_credit_with_stable_id_in_tx

    async def charge(connection, **kwargs):
        # The execution DB must have no writer while core is being charged.
        with sqlite3.connect(db_path, timeout=0) as probe:
            probe.execute("CREATE TABLE IF NOT EXISTS billing_writer_probe(value INTEGER)")
            probe.execute("INSERT INTO billing_writer_probe VALUES (1)")
        return await original(connection, **kwargs)

    monkeypatch.setattr(module, "_deduct_credit_with_stable_id_in_tx", charge)
    settled = await store.record_local_billing_settlement(execution, attempt)
    assert settled.state.value == "settled"
    assert _balances(core_path)[0] < 100
    assert _balances(db_path) == (100, 100)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM credit_ledger").fetchone() == (0,)
        witness = connection.execute("SELECT billing_witness_ciphertext FROM turn_effect_settlements").fetchone()[0]
        assert isinstance(witness, str) and "committed-credit-ledger" not in witness
    assert await store.record_local_billing_settlement(execution, attempt) == settled
    with sqlite3.connect(core_path) as connection:
        assert connection.execute("SELECT count(*) FROM credit_ledger").fetchone() == (1,)


@pytest.mark.asyncio
async def test_split_billing_recovers_after_committed_charge_before_witness(db_path):
    def fault(point):
        if point is TerminalProtocolFaultPoint.LOCAL_BILLING_AFTER_LEDGER:
            raise RuntimeError("simulated committed-charge crash")

    store, core_path = _split_store(db_path, fault=fault)
    execution = await _execution_with_usage(store)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    with pytest.raises(RuntimeError, match="committed-charge crash"):
        await store.record_local_billing_settlement(execution, attempt)
    charged = _balances(core_path)
    assert charged[0] < 100
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM turn_effect_settlements").fetchone() == (0,)
    store._fault_injector = None
    settled = await store.record_local_billing_settlement(execution, attempt)
    assert settled.state.value == "settled"
    assert _balances(core_path) == charged


@pytest.mark.asyncio
async def test_split_billing_rejects_missing_or_foreign_committed_witness(db_path):
    store, _ = _split_store(db_path)
    execution = await _execution_with_usage(store)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    await store.record_local_billing_settlement(execution, attempt)
    store._billing_cipher = ReferenceCipher(Fernet.generate_key().decode())
    with pytest.raises(TerminalProtocolMalformedError, match="witness is invalid"):
        await store.record_local_billing_settlement(execution, attempt)


@pytest.mark.asyncio
async def test_split_cancelled_billing_replays_after_core_commit_crash(db_path):
    def fault(point):
        if point is TerminalProtocolFaultPoint.LOCAL_BILLING_AFTER_LEDGER:
            raise RuntimeError("cancelled core charge committed")

    store, core_path = _split_store(db_path, fault=fault)
    execution = await _execution_with_usage(store)
    with pytest.raises(RuntimeError, match="cancelled core charge committed"):
        await store.begin_cancelled_finalization_with_local_billing(execution, finalization_owner_instance_id="gateway_1:stop")
    charged = _balances(core_path)
    assert charged[0] < 100
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state,closing_reason FROM turn_generations").fetchone() == ("closing", EXPLICIT_STOP_CANCELLED_REASON)
    store._fault_injector = None
    finalization = await store.begin_cancelled_finalization_with_local_billing(execution, finalization_owner_instance_id="gateway_1:stop")
    assert await store.begin_cancelled_finalization_with_local_billing(finalization) == finalization
    assert _balances(core_path) == charged


@pytest.mark.asyncio
async def test_split_billing_core_identity_conflict_does_not_create_witness(db_path):
    store, core_path = _split_store(db_path)
    execution = await _execution_with_usage(store)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    with sqlite3.connect(core_path) as connection:
        connection.execute(
            "INSERT INTO credit_ledger(id,user_id,team_id,workspace_id,turn_id,delta,reason,tokens_input,tokens_output,created_at,rate_card_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (attempt.idempotency_or_fence_token, "u_1", "team_1", "ws_1", "t_1", -100, "turn", 1, 0, "t", "v1"),
        )
    with pytest.raises(TerminalProtocolMalformedError, match="ledger identity changed"):
        await store.record_local_billing_settlement(execution, attempt)
    assert _balances(core_path) == (100, 100)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM turn_effect_settlements").fetchone() == (0,)


@pytest.mark.asyncio
async def test_migration_attests_existing_ledger_without_mutating_source(db_path, monkeypatch):
    from bowhead_core.terminal_protocol_store import prepare_all_billing_witnesses_for_migration

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    execution = await _execution_with_usage(store)
    attempt = await store.freeze_usage_and_claim_billing_attempt(execution)
    assert attempt is not None
    await store.record_local_billing_settlement(execution, attempt)
    before = _balances(db_path)
    witnesses = await prepare_all_billing_witnesses_for_migration(db_path)
    assert set(witnesses) == {attempt.attempt_id}
    assert witnesses[attempt.attempt_id]
    assert await prepare_all_billing_witnesses_for_migration(db_path, existing=witnesses) == witnesses
    with pytest.raises(TerminalProtocolMalformedError, match="identity set changed"):
        await prepare_all_billing_witnesses_for_migration(db_path, existing={})
    assert _balances(db_path) == before
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT billing_witness_ciphertext FROM turn_effect_settlements").fetchone() == (None,)
