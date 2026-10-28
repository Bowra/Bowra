from __future__ import annotations

import ast
import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import bowhead_core.terminal_llm_invocation as terminal_llm_invocation
import pytest
from bowhead_core.agents.report_tools import _write_report
from bowhead_core.canvas_composer import compose_canvas_from_payload
from bowhead_core.events import OrchestratorEvent, TerminalDurabilityAbort
from bowhead_core.rate_card import RATE_CARD_VERSION
from bowhead_core.terminal_effect_contract import (
    EFFECT_CONTRACT_VERSION,
    TOOL_EFFECT_NONE,
    TOOL_EFFECT_READ_ONLY,
    ClassifiedToolEffectResult,
    EffectProbeEvidence,
    ToolEffectIdempotency,
    ToolEffectSpec,
)
from bowhead_core.terminal_llm_invocation import (
    create_detached_auxiliary_task,
    durable_llm_invocation_scope,
    emit_tool_step_started_ticket,
    reject_unclassified_direct_mutation,
    tool_invocation_ticket_scope,
)
from bowhead_core.terminal_protocol import (
    EffectClass,
    EffectSettlementOutcome,
    EffectState,
    canonical_digest_v1,
)
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
)
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSchema,
    ToolSpec,
)
from bowhead_core.types import ToolAnnotations
from bowhead_router.errors import InvocationOutcomeUnknownError
from bowhead_router.invocation import physical_invocation_required
from bowhead_router.usage import current_usage_context, record_usage, usage_context
from test_terminal_finalizer import _NOW, _create_db
from test_terminal_runtime import _runtime


def _intent() -> dict[str, object]:
    return {
        "billing_rate_card_version": RATE_CARD_VERSION,
        "effect_contract_version": EFFECT_CONTRACT_VERSION,
        "text": "tool",
    }


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="member_1",
        user_id="u_1",
        turn_id="t_1",
        session_id="s_1",
        datarc=None,  # type: ignore[arg-type]
    )


def _side_effect_spec(
    idempotency: ToolEffectIdempotency = ToolEffectIdempotency.NON_RETRIABLE,
) -> ToolEffectSpec:
    return ToolEffectSpec(
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        idempotency=idempotency,
        effect_contract_key="tool_external/v1",
    )


def _classified(
    seed: str = "success",
    *,
    outcome: EffectSettlementOutcome = EffectSettlementOutcome.CONFIRMED_APPLIED,
) -> ClassifiedToolEffectResult:
    return ClassifiedToolEffectResult(
        result={"ok": True, "provider_id": seed},
        evidence=EffectProbeEvidence(
            outcome=outcome,
            settlement_code=f"tool_external_result/v1:{outcome.value}",
            evidence_digest=canonical_digest_v1({"evidence": seed}),
            object_version_digest=canonical_digest_v1({"version": seed}),
            audit_object_ref=f"provider-object:{seed}",
            provider_external_ref_digest=canonical_digest_v1({"provider_ref": seed}),
            provider_external_ref_key_version="provider-ref/v1",
        ),
    )


def _registry(
    handler: Any,
    *,
    effect_spec: ToolEffectSpec | None,
    needs_approval: bool = False,
    annotations: ToolAnnotations | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="test_tool",
                description="test",
                input_schema={"type": "object"},
            ),
            handler=handler,
            effect_spec=effect_spec,
            needs_approval=needs_approval,
            annotations=annotations,
        )
    )
    return registry


async def _execute_with_ticket(
    runtime: Any,
    registry: ToolRegistry,
    *,
    call_id: str,
    propagate_ambiguous_transport: bool = False,
) -> Any:
    ticket = await emit_tool_step_started_ticket(
        runtime.bus,
        OrchestratorEvent(
            event_type="step_started",
            turn_id="t_1",
            payload={"call_id": call_id, "step_id": f"step_{call_id}"},
        ),
    )
    assert ticket is not None
    with tool_invocation_ticket_scope(ticket):
        return await registry.execute(
            "test_tool",
            {"value": call_id},
            _ctx(),
            propagate_ambiguous_transport=propagate_ambiguous_transport,
        )


