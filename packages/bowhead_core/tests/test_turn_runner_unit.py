"""TurnRunner unit tests — Sprint θ Phase 8c-B adapted.

After Phase 8c-B-1 routes all turns through `lead_orchestrator.run_turn`, these
tests now exercise the public contract: TurnRunner.run → Lead → SubagentRunner →
TurnResult mapping. SubagentRunner is mocked via `FakeSubagentRunner` from
`conftest.py` (Lead reads `runtime.subagent_runner`, which is sourced from
`orch.subagent_runner`).

What was deleted (per phase-8c-b-test-adapt-strategy.md):
- iter_cap / session-budget guards: moved out of turn_runner; covered elsewhere.
- meta_intent CANVAS_PATCH / AMBIGUOUS short-circuits: code path deleted (A2 strict);
  replaced by Lead heuristic + clarification_tool sentinel (own tests).
- pre_llm_call hook: moved to SubagentRunner (own tests).
- prompt_snapshot emission: moved to SubagentRunner (own observability layer).

What was kept:
- Pure helpers (`_compute_turn_agent_type`, `_turn_used_skill`,
  `_should_skip_payload_missing_guard`).
- Event ordering (`turn_started` first).
- State hygiene (`_session_start_tokens` not leaked to orch).
- Metrics emission contract (now via lead_metrics).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.events import TerminalDurabilityAbort
from bowhead_core.types import (
    TurnRequest,
    TurnStatus,
)
from bowhead_router.errors import InvocationOutcomeUnknownError


# ─────────────────────────────────────────────────────────────
# Fake infrastructure
# ─────────────────────────────────────────────────────────────
class FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self._persist_before_live = True

    async def emit(self, event: Any) -> bool:
        self.events.append(event)
        return True

    def events_of_type(self, t: str) -> list[Any]:
        return [e for e in self.events if getattr(e, "event_type", None) == t]


class FakeEventRegistry:
    def __init__(self) -> None:
        self.bus = FakeBus()

    async def get_or_create(self, turn_id: str) -> FakeBus:
        return self.bus

    async def projection_checkpoint(self, turn_id: str):
        return self.bus, 0


class FakeConfig:
    llm_agent_type = "lead"
    max_output_tokens = 1000


class FakeRouter:
    """Returns pre-seeded LLM responses (still used by SubagentRunner if real).

    For TurnRunner-path tests we mock at SubagentRunner layer instead so this
    router is rarely consulted; kept here for legacy helper tests below.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.invocations = 0

    async def invoke(self, agent_type: str, req: Any) -> Any:
        self.invocations += 1
        if not self.responses:
            return SimpleNamespace(
                content="ok",
                thinking=None,
                tool_calls=[],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )
        return self.responses.pop(0)


class FakeToolRegistry:
    def to_schemas(self) -> list[Any]:
        return []

    def has(self, name: str) -> bool:
        return name == "subagent_spawn"

    def get(self, name: str) -> Any:
        return SimpleNamespace(needs_approval=False, risk_level="low")


def _make_orch(
    *,
    router: FakeRouter | None = None,
    config: FakeConfig | None = None,
    session_tokens_total: int = 0,
    session_store: Any = None,
    canvas_store: Any = None,
    subagent_runner: Any = None,
) -> SimpleNamespace:
    """Build a minimal Orchestrator-like SimpleNamespace for TurnRunner.run.

    For Lead-path tests, pass `subagent_runner=FakeSubagentRunner(...)` —
    turn_runner reads `getattr(orch, "subagent_runner", None)` into LeadRuntime.
    """
    from bowhead_core.events import OrchestratorEvent

    def _event(turn_id: str, event_type: str, payload: dict[str, Any]) -> OrchestratorEvent:
        return OrchestratorEvent(
            event_type=event_type,  # type: ignore[arg-type]
            turn_id=turn_id,
            payload=payload,
        )

    if session_store is None:

        async def _ensure_session(**kw: Any) -> None:
            pass

        async def _record_turn(**kw: Any) -> Any:
            return SimpleNamespace(seq=1)

        async def _require_legacy_chat_execution(**kw: Any) -> None:
            # This storage fake contains only legacy text sources.
            pass

        async def _record_turn_attachments(**kw: Any) -> None:
            pass

        async def _pin_session_attachments(**kw: Any) -> None:
            pass

        async def _list_recent_digests(**kw: Any) -> list[dict]:
            return []

        async def _list_pinned_attachments(**kw: Any) -> list[str]:
            return []

        async def _complete_turn(**kw: Any) -> None:
            pass

        async def _mark_terminal(**kw: Any) -> bool:
            return True

        async def _touch_session(**kw: Any) -> None:
            pass

        async def _save_digest(**kw: Any) -> None:
            pass

        async def _get_session_s(sid: str) -> Any:
            return SimpleNamespace(
                tokens_total=session_tokens_total,
                title="t",
            )

        session_store = SimpleNamespace(
            require_legacy_chat_execution=_require_legacy_chat_execution,
            ensure_session=_ensure_session,
            record_turn=_record_turn,
            record_turn_attachments=_record_turn_attachments,
            pin_session_attachments=_pin_session_attachments,
            list_recent_digests=_list_recent_digests,
            list_pinned_attachments=_list_pinned_attachments,
            complete_turn=_complete_turn,
            mark_completed=_mark_terminal,
            mark_failed=_mark_terminal,
            mark_cancelled=_mark_terminal,
            mark_aborted=_mark_terminal,
            mark_clarification_pending=_mark_terminal,
            touch_session=_touch_session,
            save_digest=_save_digest,
            get_session=_get_session_s,
        )

    class _FakeDatarc:
        pass

    orch = SimpleNamespace(
        event_registry=FakeEventRegistry(),
        session_store=session_store,
        canvas_store=canvas_store,
        user_memory_store=None,
        artifact_store=None,
        custom_subagent_store=None,
        datarc=_FakeDatarc(),
        datarc_client=None,
        router=router or FakeRouter([]),
        tool_registry=FakeToolRegistry(),
        config=config or FakeConfig(),
        vector_client=None,
        skill_instance_store=None,
        skill_catalog=None,
        approval_store=None,
        dreaming_light=None,
        subagent_runner=subagent_runner,
        fact_store=None,
        schema_cache=None,
        clarification_store=None,
        _event=_event,
    )
    return orch


