from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from bowhead_core.agents._lead_context import (
    reset_current_subagent_id,
    set_current_subagent_id,
)
from bowhead_core.rate_card import RATE_CARD_VERSION
from bowhead_core.terminal_effect_contract import EFFECT_CONTRACT_VERSION
from bowhead_core.terminal_llm_invocation import TerminalLLMInvocationHook
from bowhead_core.terminal_protocol import EffectState, TerminalProtocolMalformedError
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
)
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.invocation import (
    PhysicalInvocationSpec,
    complete_physical_invocation,
)
from bowhead_router.types import TokenUsage
from test_terminal_finalizer import _NOW, _create_db
from test_terminal_runtime import _runtime


def _intent() -> dict[str, object]:
    return {
        "billing_rate_card_version": RATE_CARD_VERSION,
        "effect_contract_version": EFFECT_CONTRACT_VERSION,
        "text": "hello",
    }


def _spec(seed: str = "a") -> PhysicalInvocationSpec:
    return PhysicalInvocationSpec(
        provider="provider",
        model="model",
        adapter_contract_key="openai_compatible/openai_chat_completions",
        mode="invoke",
        request_digest=seed * 64,
    )


@pytest.mark.anyio
async def test_physical_success_persists_marker_settlement_and_usage_once(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda input_tokens, output_tokens: input_tokens * 0.01 + output_tokens * 0.02,
    )

    attempt = await hook.begin(_spec())
    assert await attempt.complete(
        usage=TokenUsage(input_tokens=11, output_tokens=7),
        response_digest="b" * 64,
    )
    assert await attempt.complete(
        usage=TokenUsage(input_tokens=11, output_tokens=7),
        response_digest="b" * 64,
    )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        effect = connection.execute(
            "SELECT provider,state FROM turn_effect_attempts",
        ).fetchone()
        generation = connection.execute(
            "SELECT input_tokens,output_tokens,cost_amount,state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        marker = connection.execute(
            "SELECT payload_json FROM turn_events WHERE event_type='turn.llm_invocation'",
        ).fetchone()
        settlement_count = connection.execute(
            "SELECT COUNT(*) FROM turn_effect_settlements",
        ).fetchone()[0]

    assert effect is not None
    assert (effect["provider"], effect["state"]) == ("llm_provider/v1", EffectState.SETTLED.value)
    assert generation is not None
    assert (generation["input_tokens"], generation["output_tokens"], float(generation["cost_amount"]), generation["state"]) == (
        11,
        7,
        0.25,
        "running",
    )
    assert settlement_count == 1
    assert marker is not None
    payload = json.loads(marker["payload_json"])
    assert set(payload) == {"invocation_digest", "mode", "protocol"}


@pytest.mark.anyio
async def test_peer_physical_marker_preserves_actor_scope(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.0,
    )
    token = set_current_subagent_id("sub_1")
    try:
        attempt = await hook.begin(_spec("0"))
    finally:
        reset_current_subagent_id(token)
    await attempt.unknown(reason_code="test_cleanup")

    with sqlite3.connect(db_path) as connection:
        actor_scope = connection.execute(
            "SELECT actor_scope FROM turn_events WHERE event_type='turn.llm_invocation'",
        ).fetchone()[0]
    assert actor_scope == "peer"