def test_tool_effect_spec_rejects_unsafe_combinations() -> None:
    with pytest.raises(ValueError, match="idempotency disposition"):
        ToolEffectSpec(
            EffectClass.SIDE_EFFECT_POSSIBLE,
            ToolEffectIdempotency.NONE,
            "tool_external/v1",
        )
    with pytest.raises(ValueError, match="provider contract"):
        ToolEffectSpec(
            EffectClass.READ_ONLY,
            ToolEffectIdempotency.NONE,
            "tool_external/v1",
        )


@pytest.mark.anyio
async def test_absent_durable_scope_preserves_legacy_tool_behavior() -> None:
    calls = 0

    async def handler(arguments: dict[str, object], ctx: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert ctx.effect_idempotency_key is None
        return {"legacy": arguments["value"]}

    invocation = await _registry(handler, effect_spec=None).execute(
        "test_tool",
        {"value": "same"},
        _ctx(),
    )
    assert calls == 1
    assert invocation.result == {"legacy": "same"}


@pytest.mark.anyio
async def test_read_only_tool_uses_persisted_marker_without_effect_attempt(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    async def handler(arguments: dict[str, object], ctx: Any) -> dict[str, object]:
        return {"read": arguments["value"]}

    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        invocation = await _execute_with_ticket(
            runtime,
            _registry(handler, effect_spec=TOOL_EFFECT_READ_ONLY),
            call_id="read",
        )

    assert invocation.result == {"read": "read"}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_effect_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='step_started'").fetchone()[0] == 1


@pytest.mark.anyio
async def test_stable_token_tool_settles_once_with_classifier_evidence(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    provider_tokens: list[str | None] = []

    async def handler(arguments: dict[str, object], ctx: Any) -> ClassifiedToolEffectResult:
        provider_tokens.append(ctx.effect_idempotency_key)
        return _classified(str(arguments["value"]))

    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        invocation = await _execute_with_ticket(
            runtime,
            _registry(
                handler,
                effect_spec=_side_effect_spec(ToolEffectIdempotency.STABLE_TOKEN),
            ),
            call_id="apply",
        )

    assert invocation.result == {"ok": True, "provider_id": "apply"}
    assert len(provider_tokens) == 1 and provider_tokens[0]
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        effect = connection.execute(
            "SELECT provider,state,non_retriable FROM turn_effect_attempts",
        ).fetchone()
        settlement = connection.execute(
            "SELECT source,outcome,actor_audit_ref FROM turn_effect_settlements",
        ).fetchone()
        usage = connection.execute(
            "SELECT input_tokens,output_tokens FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
    assert tuple(effect) == ("tool_external/v1", EffectState.SETTLED.value, 0)
    assert tuple(settlement) == (
        "provider",
        "confirmed_applied",
        "adapter:tool_external_result/v1",
    )
    assert tuple(usage) == (0, 0)


@pytest.mark.anyio
async def test_tool_settlement_ack_loss_replays_without_duplicate_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    original = runtime.store.record_tool_effect_settlement
    commits = 0

    async def lose_first_ack(execution: Any, context: Any) -> Any:
        nonlocal commits
        result = await original(execution, context)
        commits += 1
        if commits == 1:
            raise RuntimeError("settlement ack lost")
        return result

    monkeypatch.setattr(
        runtime.store,
        "record_tool_effect_settlement",
        lose_first_ack,
    )

    async def handler(
        _arguments: dict[str, object],
        _ctx: Any,
    ) -> ClassifiedToolEffectResult:
        return _classified("ack-loss")

    async with durable_llm_invocation_scope(
        runtime,
        cost_estimator=lambda _i, _o: 0.0,
    ):
        invocation = await _execute_with_ticket(
            runtime,
            _registry(handler, effect_spec=_side_effect_spec()),
            call_id="ack-loss",
        )

    assert invocation.result == {"ok": True, "provider_id": "ack-loss"}
    assert commits == 2
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM turn_effect_settlements",
            ).fetchone()[0]
            == 1
        )


@pytest.mark.anyio
async def test_tool_completion_accepts_contract_declared_compensated_outcome(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    async def handler(
        _arguments: dict[str, object],
        _ctx: Any,
    ) -> ClassifiedToolEffectResult:
        return _classified(
            "compensated",
            outcome=EffectSettlementOutcome.COMPENSATED,
        )

    async with durable_llm_invocation_scope(
        runtime,
        cost_estimator=lambda _i, _o: 0.0,
    ):
        invocation = await _execute_with_ticket(
            runtime,
            _registry(handler, effect_spec=_side_effect_spec()),
            call_id="compensated",
        )

    assert invocation.result == {"ok": True, "provider_id": "compensated"}
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT outcome FROM turn_effect_settlements",
            ).fetchone()[0]
            == EffectSettlementOutcome.COMPENSATED.value
        )


@pytest.mark.anyio
async def test_trusted_classifier_is_not_reinterpreted_by_legacy_error_heuristic(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    async def handler(
        _arguments: dict[str, object],
        _ctx: Any,
    ) -> ClassifiedToolEffectResult:
        classified = _classified("provider-timeout")
        return ClassifiedToolEffectResult(
            result={"ok": False, "error_kind": "timeout"},
            evidence=classified.evidence,
        )

    async with durable_llm_invocation_scope(
        runtime,
        cost_estimator=lambda _i, _o: 0.0,
    ):
        invocation = await _execute_with_ticket(
            runtime,
            _registry(handler, effect_spec=_side_effect_spec()),
            call_id="classified-error-shape",
            propagate_ambiguous_transport=True,
        )

    assert invocation.result == {"ok": False, "error_kind": "timeout"}
    assert invocation.error is None
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT state FROM turn_effect_attempts",
            ).fetchone()[0]
            == EffectState.SETTLED.value
        )
        assert (
            connection.execute(
                "SELECT state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
            ).fetchone()[0]
            == "running"
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "missing",
    ["contract", "ticket", "approval", "foreign_contract"],
)
async def test_untrusted_or_unowned_dispatch_never_enters_handler(
    tmp_path: Path,
    missing: str,
) -> None:
    runtime = await _runtime(_create_db(tmp_path), execution_intent=_intent())
    calls = 0

    async def handler(_arguments: dict[str, object], _ctx: Any) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    if missing == "contract":
        spec = None
    elif missing == "foreign_contract":
        spec = ToolEffectSpec(
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            idempotency=ToolEffectIdempotency.NON_RETRIABLE,
            effect_contract_key="approval_decision/v1",
        )
    else:
        spec = _side_effect_spec()
    registry = _registry(
        handler,
        effect_spec=spec,
        needs_approval=missing == "approval",
        annotations=ToolAnnotations(read_only=True, idempotent=True),
    )
    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        if missing == "ticket":
            with pytest.raises(TerminalDurabilityAbort):
                await registry.execute("test_tool", {}, _ctx())
        else:
            with pytest.raises(TerminalDurabilityAbort):
                await _execute_with_ticket(runtime, registry, call_id=missing)
    assert calls == 0