def _req(text: str = "test", user_id: str = "u1", session_id: str = "s1") -> TurnRequest:
    return TurnRequest(
        turn_id=f"t_{hash(text) & 0xFFFF:04x}",
        session_id=session_id,
        workspace_id="1",
        member_uid="m1",
        user_id=user_id,
        text=text,
        attachments=[],
    )


@pytest.mark.asyncio
async def test_missing_legacy_admission_fails_before_store_write() -> None:
    from unittest.mock import AsyncMock

    from bowhead_core.turn_runner import TurnRunner

    orch = _make_orch()
    orch.event_registry.projection_checkpoint = AsyncMock(
        return_value=(None, 0),
    )
    orch.session_store.ensure_session = AsyncMock()

    with pytest.raises(
        RuntimeError,
        match="legacy_chat_execution_not_admitted",
    ):
        await TurnRunner(orch).run(_req("not admitted"))

    orch.session_store.ensure_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_durable_legacy_admission_fails_before_store_write() -> None:
    from unittest.mock import AsyncMock

    from bowhead_core.turn_runner import TurnRunner

    orch = _make_orch()
    orch.event_registry.bus._persist_before_live = False
    orch.session_store.ensure_session = AsyncMock()

    with pytest.raises(
        RuntimeError,
        match="legacy_chat_execution_not_durable",
    ):
        await TurnRunner(orch).run(_req("not durable"))

    orch.session_store.ensure_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_turn_started_stops_before_provider_work() -> None:
    from bowhead_core.turn_runner import TurnRunner

    router = FakeRouter([])
    orch = _make_orch(router=router)

    async def reject_started(_event: Any) -> bool:
        return False

    orch.event_registry.bus.emit = reject_started

    with pytest.raises(
        RuntimeError,
        match="turn_started_event_not_durably_accepted",
    ):
        await TurnRunner(orch).run(_req("reject first event"))

    assert router.invocations == 0


@pytest.mark.asyncio
async def test_access_context_failure_lost_cas_emits_no_false_terminal(
    monkeypatch,
) -> None:
    """A concurrent terminal writer remains authoritative at the access guard."""
    from bowhead_core.turn_runner import TurnRunner

    orch = _make_orch()
    orch.access_control_store = object()
    mark_calls = 0

    async def _lost_mark_failed(**_kwargs: Any) -> bool:
        nonlocal mark_calls
        mark_calls += 1
        return False

    orch.session_store.mark_failed = _lost_mark_failed
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")

    result = await TurnRunner(orch).run(_req("missing access context"))

    assert result.status is TurnStatus.FAILED
    assert mark_calls == 1
    terminal_events = [event.event_type for event in orch.event_registry.bus.events if event.event_type in {"turn_failed", "done", "error"}]
    assert terminal_events == []


# ─────────────────────────────────────────────────────────────
# Lead-path contract tests (adapted from old turn_runner internals)
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_turn_prefetches_execution_policy_from_its_authoritative_team(fake_subagent_runner, make_subagent_result, monkeypatch):
    from unittest.mock import AsyncMock

    from bowhead_core.execution_limits import ExecutionLimits
    from bowhead_core.turn_runner import TurnRunner

    from bowhead_core import turn_runner

    policy = ExecutionLimits(45, 90, 8388608)
    fake_subagent_runner.results = [make_subagent_result(final_answer="Policy applied")]
    orch = _make_orch(subagent_runner=fake_subagent_runner)
    orch.team_settings_store = SimpleNamespace(get=AsyncMock(side_effect=lambda team, key: policy.to_json() if key == "execution_limits" else None))
    request = _req("Inspect runtime policy")
    request.team_id = "team_policy"
    request.team_router = orch.router
    monkeypatch.setattr(turn_runner, "_make_subagent_runner_with_catalog", lambda **kwargs: fake_subagent_runner)
    result = await TurnRunner(orch).run(request)
    assert result.status == TurnStatus.COMPLETED
    assert fake_subagent_runner.invocations[0]["parent_ctx"].execution_limits == policy
    assert sum(call.args == ("team_policy", "execution_limits") for call in orch.team_settings_store.get.await_args_list) == 1


@pytest.mark.asyncio
async def test_happy_path_returns_subagent_final_answer(
    fake_subagent_runner,
    make_subagent_result,
) -> None:
    """Lead → SubagentRunner returns SubagentResult; TurnResult mirrors content + tokens."""
    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.results = [
        make_subagent_result(final_answer="answer body", tokens_input=50, tokens_output=20),
    ]
    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )
    result = await TurnRunner(orch).run(_req("今年 GMV"))
    assert result.status == TurnStatus.COMPLETED
    assert result.final_answer == "answer body"
    assert result.tokens_input == 50
    assert result.tokens_output == 20
    # Lead delegated to SubagentRunner with task=user_text, profile=lead
    assert len(fake_subagent_runner.invocations) == 1
    inv = fake_subagent_runner.invocations[0]
    assert inv["profile_name"] == "lead"
    assert inv["task"] == "今年 GMV"