@pytest.mark.anyio
async def test_physical_unknown_poison_creates_full_recovery_fence(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.0,
    )
    attempt = await hook.begin(_spec("c"))

    await attempt.unknown(reason_code="transport_timeout")

    with sqlite3.connect(db_path) as connection:
        effect_state = connection.execute(
            "SELECT state FROM turn_effect_attempts",
        ).fetchone()[0]
        generation_state = connection.execute(
            "SELECT state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()[0]
        recovery = connection.execute(
            "SELECT kind,state FROM turn_recovery_records",
        ).fetchone()
        lock = connection.execute(
            "SELECT recovery_lock_turn_id,recovery_lock_generation FROM sessions WHERE id='s_1'",
        ).fetchone()

    assert effect_state == EffectState.UNKNOWN.value
    assert generation_state == "recovery_pending"
    assert tuple(recovery) == ("effect_unknown", "active")
    assert tuple(lock) == ("t_1", 1)


@pytest.mark.anyio
async def test_preclaimed_sibling_can_settle_after_unknown_but_cannot_continue(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.5,
    )
    first = await hook.begin(_spec("d"))
    sibling = await hook.begin(_spec("e"))

    await first.unknown(reason_code="transport_timeout")
    assert not await sibling.complete(
        usage=TokenUsage(input_tokens=5, output_tokens=3),
        response_digest="f" * 64,
    )
    with pytest.raises(InvocationOutcomeUnknownError):
        await hook.begin(_spec("1"))

    with sqlite3.connect(db_path) as connection:
        states = connection.execute(
            "SELECT state FROM turn_effect_attempts ORDER BY started_at,attempt_id",
        ).fetchall()
        generation = connection.execute(
            "SELECT input_tokens,output_tokens,state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        marker_count = connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE event_type='turn.llm_invocation'",
        ).fetchone()[0]
    assert sorted(state[0] for state in states) == ["settled", "unknown"]
    assert generation == (5, 3, "recovery_pending")
    assert marker_count == 2


@pytest.mark.anyio
async def test_stop_first_late_completion_settles_disclosure_without_usage(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.5,
    )
    attempt = await hook.begin(_spec("2"))
    await runtime.bus.commit_cancelled()

    assert not await attempt.complete(
        usage=TokenUsage(input_tokens=9, output_tokens=4),
        response_digest="3" * 64,
    )

    with sqlite3.connect(db_path) as connection:
        effect_state = connection.execute(
            "SELECT state FROM turn_effect_attempts WHERE provider='llm_provider/v1'",
        ).fetchone()[0]
        generation = connection.execute(
            "SELECT input_tokens,output_tokens,state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
    assert effect_state == "settled"
    assert generation == (0, 0, "closed")


@pytest.mark.anyio
async def test_stop_winning_between_completion_read_and_commit_retries_exact_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    original = runtime.store.record_llm_effect_settlement_with_usage
    stop_committed = False

    async def stop_before_first_commit(*args: object, **kwargs: object):
        nonlocal stop_committed
        if not stop_committed:
            stop_committed = True
            await runtime.bus.commit_cancelled()
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        runtime.store,
        "record_llm_effect_settlement_with_usage",
        stop_before_first_commit,
    )
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.5,
    )
    attempt = await hook.begin(_spec("9"))

    assert not await attempt.complete(
        usage=TokenUsage(input_tokens=9, output_tokens=4),
        response_digest="a" * 64,
    )

    with sqlite3.connect(db_path) as connection:
        effect_state = connection.execute(
            "SELECT state FROM turn_effect_attempts WHERE provider='llm_provider/v1'",
        ).fetchone()[0]
        generation = connection.execute(
            "SELECT input_tokens,output_tokens,state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
    assert effect_state == "settled"
    assert generation == (0, 0, "closed")


@pytest.mark.anyio
async def test_cancelled_receipt_probe_uses_shared_store_not_local_bus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.5,
    )
    attempt = await hook.begin(_spec("b"))
    await runtime.bus.commit_cancelled()
    runtime.bus._receipt = None
    runtime.bus._receipt_ready.clear()
    original = runtime.store.record_llm_effect_settlement_with_usage
    calls = 0

    async def report_receipt_race_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TerminalProtocolMalformedError(
                "cancelled effect settlement waits for terminal receipt",
            )
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    async def local_receipt_must_not_be_required():
        raise AssertionError("shared receipt was already persisted")

    monkeypatch.setattr(
        runtime.store,
        "record_llm_effect_settlement_with_usage",
        report_receipt_race_once,
    )
    monkeypatch.setattr(
        runtime.bus,
        "wait_for_receipt",
        local_receipt_must_not_be_required,
    )

    assert not await attempt.complete(
        usage=TokenUsage(input_tokens=2, output_tokens=1),
        response_digest="c" * 64,
    )
    assert calls >= 2