@pytest.mark.anyio
async def test_unclassified_success_becomes_unknown_and_poisons_next_dispatch(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    calls: list[str] = []

    async def unclassified(arguments: dict[str, object], _ctx: Any) -> dict[str, bool]:
        calls.append(str(arguments["value"]))
        return {"ok": True}

    registry = _registry(unclassified, effect_spec=_side_effect_spec())
    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        with pytest.raises(InvocationOutcomeUnknownError):
            await _execute_with_ticket(runtime, registry, call_id="first")
        with pytest.raises((InvocationOutcomeUnknownError, TerminalDurabilityAbort)):
            await _execute_with_ticket(runtime, registry, call_id="second")

    assert calls == ["first"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_effect_attempts").fetchone()[0] == "unknown"
        assert connection.execute("SELECT state FROM turn_generations WHERE turn_id='t_1' AND generation=1").fetchone()[0] == "recovery_pending"


@pytest.mark.anyio
async def test_mismatched_classifier_evidence_becomes_unknown(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    async def handler(
        _arguments: dict[str, object],
        _ctx: Any,
    ) -> ClassifiedToolEffectResult:
        classified = _classified("wrong-classifier")
        return ClassifiedToolEffectResult(
            result=classified.result,
            evidence=EffectProbeEvidence(
                outcome=classified.evidence.outcome,
                settlement_code="untrusted_result/v1:confirmed_applied",
                evidence_digest=classified.evidence.evidence_digest,
                object_version_digest=classified.evidence.object_version_digest,
                audit_object_ref=classified.evidence.audit_object_ref,
                provider_external_ref_digest=(classified.evidence.provider_external_ref_digest),
                provider_external_ref_key_version=(classified.evidence.provider_external_ref_key_version),
            ),
        )

    async with durable_llm_invocation_scope(
        runtime,
        cost_estimator=lambda _i, _o: 0.0,
    ):
        with pytest.raises(InvocationOutcomeUnknownError):
            await _execute_with_ticket(
                runtime,
                _registry(handler, effect_spec=_side_effect_spec()),
                call_id="classifier",
            )

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT state FROM turn_effect_attempts",
            ).fetchone()[0]
            == "unknown"
        )


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [RuntimeError("boom"), TimeoutError("timeout")])
async def test_handler_failure_is_unknown_not_a_normal_tool_error(
    tmp_path: Path,
    failure: Exception,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    async def handler(_arguments: dict[str, object], _ctx: Any) -> None:
        raise failure

    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        with pytest.raises(InvocationOutcomeUnknownError):
            await _execute_with_ticket(
                runtime,
                _registry(handler, effect_spec=_side_effect_spec()),
                call_id="failure",
            )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_effect_attempts").fetchone()[0] == "unknown"


@pytest.mark.anyio
async def test_handler_reported_unknown_marks_attempt_and_poisons_next_dispatch(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    calls = 0

    async def handler(_arguments: dict[str, object], _ctx: Any) -> None:
        nonlocal calls
        calls += 1
        raise InvocationOutcomeUnknownError("nested_effect_unknown")

    registry = _registry(handler, effect_spec=_side_effect_spec())
    async with durable_llm_invocation_scope(
        runtime,
        cost_estimator=lambda _i, _o: 0.0,
    ):
        with pytest.raises(InvocationOutcomeUnknownError):
            await _execute_with_ticket(runtime, registry, call_id="first")
        with pytest.raises(
            TerminalDurabilityAbort,
            match="terminal durable tool marker write failed",
        ):
            await _execute_with_ticket(runtime, registry, call_id="second")

    assert calls == 1
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT state FROM turn_effect_attempts",
            ).fetchone()[0]
            == "unknown"
        )
        assert (
            connection.execute(
                "SELECT state FROM turn_generations WHERE turn_id='t_1' AND generation=1",
            ).fetchone()[0]
            == "recovery_pending"
        )