@pytest.mark.asyncio
async def test_request_attachments_auto_pinned(
    fake_subagent_runner,
    make_subagent_result,
) -> None:
    """M1 RD-2: request.attachments are auto-pinned to the session every turn
    (turn-lifecycle pin), so an upload attached in turn 1 survives to later
    turns with NO re-upload — the s_a9b693829c3d lost-PPT fix. Default-ON.
    """
    from unittest.mock import AsyncMock

    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )
    pin = AsyncMock()
    orch.session_store.pin_session_attachments = pin
    req = TurnRequest(
        turn_id="t_pin01",
        session_id="s1",
        workspace_id="1",
        member_uid="m1",
        user_id="u1",
        text="根据这个ppt出报告",
        attachments=["art_tmpl"],
    )
    await TurnRunner(orch).run(req)
    pin.assert_awaited_once()
    _, kwargs = pin.call_args
    assert kwargs["session_id"] == "s1"
    assert kwargs["artifact_ids"] == ["art_tmpl"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_inject", ["0", "1"])
async def test_office_request_positions_survive_pin_order_and_parser_partition(
    fake_subagent_runner,
    make_subagent_result,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    pin_inject,
) -> None:
    """Real storage + preprocessing + Lead prompt; model completion is external.

    The second current file was pinned before the first. A list-only data
    attachment must retain its position without reading its body. Next turn
    reuses the Office ID without confusing history with new attachments.
    """
    from unittest.mock import AsyncMock

    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.documents.package_io import OFFICE_MIMES
    from bowhead_core.turn_runner import TurnRunner

    monkeypatch.setenv("BOWHEAD_PSI_PIN_INJECT", pin_inject)
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "files"), db_path=tmp_path / "core.db")
    await store.initialize()
    prior = await store.put_bytes(created_by=None, content=b"HISTORICAL_BODY", filename="old.pptx", mime=OFFICE_MIMES[".pptx"], source="upload")
    second = await store.put_bytes(created_by=None, content=b"SECOND_BODY", filename="source.pptx", mime="application/zip", source="upload")
    first = await store.put_bytes(
        created_by=None, content=b"CSV_BODY", filename="data.csv", mime="text/csv", source="python_sandbox", kind="tool_data"
    )
    assert first.inject == "list"
    reader = AsyncMock(wraps=store.open_read)
    monkeypatch.setattr(store, "open_read", reader)
    fake_subagent_runner.results = [make_subagent_result(final_answer="已收到引用"), make_subagent_result(final_answer="同一个文件")]
    orch = _make_orch(subagent_runner=fake_subagent_runner)
    orch.artifact_store = store
    orch.session_store.list_pinned_attachments = AsyncMock(return_value=[prior.id, second.id, first.id])
    request = TurnRequest(
        turn_id="t_office_scope",
        session_id="s1",
        workspace_id="1",
        member_uid="m1",
        user_id="u1",
        text="第二份是模板，第一份是数据",
        attachments=[first.id, second.id],
    )
    from bowhead_core.chat_turn_source import ChatTurnUserSource

    orch.session_store.get_chat_turn_user_source = AsyncMock(
        return_value=ChatTurnUserSource(
            turn_id=request.turn_id,
            session_id=request.session_id,
            user_id=request.user_id,
            workspace_id=request.workspace_id,
            user_text=request.text,
            attachment_ids=tuple(request.attachments),
            attachment_refs=tuple({"fileId": aid} for aid in request.attachments),
            parts=None,
        )
    )
    result = await TurnRunner(orch).run(request)
    assert result.status == TurnStatus.COMPLETED

    def prompt_refs(invocation):
        import re

        prompt = (invocation.get("extra_instructions") or "") + (invocation.get("turn_context_block") or "")
        refs = [dict(re.findall(r' (id|current_message_position)="([^"]*)"', header)) for header in re.findall(r"<attachment [^>]+>", prompt)]
        return prompt, [(r["id"], int(r["current_message_position"]) if "current_message_position" in r else None) for r in refs]

    expected = [(first.id, 1), (second.id, 2)]
    if pin_inject == "1":
        expected.append((prior.id, None))
    # Prefix reordering can put attachments in the tail rather than the system
    # prefix; assert the combined actual inputs seen by the Lead runner.
    prompt, refs = prompt_refs(fake_subagent_runner.invocations[0])
    assert refs == expected
    assert 'current_message_position="2"' in prompt
    assert "SECOND_BODY" not in prompt and "HISTORICAL_BODY" not in prompt and "CSV_BODY" not in prompt
    assert not [
        event
        for event in orch.event_registry.bus.events
        if event.event_type in {"step_started", "step_done"} and event.payload.get("step_type") == "parse"
    ]
    reader.assert_not_awaited()
    assert await store.get(second.id) == second
    if pin_inject == "1":
        later = TurnRequest(
            turn_id="t_office_next", session_id="s1", workspace_id="1", member_uid="m1", user_id="u1", text="刚才那个 PPT 只是参考", attachments=[]
        )
        await TurnRunner(orch).run(later)
        _, next_refs = prompt_refs(fake_subagent_runner.invocations[1])
        assert next_refs == [(prior.id, None), (second.id, None)]
        reader.assert_not_awaited()