@pytest.mark.anyio
async def test_cancelled_receipt_wait_is_bounded_and_poisoned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.0,
    )
    attempt = await hook.begin(_spec("d"))

    async def always_wait(*args: object, **kwargs: object):
        del args, kwargs
        raise TerminalProtocolMalformedError(
            "cancelled effect settlement waits for terminal receipt",
        )

    monkeypatch.setattr(
        runtime.store,
        "record_llm_effect_settlement_with_usage",
        always_wait,
    )
    monkeypatch.setattr(
        "bowhead_core.terminal_llm_invocation._RECEIPT_WAIT_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(InvocationOutcomeUnknownError) as timed_out:
        await attempt.complete(
            usage=TokenUsage(input_tokens=2, output_tokens=1),
            response_digest="e" * 64,
        )
    assert timed_out.value.reason_code == "terminal_receipt_not_observed"
    with pytest.raises(InvocationOutcomeUnknownError) as poisoned:
        await hook.begin(_spec("f"))
    assert poisoned.value.reason_code == "generation_invocation_scope_poisoned"


@pytest.mark.anyio
async def test_usage_transaction_fault_rolls_back_and_poison_recovers(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.LLM_COMPLETION_AFTER_USAGE:
            raise RuntimeError("usage transaction fault")

    runtime.store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.5,
    )
    attempt = await hook.begin(_spec("4"))

    with pytest.raises(InvocationOutcomeUnknownError):
        await complete_physical_invocation(
            attempt,
            usage=TokenUsage(input_tokens=8, output_tokens=2),
            response={"id": "safe-id"},
        )

    with sqlite3.connect(db_path) as connection:
        effect = connection.execute(
            "SELECT state FROM turn_effect_attempts WHERE provider='llm_provider/v1'",
        ).fetchone()[0]
        settlement_count = connection.execute(
            "SELECT COUNT(*) FROM turn_effect_settlements",
        ).fetchone()[0]
        generation = connection.execute(
            "SELECT input_tokens,output_tokens,state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
    assert effect == "unknown"
    assert settlement_count == 0
    assert generation == (0, 0, "recovery_pending")


@pytest.mark.anyio
async def test_settlement_commit_ack_loss_replays_without_double_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    original = runtime.store.record_llm_effect_settlement_with_usage
    calls = 0

    async def lose_first_ack(*args: object, **kwargs: object):
        nonlocal calls
        result = await original(*args, **kwargs)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            raise RuntimeError("settlement commit acknowledgement lost")
        return result

    monkeypatch.setattr(
        runtime.store,
        "record_llm_effect_settlement_with_usage",
        lose_first_ack,
    )
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.5,
    )
    attempt = await hook.begin(_spec("7"))

    assert await attempt.complete(
        usage=TokenUsage(input_tokens=6, output_tokens=3),
        response_digest="8" * 64,
    )

    with sqlite3.connect(db_path) as connection:
        settlement_count = connection.execute(
            "SELECT COUNT(*) FROM turn_effect_settlements",
        ).fetchone()[0]
        generation = connection.execute(
            "SELECT input_tokens,output_tokens,cost_amount,state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
    assert calls == 2
    assert settlement_count == 1
    assert generation == (6, 3, 0.5, "running")


@pytest.mark.anyio
async def test_cancel_after_settlement_commit_hands_generation_to_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    original = runtime.store.record_llm_effect_settlement_with_usage
    committed = asyncio.Event()
    hold_ack = asyncio.Event()

    async def commit_then_hold_ack(*args: object, **kwargs: object):
        result = await original(*args, **kwargs)  # type: ignore[arg-type]
        committed.set()
        await hold_ack.wait()
        return result

    monkeypatch.setattr(
        runtime.store,
        "record_llm_effect_settlement_with_usage",
        commit_then_hold_ack,
    )
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.5,
    )
    attempt = await hook.begin(_spec("6"))
    completion = asyncio.create_task(
        complete_physical_invocation(
            attempt,
            usage=TokenUsage(input_tokens=6, output_tokens=3),
            response={"id": "safe-id"},
        )
    )
    await committed.wait()
    completion.cancel()
    with pytest.raises(asyncio.CancelledError):
        await completion

    with sqlite3.connect(db_path) as connection:
        effect_state = connection.execute(
            "SELECT state FROM turn_effect_attempts WHERE provider='llm_provider/v1'",
        ).fetchone()[0]
        generation = connection.execute(
            "SELECT input_tokens,output_tokens,state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
        recovery_count = connection.execute(
            "SELECT COUNT(*) FROM turn_recovery_records WHERE turn_id='t_1' AND state='active'",
        ).fetchone()[0]
    assert effect_state == "settled"
    assert generation == (6, 3, "recovery_pending")
    assert recovery_count == 1


@pytest.mark.anyio
async def test_settlement_failure_poisons_before_queued_sibling_can_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.0,
    )
    first = await hook.begin(_spec("1"))
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()

    async def fail_settlement(*args: object, **kwargs: object):
        del args, kwargs
        settlement_entered.set()
        await settlement_release.wait()
        raise RuntimeError("settlement failed")

    monkeypatch.setattr(
        runtime.store,
        "record_llm_effect_settlement_with_usage",
        fail_settlement,
    )
    settling = asyncio.create_task(
        first.complete(
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            response_digest="2" * 64,
        )
    )
    await settlement_entered.wait()
    sibling = asyncio.create_task(hook.begin(_spec("3")))
    await asyncio.sleep(0)
    settlement_release.set()

    with pytest.raises(RuntimeError, match="settlement failed"):
        await settling
    with pytest.raises(InvocationOutcomeUnknownError) as rejected:
        await sibling
    assert rejected.value.reason_code == "generation_invocation_scope_poisoned"

    with sqlite3.connect(db_path) as connection:
        marker_count = connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE event_type='turn.llm_invocation'",
        ).fetchone()[0]
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM turn_effect_attempts WHERE provider='llm_provider/v1'",
        ).fetchone()[0]
    assert marker_count == 1
    assert attempt_count == 1