@pytest.mark.anyio
async def test_cancelled_handler_marks_unknown_before_cancellation_escapes(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    async def handler(_arguments: dict[str, object], _ctx: Any) -> None:
        raise asyncio.CancelledError

    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        with pytest.raises(asyncio.CancelledError):
            await _execute_with_ticket(
                runtime,
                _registry(handler, effect_spec=_side_effect_spec()),
                call_id="cancel",
            )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_effect_attempts").fetchone()[0] == "unknown"


@pytest.mark.anyio
async def test_dispatch_claim_ack_loss_never_calls_tool_handler(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())

    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is TerminalProtocolFaultPoint.EFFECT_AFTER_DISPATCH:
            raise RuntimeError("dispatch ack lost")

    runtime.store = TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject,
    )
    calls = 0

    async def handler(_arguments: dict[str, object], _ctx: Any) -> ClassifiedToolEffectResult:
        nonlocal calls
        calls += 1
        return _classified()

    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        with pytest.raises(InvocationOutcomeUnknownError):
            await _execute_with_ticket(
                runtime,
                _registry(handler, effect_spec=_side_effect_spec()),
                call_id="claim",
            )
    assert calls == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_effect_attempts").fetchone()[0] == "unknown"


@pytest.mark.anyio
async def test_stop_after_ticket_before_claim_aborts_without_calling_handler(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    calls = 0

    async def handler(
        _arguments: dict[str, object],
        _ctx: Any,
    ) -> ClassifiedToolEffectResult:
        nonlocal calls
        calls += 1
        return _classified()

    registry = _registry(handler, effect_spec=_side_effect_spec())
    async with durable_llm_invocation_scope(
        runtime,
        cost_estimator=lambda _i, _o: 0.0,
    ):
        ticket = await emit_tool_step_started_ticket(
            runtime.bus,
            OrchestratorEvent(
                event_type="step_started",
                turn_id="t_1",
                payload={"call_id": "stop", "step_id": "step_stop"},
            ),
        )
        assert ticket is not None
        await runtime.bus.commit_cancelled()
        with (
            tool_invocation_ticket_scope(ticket),
            pytest.raises(
                TerminalDurabilityAbort,
                match="durable effect claim rejected before provider dispatch",
            ),
        ):
            await registry.execute("test_tool", {"value": "stop"}, _ctx())

    assert calls == 0
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM turn_effect_attempts",
            ).fetchone()[0]
            == 0
        )