@pytest.mark.asyncio
async def test_injection_policy_skips_list_kind_parse(
    fake_subagent_runner,
    make_subagent_result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 RD-6 Gate A (authoritative): a list-kind pinned CSV is NOT parsed
    (zero parse-step / zero extraction), while a full-kind upload IS — this is
    the CSV-flood fix that stops drug-data CSVs from burying the PPT template.
    """
    from types import SimpleNamespace

    from bowhead_core.artifact_store import Artifact
    from bowhead_core.attachments_injector import PreparedAttachment
    from bowhead_core.turn_runner import TurnRunner

    parsed_aids: list[str] = []

    async def _fake_prepare(*, store, artifact_ids):
        parsed_aids.extend(artifact_ids)
        aid = artifact_ids[0]
        return [
            PreparedAttachment(
                artifact_id=aid,
                filename=aid,
                mime="",
                size=0,
                extracted_text="x",
                truncated=False,
                summary_only=False,
                error=None,
            )
        ]

    monkeypatch.setattr("bowhead_core.turn_runner.prepare_attachments", _fake_prepare)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )

    def _mk(aid: str, kind: str, inject: str) -> Artifact:
        return Artifact(
            id=aid,
            filename=f"{aid}.f",
            mime="text/csv",
            size=10,
            source="x",
            path="/x",
            canvas_id=None,
            created_by=None,
            created_at="2026",
            kind=kind,
            inject=inject,
        )

    store_map = {
        "art_csv": _mk("art_csv", "tool_data", "list"),  # pinned flood CSV
        "art_pptx": _mk("art_pptx", "user_upload", "full"),  # the template
    }

    async def _get(aid: str, **kw):
        return store_map.get(aid)

    orch.artifact_store = SimpleNamespace(get=_get)

    async def _list_pinned(**kw) -> list[str]:
        return ["art_csv"]

    orch.session_store.list_pinned_attachments = _list_pinned

    req = TurnRequest(
        turn_id="t_inj01",
        session_id="s1",
        workspace_id="1",
        member_uid="m1",
        user_id="u1",
        text="按这个ppt出报告",
        attachments=["art_pptx"],
    )
    await TurnRunner(orch).run(req)

    assert "art_pptx" in parsed_aids  # full upload IS parsed
    assert "art_csv" not in parsed_aids  # list CSV is skipped → zero parse


@pytest.mark.asyncio
async def test_no_attachments_no_autopin(fake_subagent_runner, make_subagent_result) -> None:
    """No request.attachments → no pin call (don't churn session_attachments)."""
    from unittest.mock import AsyncMock

    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )
    pin = AsyncMock()
    orch.session_store.pin_session_attachments = pin
    await TurnRunner(orch).run(_req("今年 GMV"))
    pin.assert_not_awaited()


@pytest.mark.asyncio
async def test_subagent_exception_routes_to_failed_status(fake_subagent_runner) -> None:
    """Lead terminal events keep a stable code and never publish exception text."""
    from bowhead_core.turn_runner import TurnRunner

    secret = "db_password=terminal-producer-secret"
    fake_subagent_runner.raise_exc = RuntimeError(secret)
    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )
    result = await TurnRunner(orch).run(_req())
    assert result.status == TurnStatus.FAILED
    assert "lead_internal_error" in (result.error or "")
    bus = orch.event_registry.bus
    assert bus.events_of_type("turn_failed")
    # done event is emitted by both Lead (status=error) and turn_runner (final wrap);
    # ensure at least one done is present so SSE bus closes cleanly
    assert bus.events_of_type("done")
    terminal_payloads = [event.payload for event in bus.events if event.event_type in {"turn_failed", "done"}]
    assert all(secret not in repr(payload) for payload in terminal_payloads)
    assert terminal_payloads[-1]["error"] == "lead_internal_error"
    turn_failed = next(event.payload for event in bus.events if event.event_type == "turn_failed")
    done = next(event.payload for event in bus.events if event.event_type == "done" and event.payload.get("status") == "error")
    assert turn_failed["diagnostics"] == done["diagnostics"]
    assert turn_failed["diagnostics"]["turn_id"] == result.turn_id
    assert len(turn_failed["diagnostics"]["occurrence_id"]) == 32


@pytest.mark.asyncio
async def test_durable_unknown_escapes_without_legacy_terminal_events(
    fake_subagent_runner,
) -> None:
    """The full Lead catch chain must not rewrite durable UNKNOWN as failed."""

    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.raise_exc = InvocationOutcomeUnknownError(
        "tool_handler_outcome_unknown",
    )
    orch = _make_orch(subagent_runner=fake_subagent_runner)

    with pytest.raises(InvocationOutcomeUnknownError) as caught:
        await TurnRunner(orch).run(_req())

    assert caught.value.reason_code == "tool_handler_outcome_unknown"
    assert not orch.event_registry.bus.events_of_type("turn_failed")
    assert not orch.event_registry.bus.events_of_type("done")


@pytest.mark.asyncio
async def test_durability_abort_escapes_without_legacy_terminal_events(
    fake_subagent_runner,
) -> None:
    """A durable fail-stop hands off to recovery without legacy finalization."""

    from bowhead_core.turn_runner import TurnRunner

    abort = TerminalDurabilityAbort("durable tool marker is unavailable")
    fake_subagent_runner.raise_exc = abort
    orch = _make_orch(subagent_runner=fake_subagent_runner)

    with pytest.raises(TerminalDurabilityAbort) as caught:
        await TurnRunner(orch).run(_req())

    assert caught.value is abort
    assert not orch.event_registry.bus.events_of_type("turn_failed")
    assert not orch.event_registry.bus.events_of_type("done")