@pytest.mark.anyio
async def test_dispatch_claim_ack_loss_never_retries_or_leaves_running(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.EFFECT_AFTER_DISPATCH:
            raise RuntimeError("dispatch claim ack lost")

    runtime.store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=lambda _input, _output: 0.0,
    )

    with pytest.raises(InvocationOutcomeUnknownError):
        await hook.begin(_spec("5"))
    with pytest.raises(InvocationOutcomeUnknownError):
        await hook.begin(_spec("6"))

    with sqlite3.connect(db_path) as connection:
        effect = connection.execute(
            "SELECT state FROM turn_effect_attempts WHERE provider='llm_provider/v1'",
        ).fetchone()[0]
        generation = connection.execute(
            "SELECT state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()[0]
        marker_count = connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE event_type='turn.llm_invocation'",
        ).fetchone()[0]
    assert effect == "unknown"
    assert generation == "recovery_pending"
    assert marker_count == 1


@pytest.fixture(scope="module")
def provider_failure_database(tmp_path_factory: pytest.TempPathFactory) -> str:
    return _create_db(tmp_path_factory.mktemp("provider-failure-baseline"))


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("peer", [False, True])
@pytest.mark.parametrize("api_surface", ["responses", "chat_completions", "messages"])
async def test_provider_http_failure_survives_unknown_fence_and_replays(
    tmp_path: Path,
    mode: str,
    peer: bool,
    api_surface: str,
    provider_failure_database: str,
) -> None:
    import respx
    from bowhead_core.terminal_llm_invocation import durable_llm_invocation_scope
    from bowhead_core.terminal_protocol_store import TerminalReadContext
    from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter

    from bowhead_router import LLMRequest, Message, OpenAICompatibleAdapter

    db_path = str(tmp_path / "provider-error.sqlite3")
    with sqlite3.connect(provider_failure_database) as source, sqlite3.connect(db_path) as target:
        source.backup(target)
    runtime = await _runtime(db_path, execution_intent=_intent())
    model = "kimi_k3"
    base_url = "https://provider.example/v1"
    adapter = (
        AnthropicCompatibleAdapter(base_url=base_url.removesuffix("/v1"), api_key="sk-test", provider_name="provider")
        if api_surface == "messages"
        else OpenAICompatibleAdapter(
            base_url=base_url,
            api_key="sk-test",
            provider_name="provider",
            provider_options={"models": [{"id": model, "api_surface": api_surface}]},
        )
    )
    request = LLMRequest(model=model, messages=[Message(role="user", content="hello")])
    token = set_current_subagent_id("sub_1" if peer else None)
    path = "chat/completions" if api_surface == "chat_completions" else api_surface
    try:
        with respx.mock as mock:
            route = mock.post(f"{base_url}/{path}").respond(
                400,
                json={
                    "error": {
                        "code": "unsupported_value",
                        "param": "temperature",
                        "message": "Unsupported value for temperature",
                    }
                },
            )
            async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
                with pytest.raises(InvocationOutcomeUnknownError):
                    if mode == "stream":
                        _ = [chunk async for chunk in adapter.stream(request)]
                    else:
                        await adapter.invoke(request)
            assert route.call_count == 1
    finally:
        reset_current_subagent_id(token)
        await adapter.close()

    live = [event.payload for event in runtime.bus.snapshot() if event.event_type == "turn_failed"]
    assert len(live) == 1
    failure = live[0]
    assert failure["reason_code"] == "llm_request_rejected"
    assert failure["recovery_action"] == "copy_diagnostics"
    diagnostic = failure["diagnostics"]
    assert diagnostic["provider_message"] == "Unsupported value for temperature"
    assert diagnostic["retryable"] is False
    assert diagnostic["turn_id"] == "t_1"
    for fact in ("http_status=400", f"api_surface={api_surface}", "upstream_param=temperature", "outcome=unknown"):
        assert fact in diagnostic["safe_detail"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_effect_attempts").fetchone()[0] == "unknown"
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_settlements").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM turn_generations").fetchone()[0] == "recovery_pending"
    restarted_store = TerminalProtocolStore(db_path, _clock=lambda: _NOW)
    replay = await restarted_store.list_protocol_events(
        TerminalReadContext(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            viewer="gateway_1",
            auth_fence_vector_digest=runtime.identity.authz_snapshot_digest,
        ),
        after_event_seq=0,
    )
    # Compatibility frames enrich the durable business payload with transport cursors.
    transport_fields = {"seq", "evt_seq", "event_id", "event_seq", "generation"}
    assert [row.payload for row in replay if row.event_type == "turn_failed"] == [
        {key: value for key, value in payload.items() if key not in transport_fields} for payload in live
    ]