@pytest.mark.anyio
async def test_stop_after_claim_allows_only_late_settlement(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_arguments: dict[str, object], _ctx: Any) -> ClassifiedToolEffectResult:
        entered.set()
        await release.wait()
        return _classified("late")

    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        running = asyncio.create_task(
            _execute_with_ticket(
                runtime,
                _registry(handler, effect_spec=_side_effect_spec()),
                call_id="late",
            )
        )
        await entered.wait()
        await runtime.bus.commit_cancelled()
        release.set()
        with pytest.raises(InvocationOutcomeUnknownError) as fenced:
            await running
        assert fenced.value.reason_code == "durable_invocation_fenced"

    with sqlite3.connect(db_path) as connection:
        effect = connection.execute("SELECT state FROM turn_effect_attempts").fetchone()[0]
        generation = connection.execute(
            "SELECT state,input_tokens,output_tokens FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone()
    assert effect == "settled"
    assert generation == ("closed", 0, 0)


@pytest.mark.anyio
async def test_preclaimed_parallel_tool_settles_but_cannot_continue_after_peer_unknown(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    entered = {"a": asyncio.Event(), "b": asyncio.Event()}
    release = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def handler(arguments: dict[str, object], _ctx: Any) -> object:
        key = str(arguments["value"])
        entered[key].set()
        await release[key].wait()
        return {"ok": True} if key == "a" else _classified("b")

    registry = _registry(handler, effect_spec=_side_effect_spec())
    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        first = asyncio.create_task(_execute_with_ticket(runtime, registry, call_id="a"))
        await entered["a"].wait()
        second = asyncio.create_task(_execute_with_ticket(runtime, registry, call_id="b"))
        await entered["b"].wait()
        release["a"].set()
        with pytest.raises(InvocationOutcomeUnknownError):
            await first
        release["b"].set()
        with pytest.raises(InvocationOutcomeUnknownError) as fenced:
            await second
        assert fenced.value.reason_code == "durable_invocation_fenced"

    with sqlite3.connect(db_path) as connection:
        states = sorted(row[0] for row in connection.execute("SELECT state FROM turn_effect_attempts"))
        ticket_ids = [row[0] for row in connection.execute("SELECT id FROM turn_events WHERE event_type='step_started'")]
    assert states == ["settled", "unknown"]
    assert len(ticket_ids) == len(set(ticket_ids)) == 2


@pytest.mark.anyio
async def test_unknown_transaction_failure_still_poisons_queued_tool_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(_create_db(tmp_path), execution_intent=_intent())
    unknown_entered = asyncio.Event()
    release_unknown = asyncio.Event()
    handler_calls: list[str] = []

    async def fail_unknown(*_args: object, **_kwargs: object) -> None:
        unknown_entered.set()
        await release_unknown.wait()
        raise RuntimeError("unknown transaction unavailable")

    monkeypatch.setattr(
        runtime.store,
        "mark_effect_unknown_and_discover_recovery",
        fail_unknown,
    )

    async def handler(arguments: dict[str, object], _ctx: Any) -> dict[str, bool]:
        handler_calls.append(str(arguments["value"]))
        return {"ok": True}

    registry = _registry(handler, effect_spec=_side_effect_spec())
    async with durable_llm_invocation_scope(
        runtime,
        cost_estimator=lambda _i, _o: 0.0,
    ):
        first = asyncio.create_task(
            _execute_with_ticket(runtime, registry, call_id="first"),
        )
        await unknown_entered.wait()
        second = asyncio.create_task(
            _execute_with_ticket(runtime, registry, call_id="second"),
        )
        release_unknown.set()
        with pytest.raises(InvocationOutcomeUnknownError):
            await first
        with pytest.raises(InvocationOutcomeUnknownError):
            await second

    assert handler_calls == ["first"]


@pytest.mark.anyio
async def test_direct_composite_mutations_remain_ineligible_before_storage(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(_create_db(tmp_path), execution_intent=_intent())

    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        with pytest.raises(TerminalDurabilityAbort):
            await _write_report({"content": "text"}, SimpleNamespace())
        with pytest.raises(TerminalDurabilityAbort):
            await compose_canvas_from_payload(
                canvas_store=SimpleNamespace(),
                request=SimpleNamespace(),
                bus=SimpleNamespace(),
                payload={},
            )


@pytest.mark.anyio
async def test_detached_auxiliary_task_clears_root_authority_before_usage_freeze(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,credit_balance,created_at)
            VALUES ('u_1','team_1','u1@example.test',100,'t')
            """,
        )
        connection.execute(
            "UPDATE teams SET team_credit_balance=100 WHERE id='team_1'",
        )
        connection.commit()
    runtime = await _runtime(db_path, execution_intent=_intent())
    partial_usage: list[object] = []
    observed: dict[str, object] = {}

    async def _auxiliary() -> None:
        observed["physical_required"] = physical_invocation_required()
        observed["reason"] = current_usage_context().reason
        observed["tool_ticket"] = terminal_llm_invocation._CURRENT_TOOL_TICKET.get()
        reject_unclassified_direct_mutation("test_auxiliary")
        record_usage(
            agent_type="test",
            provider="test",
            model="test",
            tokens_input=7,
            tokens_output=3,
        )

    with usage_context(
        team_id="team_1",
        turn_id="t_1",
        reason="turn",
        turn_usage_sink=partial_usage.append,
    ):
        async with durable_llm_invocation_scope(
            runtime,
            cost_estimator=lambda _i, _o: 0.0,
        ):
            ticket = await emit_tool_step_started_ticket(
                runtime.bus,
                OrchestratorEvent(
                    event_type="step_started",
                    turn_id="t_1",
                    payload={"call_id": "stale", "step_id": "step_stale"},
                ),
            )
            assert ticket is not None
            record_usage(
                agent_type="root",
                provider="test",
                model="test",
                tokens_input=1_200,
                tokens_output=300,
            )
            with tool_invocation_ticket_scope(ticket):
                task = create_detached_auxiliary_task(
                    _auxiliary(),
                    reason="session_title",
                )
                assert terminal_llm_invocation._CURRENT_TOOL_TICKET.get() is ticket
            assert physical_invocation_required() is True
            with pytest.raises(TerminalDurabilityAbort):
                reject_unclassified_direct_mutation("root")
            await task

    assert observed == {
        "physical_required": False,
        "reason": "session_title",
        "tool_ticket": None,
    }
    assert len(partial_usage) == 1
    root_usage = partial_usage[0]
    assert (root_usage.tokens_input, root_usage.tokens_output) == (1_200, 300)

    await runtime.store.record_execution_usage(
        runtime.execution,
        input_tokens=root_usage.tokens_input,
        output_tokens=root_usage.tokens_output,
        cost_amount=0.25,
    )
    billing = await runtime.store.freeze_usage_and_claim_billing_attempt(
        runtime.execution,
    )
    assert billing is not None
    await runtime.store.record_local_billing_settlement(runtime.execution, billing)

    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            """
            SELECT input_tokens,output_tokens,usage_digest
            FROM turn_generations WHERE turn_id='t_1' AND generation=1
            """,
        ).fetchone()
        ledger = connection.execute(
            """
            SELECT tokens_input,tokens_output,delta
            FROM credit_ledger WHERE turn_id='t_1'
            """,
        ).fetchall()
    assert generation is not None
    assert generation[:2] == (1_200, 300)
    assert isinstance(generation[2], str) and generation[2]
    assert ledger == [(1_200, 300, -2)]


def test_production_tool_effect_specs_and_ticketed_catch_boundaries_are_exact() -> None:
    assert TOOL_EFFECT_NONE.effect_class is EffectClass.NONE
    assert TOOL_EFFECT_READ_ONLY.effect_class is EffectClass.READ_ONLY

    source_root = Path(__file__).parents[1] / "src" / "bowhead_core"
    effect_sites: set[tuple[str, str, str, str]] = set()
    effect_mutations: set[tuple[str, str]] = set()
    sentinel_catch_owners: set[tuple[str, str]] = set()
    ticket_scopes: set[tuple[str, str]] = set()

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ToolSpec":
                effect_keyword = next(
                    (keyword for keyword in node.keywords if keyword.arg == "effect_spec"),
                    None,
                )
                if effect_keyword is not None:
                    owner = _enclosing_function(node, parents)
                    effect_sites.add(
                        (
                            str(path.relative_to(source_root)),
                            owner,
                            _tool_schema_identity(node),
                            ast.unparse(effect_keyword.value),
                        )
                    )
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Attribute) and target.attr == "effect_spec" for target in targets):
                    effect_mutations.add(
                        (
                            str(path.relative_to(source_root)),
                            _enclosing_function(node, parents),
                        )
                    )
            if isinstance(node, ast.With) and any(
                isinstance(item.context_expr, ast.Call) and getattr(item.context_expr.func, "id", None) == "tool_invocation_ticket_scope"
                for item in node.items
            ):
                owner = _enclosing_function(node, parents)
                ticket_scopes.add((str(path.relative_to(source_root)), owner))
                _assert_unknown_rethrown_before_broad_catch(node, parents)
            if isinstance(node, ast.Try):
                handler_names = [handler.type.id for handler in node.handlers if isinstance(handler.type, ast.Name)]
                if {
                    "InvocationOutcomeUnknownError",
                    "Exception",
                } <= set(handler_names):
                    assert handler_names.index(
                        "InvocationOutcomeUnknownError",
                    ) < handler_names.index("Exception")
                    sentinel_catch_owners.add(
                        (
                            str(path.relative_to(source_root)),
                            _enclosing_function(node, parents),
                        )
                    )

    assert effect_sites == {
        # 5ccb0efd9 (reference Office reads): attachment_read only reads the confirmed upload's
        # bytes; its receiver revalidation opens BEGIN IMMEDIATE and always rolls back.
        (
            "attachment_read.py",
            "register_attachment_read",
            "attachment_read",
            "TOOL_EFFECT_READ_ONLY",
        ),
        (
            "agents/report_tools.py",
            "register_report_tools",
            "_read_report_schema()",
            "TOOL_EFFECT_READ_ONLY",
        ),
        # Coach Tools: every name has an explicit entry in _COACH_TOOL_EFFECTS and registration fails on a
        # missing or stale entry (no read-only default). Writers such as agent_working_copy_open keep no
        # effect contract, so they never take the read-only durable shortcut.
        (
            "agent_coach.py",
            "register_agent_coach_tools",
            "name",
            "_COACH_TOOL_EFFECTS[name]",
        ),
        # 4aac905ba (configuration specialist): configuration_read and configuration_result only read
        # settings and the caller-owned proposal; configuration_open only emits the Canvas open event and
        # the submit tool only delivers the final message. configuration_propose (and any other name) keeps
        # no effect contract, so it never takes the read-only durable shortcut.
        (
            "configuration_agent.py",
            "register_configuration_agent_tools",
            "name",
            "TOOL_EFFECT_READ_ONLY if name in {'configuration_read', 'configuration_result'} else TOOL_EFFECT_NONE if name in {'configuration_open', TOOL_SUBMIT} else None",
        ),
        # 15f757005 (current user profile): get_current_user only reads the requester's own account.
        (
            "current_user_tool.py",
            "register_current_user_tool",
            "get_current_user",
            "TOOL_EFFECT_READ_ONLY",
        ),
        (
            "memory_tools.py",
            "register_memory_tools",
            "recall",
            "TOOL_EFFECT_READ_ONLY",
        ),
        (
            "skill_bridge.py",
            "register_skill_tools",
            "d.name",
            "TOOL_EFFECT_READ_ONLY if d.name in {'skill_list', 'skill_read'} else None",
        ),
        (
            "subagent.py",
            "register_subagent_tool",
            "subagent_spawn",
            "TOOL_EFFECT_NONE",
        ),
        # 0ac64b97e (eval safe execution): subagent_return only returns payload, no external effect.
        (
            "subagent_return_tool.py",
            "register_subagent_return_tool",
            "schema",
            "TOOL_EFFECT_NONE",
        ),
        # 5c6052ead (Tavily web_extract / web_map, FEATURE_TAVILY_TOOLS off by default): read-only web pages.
        (
            "web_tavily_tools.py",
            "register_web_tavily_tools",
            "_SCHEMAS[tool_name]()",
            "TOOL_EFFECT_READ_ONLY",
        ),
    }
    assert all("SIDE_EFFECT" not in effect for *_site, effect in effect_sites)
    assert effect_mutations == set()
    # The forced Compute dispatch, the only ticketed dispatch outside _execute_tool, was removed;
    # every Agent Tool call now runs through SubagentRunner._execute_tool.
    assert ticket_scopes == {
        ("subagent.py", "_execute_tool"),
    }
    assert {
        ("agents/lead_orchestrator.py", "_lead_loop"),
        ("agents/lead_orchestrator.py", "run_turn"),
        ("subagent.py", "_bounded_exec"),
        ("subagent.py", "_execute_tool"),
        ("turn_runner.py", "run"),
    } <= sentinel_catch_owners


def _tool_schema_identity(node: ast.Call) -> str:
    schema = next(keyword.value for keyword in node.keywords if keyword.arg == "schema")
    if isinstance(schema, ast.Call) and getattr(schema.func, "id", None) == "ToolSchema":
        name = next(keyword.value for keyword in schema.keywords if keyword.arg == "name")
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            return name.value
        return ast.unparse(name)
    return ast.unparse(schema)


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def _assert_unknown_rethrown_before_broad_catch(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> None:
    current = node
    while current in parents:
        current = parents[current]
        if not isinstance(current, ast.Try):
            continue
        names = [handler.type.id for handler in current.handlers if isinstance(handler.type, ast.Name)]
        if "Exception" not in names:
            continue
        assert "InvocationOutcomeUnknownError" in names
        assert names.index("InvocationOutcomeUnknownError") < names.index("Exception")
        return
    raise AssertionError("ticketed ToolRegistry call has no sentinel-first catch boundary")
