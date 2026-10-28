"""Lead consumes the reserved exact primary at the actual spawn boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from functools import partial

import pytest
import pytest_asyncio
from bowhead_core.chat_reference_delegation import ReferenceDelegation, render_primary_delegation_task
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.subagent import SubagentResult, SubagentRunner
from bowhead_core.tool_registry import build_default_registry
from test_reference_primary_reader import _prepare_case
from test_reference_primary_reader import start_case as _start_case
from test_reference_primary_runtime import _ctx, _rows, _runtime
from test_subagent import _router

from bowhead_router import LLMResponse

start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def lead_case(start_case):
    case = await _prepare_case(start_case)
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1")
    await case.store.finalize_started_chat_submission(**case.args)
    return case


async def test_reserved_primary_starts_on_delegation_and_preserves_both_tasks(lead_case):
    case = lead_case
    runtime = await _runtime(case)
    primary = await runtime.validate()
    assert primary.binding.status == "admitted"
    assert _rows(case, "turn_agent_execution")[0]["started_at"] is None
    assert (await runtime.lead_input()).runtime is runtime
    task = "核对本季度利润结构，然后给出一条建议"
    dispatched_task = render_primary_delegation_task(primary.source.input_document.text_content(), task)
    router = _router(LLMResponse(content="本季度利润分析结果"))
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), team_subagent_store=case.fixture.users)
    result = await runtime.run(
        runner=runner,
        parent_ctx=_ctx(case),
        parent_bus=TurnEventBus(),
        delegation_task=task,
        delegation_call_id="call-primary",
        extra_instructions="请使用中文",
    )
    assert result.ok
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    original, actual = _rows(case, "chat_reference_resolutions")
    delegation = ReferenceDelegation.model_validate_json(actual["composition_json"])
    assert delegation.task_hash == "sha256:" + hashlib.sha256(dispatched_task.encode()).hexdigest()
    assert delegation.task_hash != json.loads(original["composition_json"])["task_hash"]
    assert delegation.source_composition_digest == original["composition_digest"]
    from bowhead_core.chat_reference_projection import read_reference_projection, reference_resource_identity

    visible = frozenset({reference_resource_identity(primary.plan.admissions[0].resource)})
    projection = await read_reference_projection(
        case.fixture.db_path,
        scope=case.scope,
        turn_id="t_reference",
        visible_resource_identities=visible,
        visibility_complete=True,
        include_content=True,
        cipher=runtime.cipher,
    )
    assert projection["delegated_payload"]["content"] == dispatched_task
    assert projection["delegated_payload"]["content_hash"] == delegation.task_hash
    assert projection["source_locator"]["text_spans"][0]["text"] == primary.source.input_document.text_content()
    hidden = await read_reference_projection(
        case.fixture.db_path,
        scope=case.scope,
        turn_id="t_reference",
        visible_resource_identities=frozenset(),
        visibility_complete=True,
        include_content=True,
        cipher=runtime.cipher,
    )
    assert hidden["delegated_payload"]["content"] is None
    assert hidden["delegated_payload"]["content_hash"] is None
    assert (await runtime.validate_lead()).binding.execution_id == primary.binding.execution_id
    # Output access does not reopen a terminal execution for another run.
    with pytest.raises((ValueError, RuntimeError)):
        await runtime.validate()
    # A further Lead dispatch continues on the same execution; it never adds
    # a second dispatch record or rewrites the first dispatch's task.
    await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus(), delegation_task=task, delegation_call_id="retry")
    assert len(_rows(case, "chat_reference_evidence")) == 2
    assert [row["call_id"] for row in _rows(case, "chat_reference_primary_continuations")] == ["retry"]
    assert _rows(case, "chat_reference_resolutions")[1] == actual
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"


async def test_primary_dispatch_keeps_current_request_above_stale_lead_task(lead_case):
    """The actual durable primary task must retain the current source request."""
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_delegation import open_delegation

    runtime = await _runtime(lead_case)
    primary = await runtime.validate()
    stale_task = "Read north.txt and south.txt from the previous turn instead."
    calls = []

    class CapturingRunner:
        async def run(self, **kwargs):
            calls.append(kwargs)
            return SubagentResult(
                profile=kwargs["profile_name"], subagent_id=kwargs["pre_minted_subagent_id"], ok=True, final_answer="Current result"
            )

    result = await runtime.run(
        runner=CapturingRunner(),
        parent_ctx=_ctx(lead_case),
        parent_bus=TurnEventBus(),
        delegation_task=stale_task,
        delegation_call_id="stale-task-dispatch",
    )
    assert result.ok and len(calls) == 1
    sent_task = calls[0]["task"]
    assert primary.source.input_document.text_content() in sent_task
    assert stale_task in sent_task
    assert "takes precedence" in sent_task
    async with open_shared_ctx(str(lead_case.fixture.db_path)) as db:
        await db.execute("BEGIN")
        delegation, saved_task, *_ = await open_delegation(db, runtime=runtime, primary=primary)
    assert saved_task == sent_task
    assert delegation.task_hash == "sha256:" + hashlib.sha256(sent_task.encode()).hexdigest()


async def test_empty_delegation_does_not_consume_primary_dispatch(lead_case):
    runtime = await _runtime(lead_case)
    runner = SubagentRunner(router=_router(LLMResponse(content="Unused")), tool_registry=build_default_registry())
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.run(
            runner=runner,
            parent_ctx=_ctx(lead_case),
            parent_bus=TurnEventBus(),
            delegation_task="  ",
            delegation_call_id="empty-task-dispatch",
        )
    assert _rows(lead_case, "turn_agent_execution")[0]["started_at"] is None
    assert len(_rows(lead_case, "chat_reference_resolutions")) == 1
    assert not _rows(lead_case, "chat_reference_evidence")


@pytest.mark.parametrize("kind", ["needs_user_input", "tool_failure"])
async def test_primary_error_envelope_is_never_recorded_as_completed_output(lead_case, kind):
    case = lead_case
    runtime = await _runtime(case)
    primary = await runtime.validate()
    subagent_id = "sub_" + hashlib.sha256(primary.binding.execution_id.encode()).hexdigest()[:20]

    class ErrorRunner:
        async def run(self, **_kwargs):
            return SubagentResult(
                profile=primary.binding.runtime_profile_name,
                subagent_id=subagent_id,
                ok=True,
                final_answer="",
                payload={"error": {"kind": kind, "message": "需要处理"}},
            )

    result = await runtime.run(
        runner=ErrorRunner(),
        parent_ctx=_ctx(case),
        parent_bus=TurnEventBus(),
        delegation_task="检查实际利润变化",
        delegation_call_id="call-primary",
    )
    assert not result.ok
    assert result.error == ("reference_continuation_unavailable" if kind == "needs_user_input" else "reference_primary_failed")
    execution_status = _rows(case, "turn_agent_execution")[0]["status"]
    output = [row for row in _rows(case, "chat_reference_evidence") if row["kind"] == "output_received"]
    if kind == "needs_user_input":
        assert execution_status == "running"  # The Turn finalizer owns unsupported-pause settlement.
        assert not output
    else:
        assert execution_status == "failed"
        assert len(output) == 1


async def test_predispatch_does_not_publish_a_second_card_over_corrupt_existing_context(lead_case):
    import sqlite3

    from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore

    case = lead_case
    store = ClarificationStore(case.fixture.db_path)
    await store.initialize()
    fields = {
        "turn_id": "t_reference",
        "user_id": case.scope.user_id,
        "workspace_id": case.scope.workspace_id,
        "session_id": case.scope.session_id,
        "token": "",
        "rationale": "选择期间",
        "candidates": [ClarificationCandidate(value="甲", label="甲", hint="", confidence="", kind="dim_value")],
        "kind": "ambiguous_turn",
    }
    old = await store.put(**fields)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE clarification_requests SET resume_context_json='{' WHERE request_id=?", (old.request_id,))
    runtime = await _runtime(case)
    with pytest.raises(ValueError, match="reference_continuation_already_started"):
        await runtime.put_lead_clarification(store=store, resume_context={}, **fields)
    assert len(_rows(case, "clarification_requests")) == 1
    assert _rows(case, "turn_agent_execution")[0]["status"] == "admitted"


async def test_lead_routing_input_accepts_vision_message(lead_case):
    from bowhead_router.types import ImageRef, LLMRequest, Message

    runtime = await _runtime(lead_case)
    policy = await runtime.lead_input()
    request = policy.attach(
        LLMRequest(model="large", messages=[Message(role="user", content="读图", images=[ImageRef(base64="UE5H", mime="image/png")])])
    )
    await policy.validate(request, provider="primary", model="large", context_window=100000)
    assert policy.budget(request, provider="primary", model="large", context_window=100000).estimate_contract == "utf8-bytes+image-reserve.v1"


async def test_reference_policy_change_is_a_public_access_failure(lead_case, monkeypatch):
    from bowhead_core.agents import lead_orchestrator
    from bowhead_core.agents.turn_context import SerializableTurnContext

    async def changed(*_args):
        raise ValueError("reference_policy_changed")

    monkeypatch.setattr(lead_orchestrator, "_lead_loop", changed)
    ctx = _ctx(lead_case)
    ctx._reference_runtime = object()
    bus = TurnEventBus()
    result = await lead_orchestrator.run_turn(
        SerializableTurnContext(**lead_case.scope.identity(), turn_id="t_reference", role="user"),
        "分析盈利能力",
        runtime=lead_orchestrator.LeadRuntime(router=None, tool_registry=None, parent_bus=bus, parent_ctx=ctx),
    )
    assert result.error == "reference_policy_changed"
    assert [
        (event.event_type, event.payload.get("reason_code", event.payload.get("reason")))
        for event in bus._events
        if event.event_type in {"turn_failed", "done"}
    ] == [
        ("turn_failed", "reference_policy_changed"),
        ("done", "reference_policy_changed"),
    ]
    assert bus._pi_turn_state["error_kind"] == "access"


@pytest.mark.parametrize("dispatch", [True, False])
async def test_normal_lead_tool_loop_runs_the_exact_selected_primary(lead_case, monkeypatch, dispatch):
    await _normal_lead(lead_case, monkeypatch, dispatch)


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize(
    ("request_text", "answer"),
    [
        ("Revenue 800, profit 120. Return only three lines.", "Revenue: 800\nProfit: 120\nMargin: 15.0%"),
        ("Revenue 200, profit -30. Return only three lines.", "Revenue: 200\nProfit: -30\nMargin: -15.0%"),
        ("Revenue 0, profit 12. Return only three lines.", "Revenue: 0\nProfit: 12\nMargin: undefined (zero revenue)"),
        ("Revenue 200. Ask only for missing profit.", "Please provide profit."),
    ],
    ids=["normal", "negative-profit", "zero-revenue", "missing-profit"],
)
async def test_selected_team_answer_preserves_live_persisted_and_replayed_text(start_case, monkeypatch, request_text, answer):
    case = await _prepare_case(start_case)
    source = case.args["reference_start"]
    source.input_document.nodes[1] = source.input_document.nodes[1].model_copy(update={"text": request_text})
    case.args["reference_start"] = replace(source, orchestration_contract="lead-delegation.v1")
    case.args["user_text"] = source.input_document.display_text()
    await case.store.finalize_started_chat_submission(**case.args)
    await _normal_lead(case, monkeypatch, primary_answer=answer, lead_predispatch_text="Here is my answer before the selected Agent runs.")


async def test_nonstream_lead_provider_failure_records_one_terminal_event(lead_case, monkeypatch):
    await _normal_lead(lead_case, monkeypatch, lead_provider_error=True)


async def test_reference_lead_rejects_non_tool_clarification_sentinel_without_card(lead_case, monkeypatch):
    await _normal_lead(lead_case, monkeypatch, clarification_sentinel=True)


async def test_reference_lead_ask_tool_persists_predispatch_card(lead_case, monkeypatch):
    await _normal_lead(lead_case, monkeypatch, clarification_tool=True)


async def test_primary_question_without_card_store_fails_without_extra_lead_retry(lead_case, monkeypatch):
    await _normal_lead(lead_case, monkeypatch, primary_needs_input=True)


async def test_effect_free_primary_question_persists_same_turn_card(lead_case, monkeypatch):
    case = lead_case
    case.runtime = await _runtime(case)
    await _normal_lead(case, monkeypatch, primary_needs_input=True, provide_card_store=True)


@pytest.mark.parametrize("resume_fails", [False, True])
@pytest.mark.parametrize("legacy_delegation", [False, True])
async def test_effect_free_primary_answer_resumes_original_execution(lead_case, monkeypatch, resume_fails, legacy_delegation):
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.clarification_store import ClarificationStore

    case = lead_case
    original = await _runtime(case)
    case.runtime = original
    with monkeypatch.context() as initial:
        if legacy_delegation:
            initial.setattr("bowhead_core.chat_reference_delegation.render_primary_delegation_task", lambda _original, task: task)
        await _normal_lead(case, initial, primary_needs_input=True, provide_card_store=True)
    card = _rows(case, "clarification_requests")[0]
    store = ClarificationStore(case.fixture.db_path)
    assert (await store.resolve(card["request_id"], "甲")).status == "resolved"
    first_usage = (await case.store.get_turn("t_reference")).tokens_input
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.is_turn_resume_ready("t_reference")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    resumed = ReferencePrimaryRuntime(
        scope=case.scope,
        turn_id="t_reference",
        owner_id="worker-resume",
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        cipher=original.cipher,
        generation=2,
    )
    await resumed.claim_bi_resume(clarification_id=card["request_id"])
    assert await resumed.is_postdispatch_resume()
    case.runtime = resumed
    requests = []
    await _normal_lead(case, monkeypatch, postdispatch_resume=True, postdispatch_resume_failure=resume_fails, captured_requests=requests)
    task_messages = [m["content"] for m in requests[0]["messages"] if isinstance(m.get("content"), str)]
    assert any("检查实际利润变化" in text for text in task_messages)
    assert any("Execute the current user request" in text for text in task_messages) is not legacy_delegation
    assert (await case.store.get_turn("t_reference")).tokens_input > first_usage
    assert _rows(case, "turn_agent_execution")[0]["status"] == ("failed" if resume_fails else "completed")
    assert len(_rows(case, "chat_reference_resolutions")) == 2
    assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]


@pytest.mark.parametrize("change", [None, "replace_text", "replace_image", "grow_image", "revoke"])
async def test_postdispatch_answer_keeps_original_text_and_image(start_case, monkeypatch, change):
    import sqlite3
    from datetime import UTC, datetime
    from pathlib import Path

    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.attachments_injector import prepare_attachments
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.clarification_store import ClarificationStore

    case = await _prepare_case(start_case)
    artifacts = ArtifactStore(str(case.fixture.db_path), LocalBackend(case.fixture.db_path.parent / "postdispatch-files"))
    await artifacts.initialize()
    source_text = b"Quarterly profit: 42"
    source_image = b"original-image-bytes"
    attached = [
        await artifacts.put_bytes(
            content=source_text, filename="profit.txt", mime="text/plain", created_by=case.scope.user_id, workspace_id=case.scope.workspace_id
        ),
        await artifacts.put_bytes(
            content=source_image, filename="profit.png", mime="image/png", created_by=case.scope.user_id, workspace_id=case.scope.workspace_id
        ),
    ]
    with sqlite3.connect(case.fixture.db_path) as db:
        db.executemany(
            "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,created_at) VALUES (?,?,?,?)",
            [(art.id, case.scope.workspace_id, case.scope.user_id, datetime.now(UTC).isoformat()) for art in attached],
        )
    case.args["attachment_ids"] = [art.id for art in attached]
    case.args["attachment_refs"] = [
        {"fileId": art.id, "filename": art.filename, "mime": art.mime, "size": art.size, "referenceKind": "upload"} for art in attached
    ]
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1", has_attachments=True)
    await case.store.finalize_started_chat_submission(**case.args)
    original = await _runtime(case)
    case.runtime = original
    prepared = await prepare_attachments(store=artifacts, artifact_ids=case.args["attachment_ids"])
    prepared[0].local_path = "/worker-a/temporary/profit.txt"
    first_requests = []
    await _normal_lead(
        case,
        monkeypatch,
        primary_needs_input=True,
        provide_card_store=True,
        artifact_store=artifacts,
        prepared_attachments=prepared,
        captured_requests=first_requests,
    )
    first_wire = json.dumps(first_requests, ensure_ascii=False)
    assert "Quarterly profit: 42" in first_wire
    assert "b3JpZ2luYWwtaW1hZ2UtYnl0ZXM=" in first_wire
    assert "/worker-a/temporary/profit.txt" not in json.dumps(first_requests[1], ensure_ascii=False)
    card = _rows(case, "clarification_requests")[0]
    assert (await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")).status == "resolved"
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    resumed = ReferencePrimaryRuntime(
        scope=case.scope,
        turn_id="t_reference",
        owner_id="worker-resume",
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        cipher=original.cipher,
        generation=2,
    )
    await resumed.claim_bi_resume(clarification_id=card["request_id"])
    if change == "replace_text":
        Path(attached[0].path).write_bytes(b"Quarterly profit: 43")
    elif change == "replace_image":
        Path(attached[1].path).write_bytes(b"replaced-image-bytes")
    elif change == "grow_image":
        Path(attached[1].path).write_bytes(source_image + b"unexpected-extra-byte")
    elif change == "revoke":
        with sqlite3.connect(case.fixture.db_path) as db:
            db.execute("UPDATE artifacts SET deleted_at=? WHERE id=?", (datetime.now(UTC).isoformat(), attached[0].id))
    case.runtime = resumed
    if change is None:
        continued_requests = []
        await _normal_lead(case, monkeypatch, postdispatch_resume=True, artifact_store=artifacts, captured_requests=continued_requests)
        continued_wire = json.dumps(continued_requests, ensure_ascii=False)
        assert "Quarterly profit: 42" in continued_wire
        assert "b3JpZ2luYWwtaW1hZ2UtYnl0ZXM=" in continued_wire
        assert "/worker-a/temporary/profit.txt" not in continued_wire
        assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    else:

        class NoRun:
            async def run(self, **_kwargs):
                raise AssertionError("changed attachment reached the model")

        ctx = _ctx(case)
        ctx.artifact_store = artifacts
        with pytest.raises(TerminalDurabilityAbort):
            await resumed.run(runner=NoRun(), parent_ctx=ctx, parent_bus=TurnEventBus(), postdispatch_resume=True)
        assert not _rows(case, "chat_reference_lead_postdispatch_attempts")


async def test_postdispatch_model_start_blocks_cold_retry(lead_case, monkeypatch):
    import sqlite3

    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.clarification_store import ClarificationStore

    case = lead_case
    original = await _runtime(case)
    case.runtime = original
    await _normal_lead(case, monkeypatch, primary_needs_input=True, provide_card_store=True)
    card = _rows(case, "clarification_requests")[0]
    assert (await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")).status == "resolved"
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    resumed = ReferencePrimaryRuntime(
        scope=case.scope,
        turn_id="t_reference",
        owner_id="worker-resume",
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        cipher=original.cipher,
        generation=2,
    )
    await resumed.claim_bi_resume(clarification_id=card["request_id"])

    class LostRunner:
        async def run(self, **_kwargs):
            raise RuntimeError("physical model outcome unknown")

    with pytest.raises(RuntimeError, match="physical model outcome unknown"):
        await resumed.run(runner=LostRunner(), parent_ctx=_ctx(case), parent_bus=TurnEventBus(), postdispatch_resume=True)
    assert len(_rows(case, "chat_reference_lead_postdispatch_attempts")) == 1
    assert not await case.store.rollback_turn_resume_generation(turn_id="t_reference", expected_execution_generation=2, owner_id="worker-resume")
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE turn_id='t_reference'")
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert (await case.store.get_turn("t_reference")).status == "failed"
    assert _rows(case, "turn_agent_execution")[0]["status"] == "failed"
    assert len(_rows(case, "chat_reference_lead_postdispatch_attempts")) == 1
    assert await case.store.recover_expired_chat_spawn_dispatches() == []


async def test_gateway_resume_reader_accepts_postdispatch_card(lead_case, monkeypatch):
    from types import SimpleNamespace

    from bowhead_core.chat_reference_scope import ReferenceRequestScope
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_core.skill_read_resolution import build_reference_skill_sources
    from bowhead_gateway.turn_service import _prepare_reference_bi_resume, build_resume_turn_request
    from cryptography.fernet import Fernet

    case = lead_case
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", key)
    case.runtime = replace(await _runtime(case), cipher=ReferenceCipher(key))
    await _normal_lead(case, monkeypatch, primary_needs_input=True, provide_card_store=True)
    card = _rows(case, "clarification_requests")[0]
    resolved = await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")
    assert resolved.status == "resolved"
    resume_request = build_resume_turn_request(resolved)
    assert resume_request is not None
    assert resume_request.reference_clarification_id == card["request_id"]
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    request = SimpleNamespace(
        **case.scope.identity(),
        turn_id="t_reference",
        reference_clarification_id=card["request_id"],
        active_datarc_project=None,
        accessible_datarc_workspaces=(),
        text="stale client text",
        attachments=[],
    )
    orchestrator = SimpleNamespace(
        session_store=case.store,
        reference_skill_sources_factory=lambda scope: build_reference_skill_sources(
            case.fixture.db_path, scope=ReferenceRequestScope.model_validate(scope), builtin_index=lambda: None
        ),
    )
    inputs = await _prepare_reference_bi_resume(orchestrator, request)
    assert inputs is not None
    assert request.text == case.args["user_text"]
    assert request.attachments == []
    assert request.reference_scope == case.scope


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_gateway_resumes_postdispatch_question_on_original_primary(receiver_case, monkeypatch):
    import asyncio

    import httpx2
    import respx
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.orchestrator import Orchestrator
    from bowhead_core.skill_read_resolution import build_reference_skill_sources
    from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from bowhead_gateway.turn_service import build_resume_turn_request, spawn_resume_turn
    from cryptography.fernet import Fernet
    from test_skill_model_invocation import adapter_for, router_for
    from test_skill_start_admission import candidate

    from bowhead_router import AgentBinding

    fixture = receiver_case
    await candidate(fixture, agent_first=True)
    case = fixture.case
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1")
    await case.store.finalize_started_chat_submission(**case.args)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", key)
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        scope=fixture.scope.model_copy(update={"session_id": "s_reference"}),
        turn_id="t_reference",
        cipher=ReferenceCipher(key),
        expected_generation=1,
        owner_id="worker-a",
    )
    case.runtime = ReferencePrimaryRuntime(
        scope=case.scope,
        turn_id="t_reference",
        owner_id="worker-a",
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        cipher=ReferenceCipher(key),
        skill_sources=fixture.sources,
    )
    await _normal_lead(case, monkeypatch, primary_needs_input=True, provide_card_store=True)
    card = _rows(case, "clarification_requests")[0]
    resolved = await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")
    assert resolved.status == "resolved"
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")

    adapter = adapter_for("openai")
    router = router_for(adapter)
    router.bind(AgentBinding(agent_type="compute", primary=("primary", "large")))
    registry = build_default_registry()
    register_subagent_return_tool(registry)
    events = EventBusRegistry(default_log_sink=OrderedTurnLogSink(TurnLogStore(case.fixture.db_path)))
    orchestrator = Orchestrator(
        router=router,
        datarc=None,
        session_store=case.store,
        tool_registry=registry,
        event_registry=events,
        user_store=case.fixture.users,
        clarification_store=ClarificationStore(case.fixture.db_path),
    )
    orchestrator.dispatch_owner_id = "mixed-worker"
    orchestrator.subagent_runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    orchestrator.reference_skill_sources_factory = lambda scope: build_reference_skill_sources(
        case.fixture.db_path, scope=scope, builtin_index=fixture.sources.builtin_index, builtin_root=fixture.root
    )
    request = build_resume_turn_request(resolved, team_router=router)
    assert request is not None and request.reference_clarification_id == card["request_id"]
    request.accessible_datarc_workspaces = ()
    request.datarc_client = object()
    calls = []

    def respond(call):
        calls.append(json.loads(call.content))
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "甲期利润结构分析结果。"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    from bowhead_gateway.routes.chat import _inflight_tasks

    try:
        async with open_shared_ctx(case.fixture.db_path) as db:
            await db.execute("BEGIN")
            last_seq = (await db.execute_fetchall("SELECT COALESCE(MAX(seq),0) FROM turn_events WHERE turn_id='t_reference'"))[0][0]
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            spawned = await spawn_resume_turn(
                orchestrator,
                request,
                team_id=case.scope.team_id,
                initial_seq=last_seq,
                resume_notice={"clarification_id": card["request_id"]},
            )
            assert spawned.execution_generation == 2 and spawned.task is not None
            monitor = next(task for task in _inflight_tasks if task.get_name() == "turn_dispatch_lease:t_reference")
            await asyncio.wait_for(asyncio.gather(spawned.task, monitor), timeout=30)
        assert len(calls) == 1
        assert "chosen_value" in json.dumps(calls[0], ensure_ascii=False)
        turn = await case.store.get_turn("t_reference")
        assert turn.status == "completed" and "甲期" in turn.final_answer
        assert len(_rows(case, "chat_reference_resolutions")) == 2
        assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]
    finally:
        await adapter.close()


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("reads_before_question,prose_before_structured", [(False, False), (True, False), (True, True)])
async def test_postdispatch_answer_keeps_original_skill_on_primary(receiver_case, monkeypatch, reads_before_question, prose_before_structured):
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
    from cryptography.fernet import Fernet
    from test_skill_start_admission import candidate

    fixture = receiver_case
    await candidate(fixture, agent_first=True)
    case = fixture.case
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1")
    await case.store.finalize_started_chat_submission(**case.args)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        scope=fixture.scope.model_copy(update={"session_id": "s_reference"}),
        turn_id="t_reference",
        cipher=cipher,
        expected_generation=1,
        owner_id="worker-a",
    )
    case.runtime = ReferencePrimaryRuntime(
        scope=case.scope, turn_id="t_reference", owner_id="worker-a", bindings=case.bindings, cipher=cipher, skill_sources=fixture.sources
    )
    initial_requests = []
    await _normal_lead(
        case,
        monkeypatch,
        primary_needs_input=True,
        primary_reads_before_question=reads_before_question,
        primary_prose_before_structured=prose_before_structured,
        provide_card_store=True,
        free_text_question=True,
        primary_language="zh-CN" if reads_before_question else None,
        captured_requests=initial_requests,
    )
    card = _rows(case, "clarification_requests")[0]
    request_text = "\n".join(message.get("content") or "" for message in initial_requests[-1]["messages"])
    assert '"question"' in request_text
    assert '"type":"free_text"' in request_text
    assert '"options":[]' in request_text
    assert ("reference_read_question" in json.loads(card["resume_context_json"])) is reads_before_question
    if reads_before_question:
        context = json.loads(card["resume_context_json"])
        assert (
            context["reference_continuation"]["read_checkpoint_hash"]
            == "sha256:" + hashlib.sha256(context["reference_read_question"].encode()).hexdigest()
        )
        assert case.read_method_calls == 1
    if prose_before_structured:
        assert "Do not ask for missing input in final prose" in json.dumps(initial_requests[-1], ensure_ascii=False)
    assert json.loads(card["resume_context_json"])["reference_questions"] == [
        {"question": "平均所有者权益是多少？", "options": [], "type": "free_text"}
    ]
    assert (await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "50")).status == "resolved"
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    resumed = ReferencePrimaryRuntime(
        scope=case.scope,
        turn_id="t_reference",
        owner_id="worker-resume",
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        cipher=cipher,
        generation=2,
        skill_sources=fixture.sources,
    )
    await resumed.claim_bi_resume(clarification_id=card["request_id"])
    case.runtime = resumed
    await _normal_lead(
        case,
        monkeypatch,
        postdispatch_resume=True,
        primary_reads_before_question=reads_before_question,
        primary_language="zh-CN" if reads_before_question else None,
    )
    if reads_before_question:
        assert case.read_method_calls == 1
        assert "reference_read_question" not in json.loads(_rows(case, "clarification_requests")[0]["resume_context_json"])
    assert sorted(row["created_generation"] for row in _rows(case, "chat_reference_model_invocations")) == (
        [1, 1, 1, 2] if prose_before_structured else [1, 1, 2] if reads_before_question else [1, 2]
    )
    assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]


@pytest.mark.parametrize("free_text_question", [False, True])
async def test_reference_lead_answer_resumes_same_turn_and_dispatches_once(lead_case, monkeypatch, free_text_question):
    from bowhead_core.clarification_store import ClarificationStore

    case = lead_case
    initial_requests = []
    await _normal_lead(
        case,
        monkeypatch,
        clarification_tool=True,
        lead_free_text_question=free_text_question,
        captured_requests=initial_requests,
    )
    card = _rows(case, "clarification_requests")[0]
    store = ClarificationStore(case.fixture.db_path)
    answer_value = "50" if free_text_question else "甲"
    if free_text_question:
        assert "clarification_ask" in json.dumps(initial_requests[0], ensure_ascii=False)
        assert "free_text question with options=[]" in json.dumps(initial_requests[0], ensure_ascii=False)
        assert json.loads(card["resume_context_json"])["reference_questions"] == [
            {"question": "平均所有者权益是多少？", "options": [], "type": "free_text"}
        ]
    resolved = await store.resolve(card["request_id"], answer_value)
    assert resolved is not None and resolved.status == "resolved"
    first_usage = (await case.store.get_turn("t_reference")).tokens_input
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.is_turn_resume_ready("t_reference")
    generation = await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"])
    assert generation == 2
    initial = await _runtime_for_resume(case)
    await initial.claim_bi_resume(clarification_id=card["request_id"])
    assert await initial.reprepare_original_attachments_on_resume([])
    with pytest.raises(ValueError, match="reference_request_changed"):
        await initial.reprepare_original_attachments_on_resume(["unselected-image"])
    case.runtime = initial
    await _normal_lead(case, monkeypatch, expected_answer=answer_value)
    assert (await case.store.get_turn("t_reference")).tokens_input > first_usage
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    assert len(_rows(case, "chat_reference_resolutions")) == 2
    assert len([row for row in _rows(case, "chat_reference_evidence") if row["kind"] == "delegate_started"]) == 1


async def test_predispatch_answer_reopens_original_image_for_same_primary(start_case, monkeypatch):
    import sqlite3
    from datetime import UTC, datetime

    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.attachments_injector import prepare_attachments
    from bowhead_core.chat_reference_runtime import _attachment_context
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_core.turn_runner import _reference_resume_attachment_snapshot

    case = await _prepare_case(start_case)
    artifacts = ArtifactStore(str(case.fixture.db_path), LocalBackend(case.fixture.db_path.parent / "reference-images"))
    await artifacts.initialize()
    image = await artifacts.put_bytes(
        content=b"original-image-bytes",
        filename="profit.png",
        mime="image/png",
        created_by=case.scope.user_id,
        workspace_id=case.scope.workspace_id,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "INSERT INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,created_at) VALUES (?,?,?,?)",
            (image.id, case.scope.workspace_id, case.scope.user_id, datetime.now(UTC).isoformat()),
        )
    case.args["attachment_ids"] = [image.id]
    case.args["attachment_refs"] = [
        {"fileId": image.id, "filename": image.filename, "mime": image.mime, "size": image.size, "referenceKind": "upload"}
    ]
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1", has_attachments=True)
    await case.store.finalize_started_chat_submission(**case.args)

    await _normal_lead(case, monkeypatch, clarification_tool=True)
    card = _rows(case, "clarification_requests")[0]
    await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    resumed = await _runtime_for_resume(case)
    await resumed.claim_bi_resume(clarification_id=card["request_id"])
    snapshot = await _reference_resume_attachment_snapshot(
        resumed,
        [image.id],
        [
            {"artifact_id": "another-session-image", "filename": "private.png", "mime": "image/png", "size": 10},
            {"artifact_id": image.id, "filename": image.filename, "mime": image.mime, "size": image.size},
        ],
    )
    assert snapshot == [{"artifact_id": image.id, "filename": image.filename, "mime": image.mime, "size": image.size}]
    prepared = await prepare_attachments(store=artifacts, artifact_ids=[item["artifact_id"] for item in snapshot])
    ctx = _ctx(case)
    ctx.artifact_store = artifacts
    ctx._psi_parsed_results = prepared
    original = await resumed.validate()
    content, images = await _attachment_context(original, ctx)
    assert len(images) == 1 and images[0].base64 == "b3JpZ2luYWwtaW1hZ2UtYnl0ZXM="
    assert image.id in content

    class ImageRunner:
        async def run(self, **kwargs):
            assert kwargs["message_images"] == images
            assert kwargs["task"] == render_primary_delegation_task(original.source.input_document.text_content(), "检查实际利润变化")
            return SubagentResult(
                profile=original.binding.runtime_profile_name,
                subagent_id=kwargs["pre_minted_subagent_id"],
                ok=True,
                final_answer="按原图完成分析。",
            )

    result = await resumed.run(
        runner=ImageRunner(),
        parent_ctx=ctx,
        parent_bus=TurnEventBus(),
        delegation_task="检查实际利润变化",
        delegation_call_id="dispatch-primary",
    )
    assert result.ok
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"


@pytest.mark.parametrize("bounded", [False, True])
async def test_predispatch_resume_lease_rolls_back_without_consuming_answer(lead_case, monkeypatch, bounded):
    from bowhead_core.clarification_store import ClarificationStore

    case = lead_case
    await _normal_lead(case, monkeypatch, clarification_tool=True)
    card = _rows(case, "clarification_requests")[0]
    await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) is None
    assert await case.store.rollback_turn_resume_generation(turn_id="t_reference", expected_execution_generation=2, owner_id=None)
    assert await case.store.is_turn_resume_ready("t_reference")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 3
    assert _rows(case, "turn_agent_execution")[0]["status"] == "admitted"
    assert not _rows(case, "chat_reference_evidence")


async def test_predispatch_answer_does_not_bypass_revoked_receiver(lead_case, monkeypatch):
    import sqlite3

    from bowhead_core.clarification_store import ClarificationStore

    case = lead_case
    await _normal_lead(case, monkeypatch, clarification_tool=True)
    card = _rows(case, "clarification_requests")[0]
    await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE agent_assignment SET access_state='revoked'")
    resumed = await _runtime_for_resume(case)
    from bowhead_core.agent_execution_binding import AgentExecutionBindingError

    with pytest.raises((ValueError, AgentExecutionBindingError)):
        await resumed.claim_bi_resume(clarification_id=card["request_id"])
    assert await case.store.rollback_turn_resume_generation(turn_id="t_reference", expected_execution_generation=2, owner_id=None)
    assert _rows(case, "turn_agent_execution")[0]["status"] == "admitted"
    assert not _rows(case, "chat_reference_evidence")


async def _runtime_for_resume(case):
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from cryptography.fernet import Fernet

    return ReferencePrimaryRuntime(
        scope=case.scope,
        turn_id="t_reference",
        owner_id="worker-resume",
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        cipher=ReferenceCipher(Fernet.generate_key().decode()),
        generation=2,
    )


async def _normal_lead(
    case,
    monkeypatch,
    dispatch=True,
    role="primary",
    clarification_sentinel=False,
    clarification_tool=False,
    lead_free_text_question=False,
    primary_needs_input=False,
    primary_reads_before_question=False,
    primary_language=None,
    primary_prose_before_structured=False,
    free_text_question=False,
    expected_answer="甲",
    provide_card_store=False,
    postdispatch_resume=False,
    postdispatch_resume_failure=False,
    artifact_store=None,
    prepared_attachments=None,
    captured_requests=None,
    lead_provider_error=False,
    lead_question_text=None,
    expected_question_rounds=1,
    expected_pause_count=None,
    primary_answer=None,
    lead_predispatch_text="",
):
    import httpx2
    import respx
    from bowhead_core.agents.clarification_tool import register_clarification_tool
    from bowhead_core.agents.lead_orchestrator import LeadRuntime, run_turn
    from bowhead_core.agents.turn_context import SerializableTurnContext
    from bowhead_core.subagent import PROFILES, SubagentProfile, register_subagent_tool
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.tool_registry import ToolSpec
    from bowhead_router.types import ToolSchema
    from test_skill_model_invocation import adapter_for, router_for

    from bowhead_router import AgentBinding

    runtime = getattr(case, "runtime", None) or await _runtime(case)
    primary = await runtime.validate()
    ctx = _ctx(case)
    if artifact_store is not None:
        ctx.artifact_store = artifact_store
    if prepared_attachments is not None:
        ctx._psi_parsed_results = prepared_attachments
    ctx.session_store = case.store
    ctx._reference_runtime = runtime
    clarification_store = None
    if clarification_tool or provide_card_store:
        from bowhead_core.clarification_store import ClarificationStore

        clarification_store = ClarificationStore(case.fixture.db_path)
        await clarification_store.initialize()
        ctx.clarification_store = clarification_store
    monkeypatch.setenv("BOWHEAD_STREAMING_LEAD", "0" if lead_provider_error else "1")
    monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "0")
    monkeypatch.setenv("BOWHEAD_LEAD_FINAL_ANSWER_GATE", "0")
    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            tool_whitelist=(
                ("subagent_spawn", "probe_workspace")
                if clarification_sentinel
                else ("subagent_spawn", "clarification_ask")
                if clarification_tool
                else ("subagent_spawn",)
            ),
            system_prompt="Delegate then summarize.",
            max_iterations=4,
        ),
    )

    adapter = adapter_for("openai")
    router = router_for(adapter)
    for agent_type in ("lead", "compute"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("primary", "large")))
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if lead_provider_error:
            return httpx2.Response(400, json={"error": {"message": "invalid model request", "type": "invalid_request_error"}})
        if captured_requests is not None:
            captured_requests.append(body)
        if postdispatch_resume:
            assert len(requests) == 1
            assert "chosen_value" in json.dumps(body, ensure_ascii=False)
            if primary_reads_before_question:
                wire = json.dumps(body, ensure_ascii=False)
                assert "DuPont checkpoint material" in wire
                assert '"name": "skill_read"' in wire
            message = {"role": "assistant", "content": "" if postdispatch_resume_failure else "甲期利润结构分析结果。"}
            reason = "stop"
            usage = {"prompt_tokens": 11, "completion_tokens": 7}
            if not body.get("stream"):
                return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})
            events = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage}]
            return httpx2.Response(
                200,
                text="".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        if runtime.generation > 1 and len(requests) == 1:
            wire = json.dumps(body, ensure_ascii=False)
            assert "chosen_value" in wire and expected_answer in wire
        if runtime.skill_sources is not None:
            wire = json.dumps(body, ensure_ascii=False)
            primary_request = len(requests) == 2 or (
                primary_reads_before_question and len(requests) in ({3, 4} if primary_prose_before_structured else {3})
            )
            assert ("BODY_method" in wire) == primary_request
            assert ("BODY_support" in wire) == primary_request
        primary_request_number = 3 if clarification_tool else 2
        message = {
            "role": "assistant",
            "content": (primary_answer or "选定分析师给出的结果。")
            if len(requests) == primary_request_number
            else "结合分析师结果，建议检查利润结构。",
        }
        reason = "stop"
        if (len(requests) == 1 and dispatch) or (clarification_tool and len(requests) == 2):
            reason = "tool_calls"
            asking = len(requests) == 1 and (clarification_sentinel or clarification_tool)
            message = {
                "role": "assistant",
                "content": lead_predispatch_text,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "dispatch-primary",
                        "type": "function",
                        "function": {
                            "name": "probe_workspace" if clarification_sentinel else "clarification_ask" if asking else "subagent_spawn",
                            "arguments": json.dumps(
                                {}
                                if clarification_sentinel
                                else {"questions": [{"question": lead_question_text or "平均所有者权益是多少？", "options": [], "type": "free_text"}]}
                                if asking and lead_free_text_question
                                else {"candidates": ["甲", "乙"], "fallback_text": "Select the analysis period."}
                                if asking
                                else {
                                    "profile": primary.binding.runtime_profile_name,
                                    "task": "检查实际利润变化",
                                    "delegation_role": role,
                                    **({"language": primary_language} if primary_language else {}),
                                }
                            ),
                        },
                    }
                ],
            }
        elif primary_reads_before_question and len(requests) == 2:
            reason = "tool_calls"
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "read-primary",
                        "type": "function",
                        "function": {"name": "skill_read", "arguments": '{"name":"method"}'},
                    }
                ],
            }
        elif primary_prose_before_structured and primary_reads_before_question and len(requests) == 3:
            message = {"role": "assistant", "content": "缺少平均所有者权益，请补充后继续计算。"}
        elif primary_needs_input and len(requests) == (
            4 if primary_reads_before_question and primary_prose_before_structured else 3 if primary_reads_before_question else 2
        ):
            reason = "tool_calls"
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "ask-primary",
                        "type": "function",
                        "function": {
                            "name": "subagent_return",
                            "arguments": json.dumps(
                                {
                                    "payload": {
                                        "error": {
                                            "kind": "needs_user_input",
                                            "message": "请确认分析期间",
                                            "clarification_payload": {
                                                "questions": (
                                                    [{"question": "平均所有者权益是多少？", "options": [], "type": "free_text"}]
                                                    if free_text_question
                                                    else [{"question": "分析哪个期间？", "options": ["甲", "乙"], "type": "single_select"}]
                                                )
                                            },
                                        }
                                    }
                                }
                            ),
                        },
                    }
                ],
            }
        usage = {"prompt_tokens": 11, "completion_tokens": 7}
        if not body.get("stream"):
            return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})
        events = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage}]
        return httpx2.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    registry = build_default_registry()
    if primary_reads_before_question:

        async def read_method(_args, _ctx):
            case.read_method_calls = getattr(case, "read_method_calls", 0) + 1
            return {"name": "method", "body": "DuPont checkpoint material"}

        registry.register(
            ToolSpec(schema=ToolSchema(name="skill_read", description="read selected method", input_schema={"type": "object"}), handler=read_method)
        )
    register_subagent_tool(registry)
    register_subagent_return_tool(registry)
    if clarification_tool:
        register_clarification_tool(registry)
    if clarification_sentinel:

        async def _probe_workspace(_args, _ctx):
            return {
                "ok": False,
                "error_kind": "needs_clarification",
                "clarification": {
                    "kind": "tool_context_select",
                    "tool": "datarc",
                    "prompt": "选择工作区",
                    "options": [{"value": "ws_a", "label": "甲"}, {"value": "ws_b", "label": "乙"}],
                },
            }

        registry.register(
            ToolSpec(
                schema=ToolSchema(name="probe_workspace", description="ask for workspace", input_schema={"type": "object"}), handler=_probe_workspace
            )
        )
    runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    log = TurnLogStore(case.fixture.db_path)
    bus = TurnEventBus(log_sink=OrderedTurnLogSink(log), persist_before_live=True)
    if runtime.generation > 1:
        from bowhead_core.events import OrchestratorEvent

        await bus.emit(OrchestratorEvent(event_type="turn.resumed", turn_id="t_reference", payload={"execution_generation": runtime.generation}))
    ctx.subagent_runner = runner
    ctx.parent_bus = bus
    try:
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await run_turn(
                SerializableTurnContext(**case.scope.identity(), turn_id="t_reference", role="user"),
                primary.source.user_text,
                runtime=LeadRuntime(
                    router=router,
                    tool_registry=registry,
                    parent_bus=bus,
                    parent_ctx=ctx,
                    subagent_runner=runner,
                    session_store=case.store,
                    persist_turn_state=partial(runtime.persist_turn_state, store=case.store),
                ),
            )
    finally:
        await adapter.close()
    if lead_provider_error:
        assert result.status == "failed"
        assert len(await log.list_events("t_reference", event_types=("turn_failed",))) == 1
        assert len(await log.list_events("t_reference", event_types=("done",))) == 1
        return
    if clarification_tool:
        import aiosqlite
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.chat_reference_lead_continuation import pause_ready

        cards = _rows(case, "clarification_requests")
        assert len(cards) == expected_question_rounds
        assert json.loads(cards[0]["resume_context_json"])["reference_continuation"]["contract"] == "reference-lead-predispatch.v1"
        assert len(await log.list_events("t_reference", event_types=("clarification_required",))) == expected_question_rounds
        async with open_shared_ctx(case.fixture.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            assert await pause_ready(db, turn_id="t_reference")
        assert result.status == "clarification_pending"
        assert _rows(case, "turn_agent_execution")[0]["status"] == "admitted"
        return
    if primary_needs_input:
        if provide_card_store:
            cards = _rows(case, "clarification_requests")
            assert result.status == "clarification_pending"
            assert len(cards) == 1
            assert json.loads(cards[0]["resume_context_json"])["reference_continuation"]["contract"] == "reference-lead-postdispatch.v1"
            assert _rows(case, "turn_agent_execution")[0]["status"] == "running"
            assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started"]
            if primary_reads_before_question:
                assert "reference_read_question" in json.loads(cards[0]["resume_context_json"])
            assert len(await log.list_events("t_reference", event_types=("clarification_required",))) == 1
            return
        assert result.status == "failed"
        assert result.error == "reference_continuation_unavailable"
        # Chat turn feedback D3: the failure card states this; no system answer prose.
        assert result.final_answer == ""
        assert await log.list_events("t_reference", event_types=("final_answer",)) == []
        assert len(requests) == 2  # The question cannot prompt another Lead model call.
        assert not _rows(case, "clarification_requests")
        assert _rows(case, "turn_agent_execution")[0]["status"] == "failed"
        assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started"]
        assert await log.list_events("t_reference", event_types=("clarification_required",)) == []
        failed = await log.list_events("t_reference", event_types=("turn_failed",))
        done = await log.list_events("t_reference", event_types=("done",))
        assert len(failed) == len(done) == 1
        assert failed[0].payload["reason"] == "reference_continuation_unavailable"
        assert done[0].payload["status"] == "error"
        assert failed[0].payload["recovery_action"] == done[0].payload["recovery_action"] == "edit_and_resend"
        return
    if postdispatch_resume:
        assert result.status == ("failed" if postdispatch_resume_failure else "completed")
        if not postdispatch_resume_failure:
            assert "甲期" in result.final_answer
        assert len(requests) == 1
        return
    if not dispatch or role == "helper" or clarification_sentinel:
        assert result.status == "failed"
        if clarification_sentinel:
            assert result.error == "reference_continuation_unavailable"
            assert result.final_answer == ""
        else:
            assert not result.final_answer
        assert not _rows(case, "chat_reference_evidence")
        assert len(_rows(case, "chat_reference_resolutions")) == 1
        if clarification_sentinel:
            assert await log.list_events("t_reference", event_types=("clarification_required",)) == []
        return
    assert result.status == "completed"
    if primary_answer is not None:
        assert result.final_answer == primary_answer
        assert len(requests) == 2
        stored = await case.store.get_turn("t_reference")
        assert stored is not None and stored.final_answer == primary_answer
        live = [event for event in bus._events if event.parent_agent_id is None]
        replay = await log.list_events("t_reference", event_types=("narrative.delta", "final_answer"))
        for events in (live, replay):
            narrative = "".join(
                event.payload["delta"] for event in events if event.event_type == "narrative.delta" and event.payload.get("parent_agent_id") is None
            )
            assert narrative == primary_answer
            assert [
                event.payload["text"] for event in events if event.event_type == "final_answer" and event.payload.get("parent_agent_id") is None
            ] == [primary_answer]
    elif primary.binding.agent_source_kind == "team":
        assert result.final_answer == "选定分析师给出的结果。"
    else:
        assert "利润结构" in result.final_answer
    done = await log.list_events("t_reference", event_types=("done",))
    pause_count = runtime.generation - 1 if expected_pause_count is None else expected_pause_count
    assert [item.payload["status"] for item in done] == [*(["clarification_pending"] * pause_count), "completed"]
    executions = _rows(case, "turn_agent_execution")
    assert len(executions) == 1 and executions[0]["status"] == "completed", [(i.tool_name, i.result) for i in result.tool_invocations]
    actual = ReferenceDelegation.model_validate_json(_rows(case, "chat_reference_resolutions")[1]["composition_json"])
    dispatched_task = render_primary_delegation_task(primary.source.input_document.text_content(), "检查实际利润变化")
    assert actual.task_hash == "sha256:" + hashlib.sha256(dispatched_task.encode()).hexdigest()


from test_skill_read_receiver_integration import receiver_case as _receiver_case

receiver_case = _receiver_case


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_delegation_keeps_readonly_skill_material_on_exact_primary(receiver_case, monkeypatch):
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
    from cryptography.fernet import Fernet
    from test_skill_start_admission import candidate

    fixture = receiver_case
    await candidate(fixture, agent_first=True)
    case = fixture.case
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1")
    await case.store.finalize_started_chat_submission(**case.args)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    claimed = await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        scope=fixture.scope.model_copy(update={"session_id": "s_reference"}),
        turn_id="t_reference",
        cipher=cipher,
        expected_generation=1,
        owner_id="worker-a",
    )
    assert claimed.primary.binding.status == "admitted"
    runtime = ReferencePrimaryRuntime(
        scope=case.scope, turn_id="t_reference", owner_id="worker-a", bindings=case.bindings, cipher=cipher, skill_sources=fixture.sources
    )
    case.runtime = runtime
    await _normal_lead(case, monkeypatch)
    assert len(_rows(case, "chat_reference_model_invocations")) == 1


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_predispatch_answer_keeps_skill_on_original_primary(receiver_case, monkeypatch):
    from types import SimpleNamespace

    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
    from bowhead_gateway.turn_service import _prepare_reference_bi_resume
    from cryptography.fernet import Fernet
    from test_skill_start_admission import candidate

    fixture = receiver_case
    await candidate(fixture, agent_first=True)
    case = fixture.case
    case.args["reference_start"] = replace(case.args["reference_start"], orchestration_contract="lead-delegation.v1")
    await case.store.finalize_started_chat_submission(**case.args)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        scope=fixture.scope.model_copy(update={"session_id": "s_reference"}),
        turn_id="t_reference",
        cipher=cipher,
        expected_generation=1,
        owner_id="worker-a",
    )
    case.runtime = ReferencePrimaryRuntime(
        scope=case.scope, turn_id="t_reference", owner_id="worker-a", bindings=case.bindings, cipher=cipher, skill_sources=fixture.sources
    )
    await _normal_lead(case, monkeypatch, clarification_tool=True)
    card = _rows(case, "clarification_requests")[0]
    await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], "甲")
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    request = SimpleNamespace(
        **case.scope.identity(),
        turn_id="t_reference",
        reference_clarification_id=card["request_id"],
        active_datarc_project=None,
        accessible_datarc_workspaces=(),
    )
    prepared = await _prepare_reference_bi_resume(
        SimpleNamespace(session_store=case.store, reference_skill_sources_factory=lambda _scope: fixture.sources), request
    )
    assert prepared["reference_skill_sources"] is fixture.sources
    assert request.text == json.loads(card["resume_context_json"])["user_text"]
    wrong_card = SimpleNamespace(**{**vars(request), "reference_clarification_id": "wrong-card"})
    with pytest.raises(ValueError):
        await _prepare_reference_bi_resume(
            SimpleNamespace(session_store=case.store, reference_skill_sources_factory=lambda _scope: fixture.sources), wrong_card
        )
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id="worker-a")
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    case.runtime = replace(case.runtime, generation=2, owner_id="worker-resume")
    await case.runtime.claim_bi_resume(clarification_id=card["request_id"])
    assert await case.runtime.reprepare_original_attachments_on_resume([])
    await _normal_lead(case, monkeypatch)
    assert len(_rows(case, "chat_reference_model_invocations")) == 1


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_revoked_completed_primary_is_not_authority_for_lead_summary(lead_case):
    import sqlite3

    import respx
    from bowhead_router.input_admission import LLMInputAdmissionError
    from bowhead_router.types import LLMRequest, Message
    from test_skill_model_invocation import adapter_for, router_for, success

    case = lead_case
    runtime = await _runtime(case)
    policy = await runtime.lead_input()
    runner = SubagentRunner(
        router=_router(LLMResponse(content="private primary output")), tool_registry=build_default_registry(), team_subagent_store=case.fixture.users
    )
    await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus(), delegation_task="实际任务", delegation_call_id="call")
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE agent_assignment SET access_state='revoked'")
    adapter = adapter_for("openai")
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
            request = policy.attach(
                LLMRequest(model="large", messages=[Message(role="user", content="private primary output")], max_output_tokens=100)
            )
            with pytest.raises(LLMInputAdmissionError):
                await router_for(adapter).invoke("analyst", request)
            assert route.call_count == 0
    finally:
        await adapter.close()
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_wrong_primary_role_or_identity_cannot_claim_reservation(lead_case):
    runtime = await _runtime(lead_case)
    primary = await runtime.validate()
    for args in (
        {"profile": "another-agent", "agent_id": None, "role": "primary"},
        {"profile": primary.binding.runtime_profile_name, "agent_id": "another-id", "role": "primary"},
        {"profile": "another-agent", "agent_id": None, "role": None},
        {"profile": primary.binding.runtime_profile_name, "agent_id": "another-id", "role": None},
        {"profile": primary.binding.runtime_profile_name, "agent_id": None, "role": "unexpected"},
    ):
        with pytest.raises(ValueError):
            await runtime.binding_for_delegation(**args)
    assert (await runtime.binding_for_delegation(profile="compute", agent_id=primary.binding.agent_id, role="primary")) == primary.binding
    assert (await runtime.binding_for_delegation(profile="compute", agent_id=primary.binding.agent_id, role=None)) == primary.binding
    assert (await runtime.binding_for_delegation(profile=primary.binding.runtime_profile_name, agent_id=None, role=None)) == primary.binding
    assert await runtime.binding_for_delegation(profile=primary.binding.runtime_profile_name, agent_id=None, role="helper") is None
    assert await runtime.binding_for_delegation(profile="assistant", agent_id=None, role="helper") is None
    assert _rows(lead_case, "turn_agent_execution")[0]["status"] == "admitted"
    assert not _rows(lead_case, "chat_reference_evidence")


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_concurrent_primary_delegations_invoke_once(lead_case):
    import asyncio

    from bowhead_core.subagent import SubagentResult

    runtime = await _runtime(lead_case)
    runner = SubagentRunner(
        router=_router(LLMResponse(content="One output")), tool_registry=build_default_registry(), team_subagent_store=lead_case.fixture.users
    )
    results = await asyncio.gather(
        *(
            runtime.run(
                runner=runner,
                parent_ctx=_ctx(lead_case),
                parent_bus=TurnEventBus(),
                delegation_task="同一个主要任务",
                delegation_call_id=call,
                expect_first_dispatch=True,
            )
            for call in ("call-a", "call-b")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, SubagentResult) and r.ok for r in results) == 1
    from bowhead_core.chat_reference_runtime import ReferenceDispatchRefused

    # The losing sibling is refused before any write; the Turn keeps the winner.
    assert sum(isinstance(r, ReferenceDispatchRefused) for r in results) == 1
    assert len(_rows(lead_case, "chat_reference_resolutions")) == 2
    assert len(_rows(lead_case, "chat_reference_evidence")) == 2


@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_successful_helper_cannot_replace_selected_primary(lead_case, monkeypatch):
    await _normal_lead(lead_case, monkeypatch, role="helper")
    executions = _rows(lead_case, "turn_agent_execution")
    assert len(executions) == 2
    assert sorted(e["status"] for e in executions) == ["completed", "failed"]