@pytest.mark.asyncio
async def test_durable_fail_stop_skips_citation_flush_and_always_cleans(
    fake_subagent_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.agents import lead_orchestrator
    from bowhead_core.turn_runner import TurnRunner

    unknown = InvocationOutcomeUnknownError("citation_fail_stop")
    captured_ctx: list[Any] = []

    async def _register_citation_then_fail(**kwargs: Any) -> Any:
        parent_ctx = kwargs["parent_ctx"]
        parent_ctx.turn_citation_registry = object()
        captured_ctx.append(parent_ctx)
        raise unknown

    fake_subagent_runner.run = _register_citation_then_fail
    flush_calls = 0

    async def _forbidden_flush(*_args: Any, **_kwargs: Any) -> None:
        nonlocal flush_calls
        flush_calls += 1
        raise TerminalDurabilityAbort("citation emit must not replace UNKNOWN")

    monkeypatch.setattr(lead_orchestrator, "_flush_citations", _forbidden_flush)
    request = _req("citation fail-stop cleanup")
    current_task = asyncio.current_task()
    assert current_task is not None
    lead_orchestrator.register_in_flight_turn(request.turn_id, current_task)
    orch = _make_orch(subagent_runner=fake_subagent_runner)

    try:
        with pytest.raises(InvocationOutcomeUnknownError) as caught:
            await TurnRunner(orch).run(request)
    finally:
        lead_orchestrator._IN_FLIGHT_TURNS.pop(request.turn_id, None)

    assert caught.value is unknown
    assert flush_calls == 0
    assert request.turn_id not in lead_orchestrator._IN_FLIGHT_TURNS
    assert captured_ctx and not hasattr(
        captured_ctx[0],
        "turn_citation_registry",
    )


@pytest.mark.asyncio
async def test_terminal_raw_cancellation_is_not_rewritten_as_user_stop(
    fake_subagent_runner,
) -> None:
    from bowhead_core.turn_runner import TurnRunner

    cancellation = asyncio.CancelledError("tool_handler_cancelled")
    fake_subagent_runner.raise_exc = cancellation
    orch = _make_orch(subagent_runner=fake_subagent_runner)
    orch.event_registry.bus.terminal_protocol_version = 1

    with pytest.raises(asyncio.CancelledError) as caught:
        await TurnRunner(orch).run(_req("terminal raw cancellation"))

    assert caught.value is cancellation
    assert not orch.event_registry.bus.events_of_type("turn_aborted")
    assert not orch.event_registry.bus.events_of_type("done")


@pytest.mark.asyncio
async def test_legacy_raw_cancellation_keeps_existing_user_abort_projection(
    fake_subagent_runner,
) -> None:
    from bowhead_core.turn_runner import TurnRunner

    fake_subagent_runner.raise_exc = asyncio.CancelledError("legacy cancel")
    orch = _make_orch(subagent_runner=fake_subagent_runner)

    result = await TurnRunner(orch).run(_req("legacy raw cancellation"))

    assert result.status is TurnStatus.ABORTED
    assert orch.event_registry.bus.events_of_type("turn_aborted")
    assert orch.event_registry.bus.events_of_type("done")


@pytest.mark.asyncio
async def test_stream_cleanup_cannot_replace_durable_unknown(
    fake_subagent_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.canvas_composers import streaming
    from bowhead_core.turn_runner import TurnRunner

    unknown = InvocationOutcomeUnknownError("stream_cleanup_unknown")
    fake_subagent_runner.raise_exc = unknown

    class FailingStreamingDecomposer:
        emitted_section_ids: list[str] = []

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def abort_open_section(self, *, reason: str) -> None:
            del reason
            raise TerminalDurabilityAbort("secondary stream cleanup failure")

    monkeypatch.setenv("BOWHEAD_STREAMING_LEAD", "1")
    monkeypatch.setenv("BOWHEAD_LEAD_CANVAS_AUTOCHOP", "1")
    monkeypatch.setattr(
        streaming,
        "StreamingDecomposer",
        FailingStreamingDecomposer,
    )
    orch = _make_orch(subagent_runner=fake_subagent_runner)

    with pytest.raises(InvocationOutcomeUnknownError) as caught:
        await TurnRunner(orch).run(_req("stream cleanup fail-stop"))

    assert caught.value is unknown
    assert not orch.event_registry.bus.events_of_type("turn_failed")
    assert not orch.event_registry.bus.events_of_type("done")


@pytest.mark.asyncio
async def test_run_turn_unexpected_exception_does_not_publish_raw_text(
    fake_subagent_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer run_turn safety net also keeps event and replay state code-only."""
    from bowhead_core.agents import lead_orchestrator
    from bowhead_core.turn_runner import TurnRunner

    secret = "Authorization: Bearer terminal-producer-secret"

    async def _raise_unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(lead_orchestrator, "_lead_loop", _raise_unexpected)
    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )

    result = await TurnRunner(orch).run(_req("outer safety net"))

    assert result.status == TurnStatus.FAILED
    assert result.error == "lead_internal_error"
    terminal_payloads = [event.payload for event in orch.event_registry.bus.events if event.event_type in {"turn_failed", "done"}]
    assert terminal_payloads
    assert all(secret not in repr(payload) for payload in terminal_payloads)
    assert terminal_payloads[-1]["error"] == "lead_internal_error"
    assert terminal_payloads[0]["diagnostics"] == terminal_payloads[-1]["diagnostics"]


@pytest.mark.asyncio
async def test_subagent_not_ok_returns_failed_with_error(
    fake_subagent_runner,
    make_subagent_result,
) -> None:
    """Arbitrary SubagentResult.error text never crosses the turn boundary."""
    from bowhead_core.turn_runner import TurnRunner

    secret = "Authorization: Bearer downstream-secret-should-not-cross-terminal-events"
    fake_subagent_runner.results = [
        make_subagent_result(
            final_answer="",
            ok=False,
            error=secret,
        ),
    ]
    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )
    result = await TurnRunner(orch).run(_req())
    assert result.status == TurnStatus.FAILED
    assert result.error == "lead_internal_error"
    terminal_payloads = [event.payload for event in orch.event_registry.bus.events if event.event_type in {"turn_failed", "done"}]
    assert terminal_payloads
    assert all(secret not in repr(payload) for payload in terminal_payloads)
    assert terminal_payloads[0]["reason"] == "lead_internal_error"
    assert terminal_payloads[-1]["error"] == "lead_internal_error"
    assert terminal_payloads[0]["diagnostics"] == terminal_payloads[-1]["diagnostics"]


@pytest.mark.parametrize(
    ("internal_error", "public_reason"),
    [
        ("token_cap_exhausted: 1000 >= 1000", "token_cap_exhausted"),
        ("peer_wallclock_exhausted: 90s > 60s", "peer_dispatch_timeout"),
        ("peer_total_failures=8", "peer_dispatch_network_error"),
        ("peer_no_commit_force_bail", "peer_dispatch_network_error"),
        ("subagent_internal_error", "lead_internal_error"),
    ],
)
def test_subagent_failure_reason_projection_is_finite(
    internal_error: str,
    public_reason: str,
) -> None:
    from bowhead_core.agents.lead_orchestrator import _public_turn_failure_reason

    assert _public_turn_failure_reason(internal_error) == public_reason


@pytest.mark.asyncio
async def test_metrics_emitted_on_completion(fake_subagent_runner, make_subagent_result) -> None:
    """Lead emits lead_metrics outcome counter on success."""
    from bowhead_core.turn_runner import TurnRunner

    # lead_metrics emits to global registry; capture pre-count delta
    from bowhead_core import metrics as _m

    _m.get_registry().reset()

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )
    await TurnRunner(orch).run(_req())
    out = _m.get_registry().render_prometheus()
    # lead_metrics.emit_outcome → bowhead_lead_orchestrator_outcome_total{outcome="succeeded"}
    assert "bowhead_lead_orchestrator_outcome_total" in out
    assert 'outcome="succeeded"' in out
    _m.get_registry().reset()


# ─────────────────────────────────────────────────────────────
# Top-level event order — turn_started first, done last
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_turn_started_event_always_first(fake_subagent_runner) -> None:
    """turn_runner emits turn_started before delegating to Lead."""
    from bowhead_core.turn_runner import TurnRunner

    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
    )
    await TurnRunner(orch).run(_req())
    first = orch.event_registry.bus.events[0]
    assert first.event_type == "turn_started"


@pytest.mark.asyncio
async def test_session_state_is_local_not_leaked_to_orch(fake_subagent_runner) -> None:
    """#128 Step 3c: TurnRunner shouldn't write scratchpad attrs onto orch."""
    from bowhead_core.turn_runner import TurnRunner

    orch = _make_orch(
        subagent_runner=fake_subagent_runner,
        session_tokens_total=5000,
    )
    await TurnRunner(orch).run(_req())
    assert not hasattr(orch, "_session_start_tokens")


# ─── W12-1 (#72) _compute_turn_agent_type pure helper ───────────────
def test_compute_agent_type_no_invocations_returns_lead() -> None:
    from bowhead_core.turn_runner import _compute_turn_agent_type

    assert _compute_turn_agent_type([]) == "lead"


def test_compute_agent_type_no_spawn_returns_lead() -> None:
    """非 subagent.spawn 的 invocations 不影响, 返 lead."""
    from bowhead_core.turn_runner import _compute_turn_agent_type
    from bowhead_core.types import ToolInvocation

    invs = [
        ToolInvocation(tool_name="bi_ask_question", arguments={"text": "GMV"}),
        ToolInvocation(tool_name="bi_attribute", arguments={"dim": "city"}),
    ]
    assert _compute_turn_agent_type(invs) == "lead"


def test_compute_agent_type_single_spawn_returns_profile() -> None:
    """1 次 spawn → 取 spawn 的 profile."""
    from bowhead_core.turn_runner import _compute_turn_agent_type
    from bowhead_core.types import ToolInvocation

    invs = [
        ToolInvocation(tool_name="subagent_spawn", arguments={"profile": "compute", "task": "..."}),
    ]
    assert _compute_turn_agent_type(invs) == "compute"


def test_compute_agent_type_multiple_spawns_takes_last() -> None:
    """多次 spawn → 取最后一次 (V3 review §C.1: 主导原则)."""
    from bowhead_core.turn_runner import _compute_turn_agent_type
    from bowhead_core.types import ToolInvocation

    invs = [
        ToolInvocation(tool_name="subagent_spawn", arguments={"profile": "compute", "task": "lookup"}),
        ToolInvocation(tool_name="bi_ask_question", arguments={"text": "..."}),
        ToolInvocation(tool_name="subagent_spawn", arguments={"profile": "modeler", "task": "deep"}),
    ]
    assert _compute_turn_agent_type(invs) == "modeler"


def test_compute_agent_type_spawn_without_profile_falls_back() -> None:
    """spawn arguments 不带 profile 字段 → 回落 lead (异常防御)."""
    from bowhead_core.turn_runner import _compute_turn_agent_type
    from bowhead_core.types import ToolInvocation

    invs = [
        ToolInvocation(tool_name="subagent_spawn", arguments={"task": "no profile"}),
    ]
    assert _compute_turn_agent_type(invs) == "lead"


# ─── W12-3 (#74) _turn_used_skill helper ────────────────────────
def test_turn_used_skill_empty_returns_false() -> None:
    from bowhead_core.turn_runner import _turn_used_skill

    assert _turn_used_skill([]) is False


def test_turn_used_skill_no_skill_invoke_returns_false() -> None:
    from bowhead_core.turn_runner import _turn_used_skill
    from bowhead_core.types import ToolInvocation

    invs = [
        ToolInvocation(tool_name="bi_ask_question", arguments={"text": "GMV"}),
        ToolInvocation(tool_name="subagent_spawn", arguments={"profile": "compute"}),
    ]
    assert _turn_used_skill(invs) is False


def test_turn_used_skill_top_level_invoke_returns_true() -> None:
    from bowhead_core.turn_runner import _turn_used_skill
    from bowhead_core.types import ToolInvocation

    invs = [
        ToolInvocation(tool_name="skill_invoke", arguments={"name": "python_sandbox"}),
    ]
    assert _turn_used_skill(invs) is True


def test_turn_used_skill_nested_invoke_returns_true() -> None:
    """subagent 内嵌的 skill.invoke 也算 (常见场景: Lead spawn analyst, analyst 调 skill)."""
    from bowhead_core.turn_runner import _turn_used_skill
    from bowhead_core.types import ToolInvocation

    nested = [
        ToolInvocation(tool_name="bi_ask_question", arguments={"text": "x"}),
        ToolInvocation(tool_name="skill_invoke", arguments={"name": "python_sandbox"}),
    ]
    invs = [
        ToolInvocation(tool_name="subagent_spawn", arguments={"profile": "compute"}, nested=nested),
    ]
    assert _turn_used_skill(invs) is True


# ─── W12 #91 _should_skip_payload_missing_guard helper ────────────
def test_should_skip_guard_empty_returns_false() -> None:
    """空 / None final_answer → 必走 guard (没东西可显)."""
    from bowhead_core.turn_runner import _should_skip_payload_missing_guard

    assert _should_skip_payload_missing_guard(None) is False
    assert _should_skip_payload_missing_guard("") is False


def test_should_skip_guard_short_answer_returns_false() -> None:
    """Lead 只写很短 fallback 句子 (<500 字) → guard 该出 (避免空白 UI)."""
    from bowhead_core.turn_runner import _should_skip_payload_missing_guard

    short = "我没办法回答这个问题。" * 10
    assert _should_skip_payload_missing_guard(short) is False


def test_should_skip_guard_substantive_answer_returns_true() -> None:
    """Lead 写出 ≥500 字真 report → skip guard (避免 fallback 盖真内容)."""
    from bowhead_core.turn_runner import _should_skip_payload_missing_guard

    full_report = ("### 2025 示例药品甲 vs 示例药品乙 Competitive Landscape & Strategy Report\nExecutive Summary\n") + (
        "示例药品甲 holds a narrow 53.7% overall market share. " * 30
    )
    assert _should_skip_payload_missing_guard(full_report) is True


def test_should_skip_guard_whitespace_only_returns_false() -> None:
    """只 whitespace 的 'long' 字符串 → 不算 substantive (strip 后 0 字)."""
    from bowhead_core.turn_runner import _should_skip_payload_missing_guard

    ws = "   \n\n   \n" * 200
    assert _should_skip_payload_missing_guard(ws) is False


# ── byo-no-clone: per-turn effective router / KB / subagent-runner ───────────
# Regression coverage for the prod P0 where every BYO-team turn's subagent_spawn
# crashed with "subagent_runner/bus not configured on context": the gateway used
# dataclasses.replace(orchestrator, router=team_router) which dropped orch's
# dynamically-attached subagent_runner sidecar. Fix carries the team router on the
# request and builds the runner fresh from the effective router.


def test_effective_router_prefers_request_team_router() -> None:
    from bowhead_core.turn_runner import _effective_router

    orch = SimpleNamespace(router="GLOBAL_ROUTER")
    assert _effective_router(orch, SimpleNamespace(team_router="TEAM_ROUTER")) == "TEAM_ROUTER"
    assert _effective_router(orch, SimpleNamespace(team_router=None)) == "GLOBAL_ROUTER"
    # missing attr entirely → still falls back (defensive getattr)
    assert _effective_router(orch, SimpleNamespace()) == "GLOBAL_ROUTER"


def test_effective_kb_retriever_prefers_request() -> None:
    from bowhead_core.turn_runner import _effective_kb_retriever

    orch = SimpleNamespace(kb_retriever="GLOBAL_KB")
    assert _effective_kb_retriever(orch, SimpleNamespace(team_id="team_1", team_kb_retriever="TEAM_KB")) == "TEAM_KB"
    assert _effective_kb_retriever(orch, SimpleNamespace(team_id="team_1", team_kb_retriever=None)) is None
    assert _effective_kb_retriever(orch, SimpleNamespace(team_id=None, team_kb_retriever=None)) == "GLOBAL_KB"


def test_effective_subagent_runner_built_from_team_router() -> None:
    """核心回归：BYO 团队 turn 的子 agent runner 用**团队 router** 现建（非全局单例）。

    旧代码读 orch.subagent_runner（全局 router 焊死的单例），且 clone 路径会把它丢成 None →
    subagent_spawn 崩「subagent_runner/bus not configured on context」。修复后：不依赖该 sidecar，
    从 effective router 现建 → 子 agent 真走团队 BYO 模型。
    """
    from bowhead_core.turn_runner import _effective_subagent_runner

    orch = SimpleNamespace(
        router="GLOBAL_ROUTER",
        tool_registry=None,
        custom_subagent_store=None,
        product_terms_catalog=None,
        # subagent_runner intentionally ABSENT — the fix must NOT read it.
    )
    byo = _effective_subagent_runner(orch, SimpleNamespace(team_router="TEAM_ROUTER"))
    assert byo is not None
    assert byo.router == "TEAM_ROUTER"  # subagents honor the team BYO model
    # non-BYO, no singleton on orch → build from orch.router (harness path).
    plain = _effective_subagent_runner(orch, SimpleNamespace(team_router=None))
    assert plain is not None
    assert plain.router == "GLOBAL_ROUTER"
    # non-BYO WITH a pre-set orch.subagent_runner → reuse it verbatim (prod singleton /
    # test injection): behavior unchanged for the common non-BYO path.
    sentinel_runner = object()
    orch_with_singleton = SimpleNamespace(router="GLOBAL_ROUTER", subagent_runner=sentinel_runner)
    assert _effective_subagent_runner(orch_with_singleton, SimpleNamespace(team_router=None)) is sentinel_runner
    # ...but BYO still builds fresh even when a singleton exists (must NOT reuse global-router one).
    byo2 = _effective_subagent_runner(
        SimpleNamespace(
            router="GLOBAL_ROUTER", subagent_runner=sentinel_runner, tool_registry=None, custom_subagent_store=None, product_terms_catalog=None
        ),
        SimpleNamespace(team_router="TEAM_ROUTER"),
    )
    assert byo2 is not sentinel_runner and byo2.router == "TEAM_ROUTER"
    # no router anywhere → None (the spawn handler still guards, harness-safe)
    assert _effective_subagent_runner(SimpleNamespace(router=None), SimpleNamespace(team_router=None)) is None


def test_turn_request_carries_and_inherits_team_router() -> None:
    """team_router/team_kb_retriever 是 TurnRequest 字段；派生请求（steer/resume via
    dataclasses.replace(req)）自动继承 —— 不再 clone Orchestrator 的关键收益。"""
    import dataclasses

    from bowhead_core.types import TurnRequest

    fields = {f.name for f in dataclasses.fields(TurnRequest)}
    assert {"team_router", "team_kb_retriever"} <= fields

    req = TurnRequest(
        session_id="s",
        workspace_id="w",
        user_id="u",
        member_uid="m",
        text="hi",
        team_router="TEAM_ROUTER",
        team_kb_retriever="TEAM_KB",
    )
    derived = dataclasses.replace(req, turn_id="t_new", text="continue")
    assert derived.team_router == "TEAM_ROUTER"
    assert derived.team_kb_retriever == "TEAM_KB"
    # default None → non-BYO turns unaffected.
    plain = TurnRequest(session_id="s", workspace_id="w", user_id="u", member_uid="m", text="hi")
    assert plain.team_router is None
    assert plain.team_kb_retriever is None


@pytest.mark.asyncio
async def test_turn_head_pins_effective_output_language(
    fake_subagent_runner,
    make_subagent_result,
) -> None:
    """多语言批1 接线闸：`run_turn` 必须在 **turn 头**把生效语言钉到 bus。

    钉不上就会退回批1 之前的病：silent-done 路径（本轮没产出/报错）读到空 pin，
    `turn_empty_hint` / `no_report_this_turn` 一律落 zh-CN 兜底
    —— 英文用户的失败提示是中文的。单测只钉 helper 钉不住这条**接线**，必须过真 run。
    """
    from bowhead_core.effective_language import read_turn_pin
    from bowhead_core.language_context import LanguageContext
    from bowhead_core.turn_runner import TurnRunner

    seen: list[str] = []

    class _Recorder:
        """Lead 一开跑就把当时的 pin 记下来 —— 证明它在 turn **头**就已经钉好，
        而不是等到 chat final 组装那个晚写点。"""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def run(self, **kwargs: Any) -> Any:
            seen.append(read_turn_pin(kwargs.get("parent_bus")))
            return await self._inner.run(**kwargs)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(subagent_runner=_Recorder(fake_subagent_runner))
    req = _req("这轮问什么不重要")
    req.language_context = LanguageContext(ui_language="en-US", output_language="en-US")

    await TurnRunner(orch).run(req)

    assert seen == ["en-US"], f"turn 头没钉上生效语言：{seen}"


@pytest.mark.asyncio
async def test_turn_head_pin_absent_when_no_language_context(
    fake_subagent_runner,
    make_subagent_result,
) -> None:
    """Rollback path: `language_context=None` pins nothing; readers fall back to English."""
    from bowhead_core.effective_language import effective_output_language, read_turn_pin
    from bowhead_core.turn_runner import TurnRunner

    seen: list[str] = []

    class _Recorder:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def run(self, **kwargs: Any) -> Any:
            bus = kwargs.get("parent_bus")
            seen.append(read_turn_pin(bus))
            seen.append(effective_output_language(bus=bus, language_context=None))
            return await self._inner.run(**kwargs)

    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(subagent_runner=_Recorder(fake_subagent_runner))
    await TurnRunner(orch).run(_req("老路径"))

    assert seen == ["", "en-US"]


@pytest.mark.asyncio
async def test_output_language_contextvar_does_not_leak_across_turns(
    fake_subagent_runner,
    make_subagent_result,
) -> None:
    """批3 卫生闸：turn 尾必须复位输出语言 ContextVar。

    不复位 = 同一个 worker 上的**下一轮**捡到上一轮的语言（chrome 全跟错）。
    这类泄漏在单测里最容易被"每个用例各自设一遍"掩盖，所以判据取**跑完之后**的值。
    """
    from bowhead_core.agents._lead_context import get_current_output_language
    from bowhead_core.language_context import LanguageContext
    from bowhead_core.turn_runner import TurnRunner

    before = get_current_output_language()
    fake_subagent_runner.results = [make_subagent_result(final_answer="ok")]
    orch = _make_orch(subagent_runner=fake_subagent_runner)
    req = _req("英文轮")
    req.language_context = LanguageContext(ui_language="en-US", output_language="en-US")
    await TurnRunner(orch).run(req)

    assert get_current_output_language() == before, "输出语言 ContextVar 跨 turn 泄漏了"


@pytest.mark.asyncio
async def test_swallowed_shutdown_cancel_is_not_reported_as_internal_error(
    fake_subagent_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """116 `t_c712ab662abd`：服务停机取消正在跑的 turn，团队工具会话的 __aexit__ 把取消吞了，
    随后读 `_lead_result` 抛 UnboundLocalError ⇒ 整轮报成 lead_internal_error。
    取消必须照原样往上抛（交给派活租约恢复标成「被打断」），不发 turn_failed。"""
    from bowhead_core.agents import lead_orchestrator
    from bowhead_core.turn_runner import TurnRunner

    started = asyncio.Event()

    async def _slow_lead(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        await asyncio.sleep(30)

    async def _swallowing_view(self: Any, stack: Any, *_args: Any, **_kwargs: Any) -> Any:
        stack.push_async_exit(_swallow)
        return None

    async def _swallow(*_exc: Any) -> bool:
        return True

    monkeypatch.setattr(lead_orchestrator, "run_turn", _slow_lead)
    monkeypatch.setattr(TurnRunner, "_build_team_tool_view", _swallowing_view)
    orch = _make_orch(subagent_runner=fake_subagent_runner)

    task = asyncio.create_task(TurnRunner(orch).run(_req("shutdown mid-run")))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not orch.event_registry.bus.events_of_type("turn_failed")
