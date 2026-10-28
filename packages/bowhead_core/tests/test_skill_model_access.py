"""Current saved-source authority through real Router/native HTTP boundaries.

HTTP is mocked; these tests do not mount the mixed scheduler or measure real
provider quality/cost. Preparation facts are distinct from actual send evidence. Receiver ContextVars are explicit
except in the actual SubagentRunner test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx2
import pytest
import respx
from bowhead_core.skill_model_access import AuthorizedSkillModelInput
from bowhead_router.errors import RetryPolicy
from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.invocation import physical_invocation_scope
from bowhead_router.router import AgentBinding, Router
from bowhead_router.types import LLMRequest, Message, ToolSchema
from test_skill_model_wire import adapter_for
from test_skill_read_access import access_case as _access_case
from test_skill_read_access import claimed_case as _claimed_case
from test_skill_read_access import receiver
from test_skill_read_access import receiver_case as _receiver_case
from test_skill_read_access import start_case as _start_case
from test_skill_read_model_input import Adapter, Registry
from test_skill_reference_claim import facts, restore

access_case = _access_case
claimed_case = _claimed_case
receiver_case = _receiver_case
start_case = _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.fixture
async def model_case(access_case):
    case = access_case
    case.ctx.session_store = case.case.fixture.case.store
    case.ctx.reference_skill_access = case.access
    _, _, rendered = await restore(case.case)
    policy = AuthorizedSkillModelInput.for_receiver(rendered, access=case.access, ctx=case.ctx)
    return SimpleNamespace(case=case, policy=policy)


def request(case):
    return case.policy.attach(LLMRequest(model="large", system="BASELINE", messages=[Message(role="user", content="TASK")]))


def router_for(adapter, secondary=None):
    registry = Registry(primary=adapter, **({"secondary": secondary} if secondary is not None else {}))
    router = Router(registry)
    router.bind(AgentBinding(agent_type="analyst", primary=("primary", "large"), fallback=[("secondary", "large")] if secondary else []))
    router.set_context_windows({("primary", "large"): 1000000, ("secondary", "large"): 1000000})
    return router


async def call(router, req, mode="invoke"):
    retry = RetryPolicy(max_attempts_per_target=2, base_delay_s=0)
    if mode == "invoke":
        return (await router.invoke("analyst", req, retry_policy=retry)).content
    return "".join([chunk.delta async for chunk in router.stream("analyst", req, retry_policy=retry)])


def success(family, mode):
    if family == "openai":
        body = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        events = [
            {"choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        ]
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
    elif family == "responses":
        body = {
            "id": "reply",
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        stream = 'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"OK"}\n\n'
        stream += "event: response.completed\ndata: " + json.dumps({"type": "response.completed", "response": body}) + "\n\n"
    else:
        body = {
            "id": "reply",
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        events = [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1, "output_tokens": 0}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        stream = "".join("event: " + name + "\ndata: " + json.dumps(event) + "\n\n" for name, event in events)
    return httpx2.Response(200, json=body) if mode == "invoke" else httpx2.Response(200, text=stream, headers={"content-type": "text/event-stream"})


async def revoke(case, name="support"):
    fixture = case.case.case.fixture
    await fixture.sources.instances.upsert(
        user_id=case.case.ctx.user_id, name=name, source_type="builtin", source_path=str(fixture.root / name), enabled=False
    )


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_current_methods_reach_native_wire_without_later_request_leak(model_case, family, mode):
    case = model_case
    adapter = adapter_for(family)
    router = router_for(adapter)
    before = facts(case.case.case)
    try:
        with respx.mock() as mock, receiver(case.case.handle.subagent_id):
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=lambda _: success(family, mode))
            assert await call(router, request(case), mode) == "OK"
            body = json.loads(route.calls[0].request.content)
            assert "BODY_method" in json.dumps(body) and "BODY_support" in json.dumps(body)
            assert "NOT_SELECTED_MANIFEST" not in json.dumps(body)
            assert "input_admission" not in json.dumps(body)
            # Existing Agent dispatch/output state is unchanged by model preparation.
            assert facts(case.case.case) == before
            # A fresh ordinary model request on the same Router has no policy.
            assert await call(router, LLMRequest(model="large", messages=[Message(role="user", content="ordinary")]), mode) == "OK"
            assert "BODY_method" not in route.calls[1].request.content.decode()
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_revocation_between_router_admission_and_send_reserves_no_ticket_or_http(model_case, family, mode, monkeypatch):
    import bowhead_router.adapters.anthropic_compat as anthropic
    import bowhead_router.adapters.openai_compat as openai

    case = model_case
    adapter = (
        openai.OpenAICompatibleAdapter(
            base_url="https://api.openai.com/v1", api_key="test-only", provider_name="primary", provider_options={"models": [{"id": "large"}]}
        )
        if family == "openai" and mode == "stream"
        else adapter_for(family)
    )
    fallback = Adapter(["MUST_NOT_RUN"])
    router = router_for(adapter, fallback)
    module = anthropic if family == "anthropic" else openai
    original = module.begin_physical_invocation
    tickets = []

    async def before_send(**kwargs):
        await revoke(case)
        return await original(**kwargs)

    class Hook:
        async def begin(self, spec):
            tickets.append(spec)
            raise AssertionError("denied preparation must not reserve an invocation")

    monkeypatch.setattr(module, "begin_physical_invocation", before_send)
    try:
        with respx.mock(assert_all_called=False) as mock, receiver(case.case.handle.subagent_id), physical_invocation_scope(Hook()):
            route = mock.post(url__regex=r"https://api\.(?:example|openai)\.com/.*").mock(side_effect=lambda _: success(family, mode))
            with pytest.raises(LLMInputAdmissionError, match="authority_unavailable"):
                await call(router, request(case), mode)
            assert route.call_count == 0 and tickets == [] and fallback.calls == []
    finally:
        await adapter.close()


@pytest.mark.parametrize("family", ["openai", "anthropic"])
@pytest.mark.parametrize("revoke_first", [False, True])
async def test_self_heal_rechecks_authority_without_a_terminal_hook(model_case, family, revoke_first, monkeypatch):
    from bowhead_router.outbound import reset_learned_for_test

    reset_learned_for_test()
    case = model_case
    adapter = adapter_for(family)
    router = router_for(adapter)
    sent = []

    async def respond(http_request):
        sent.append(json.loads(http_request.content))
        if len(sent) == 1:
            if revoke_first:
                await revoke(case)
            return httpx2.Response(
                400, json={"error": {"message": "unsupported temperature for this model", "param": "temperature", "type": "invalid_request_error"}}
            )
        return success(family, "invoke")

    try:
        with respx.mock() as mock, receiver(case.case.handle.subagent_id):
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=respond)
            if revoke_first:
                with pytest.raises(LLMInputAdmissionError, match="authority_unavailable"):
                    await call(router, request(case))
                assert len(sent) == 1
            else:
                assert await call(router, request(case)) == "OK"
                assert len(sent) == 2 and "temperature" not in sent[1]
    finally:
        await adapter.close()
        reset_learned_for_test()


@pytest.mark.parametrize("change", ["bytes", "receiver", "context", "marker", "handles", "lease", "cancel", "purge"])
async def test_wrong_or_expired_model_authority_never_reaches_an_adapter(model_case, change):
    case = model_case
    subagent_id = case.case.handle.subagent_id
    if change == "bytes":
        text = case.policy.content.replace("BODY_method", "FOREIGN_METHOD")
        case.policy = replace(case.policy, content=text, content_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest())
    elif change == "receiver":
        subagent_id = "helper"
    elif change == "context":
        case.case.ctx.reference_skill_access = None
    elif change == "marker":
        case.case.ctx.reference_skill_required = None
    elif change == "handles":
        access = replace(case.case.access, handles=case.case.access.handles[::-1])
        case.case.ctx.reference_skill_access = access
        case.policy = replace(case.policy, access=access)
    else:
        with sqlite3.connect(case.case.case.path) as db:
            if change == "lease":
                db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
                )
            elif change == "cancel":
                db.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id='t_reference'", (datetime.now(UTC).isoformat(),))
            else:
                db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
    adapter = Adapter(["MUST_NOT_RUN"])
    with receiver(subagent_id), pytest.raises(LLMInputAdmissionError, match="authority_unavailable"):
        await call(router_for(adapter), request(case))
    assert adapter.calls == []


async def test_cancelled_authorization_propagates_and_does_not_poison_next_check(model_case, monkeypatch):
    case = model_case
    original = type(case.case.access.sources).revalidate_in_connection

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(type(case.case.access.sources), "revalidate_in_connection", cancel)
    adapter = Adapter(["OK"])
    with receiver(case.case.handle.subagent_id), pytest.raises(asyncio.CancelledError):
        await call(router_for(adapter), request(case))
    assert adapter.calls == []
    monkeypatch.setattr(type(case.case.access.sources), "revalidate_in_connection", original)
    with receiver(case.case.handle.subagent_id):
        assert await call(router_for(adapter), request(case)) == "OK"


@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_router_retry_after_revocation_never_reuses_the_old_input_permission(model_case, mode):
    case = model_case
    adapter = adapter_for("openai")
    secondary = Adapter(["MUST_NOT_RUN"])

    async def failed(_):
        await revoke(case, "method")
        return httpx2.Response(500, json={"error": {"message": "temporary failure"}})

    try:
        with respx.mock() as mock, receiver(case.case.handle.subagent_id):
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=failed)
            with pytest.raises(LLMInputAdmissionError, match="authority_unavailable"):
                await call(router_for(adapter, secondary), request(case), mode)
            assert route.call_count == 1 and secondary.calls == []
    finally:
        await adapter.close()


async def test_original_files_can_disappear_without_substituting_current_method_text(model_case):
    case = model_case
    for name in ("method", "support"):
        path = case.case.case.fixture.root / name / "method.md"
        path.write_text("REPLACEMENT_MUST_NOT_BE_SENT")
        path.unlink()
    adapter = adapter_for("openai")
    try:
        with respx.mock() as mock, receiver(case.case.handle.subagent_id):
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=lambda _: success("openai", "invoke"))
            assert await call(router_for(adapter), request(case)) == "OK"
            sent = route.calls[0].request.content.decode()
            assert "BODY_method" in sent and "REPLACEMENT_MUST_NOT_BE_SENT" not in sent
    finally:
        await adapter.close()


async def test_existing_subagent_runner_uses_the_actual_receiver_for_native_dispatch(model_case):
    from bowhead_core.events import TurnEventBus
    from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import build_default_registry

    case = model_case
    primary = (await claim_reference_skill_primary_for_worker(**case.case.case.args)).primary
    adapter = adapter_for("openai")
    runner = SubagentRunner(
        router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=case.case.case.fixture.case.fixture.users
    )
    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=lambda _: success("openai", "invoke"))
            # No manually established receiver ContextVar in this test.
            result = await runner.run(
                profile_name=primary.binding.runtime_profile_name,
                task=primary.source.input_document.text_content(),
                parent_turn_id=case.case.ctx.turn_id,
                parent_bus=TurnEventBus(),
                parent_ctx=case.case.ctx,
                exact_binding=primary.binding,
                pre_minted_subagent_id=case.case.handle.subagent_id,
                required_model_input=case.policy,
            )
            assert result.ok and result.subagent_id == case.case.handle.subagent_id
            body = json.loads(route.calls[0].request.content)
            assert "BODY_method" in json.dumps(body)
            assert primary.source.input_document.text_content() in json.dumps(body, ensure_ascii=False).replace("\\n", "\n")
    finally:
        await adapter.close()


@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("revoke_first", [False, True])
async def test_parallel_parameter_self_heal_keeps_current_authority(model_case, mode, revoke_first, monkeypatch):
    from bowhead_router.adapters import _chi_sniff
    from bowhead_router.outbound import reset_learned_for_test

    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _chi_sniff._reset_for_test()
    reset_learned_for_test()
    case = model_case
    adapter = adapter_for("openai")
    sent = []

    async def respond(http_request):
        sent.append(json.loads(http_request.content))
        if len(sent) == 1:
            if revoke_first:
                await revoke(case)
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "message": "Unknown parameter: parallel_tool_calls",
                        "param": "parallel_tool_calls",
                        "type": "invalid_request_error",
                        "code": "unsupported_parameter",
                    }
                },
            )
        return success("openai", mode)

    req = request(case)
    req.tools = [ToolSchema("probe", "tool schema for transport compatibility", {})]
    try:
        with respx.mock() as mock, receiver(case.case.handle.subagent_id):
            mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=respond)
            if revoke_first:
                with pytest.raises(LLMInputAdmissionError, match="authority_unavailable"):
                    await call(router_for(adapter), req, mode)
                assert len(sent) == 1
            else:
                assert await call(router_for(adapter), req, mode) == "OK"
                assert len(sent) == 2 and "parallel_tool_calls" not in sent[1]
            assert sent[0]["parallel_tool_calls"] is True
    finally:
        await adapter.close()
        _chi_sniff._reset_for_test()
        reset_learned_for_test()


@pytest.mark.parametrize("unsupported", ["invoke", "outside_read"])
async def test_model_adapter_refuses_an_unsupported_baseline_before_sending(model_case, monkeypatch, unsupported):
    import bowhead_core.skill_read_access as module

    case = model_case
    original = module._load_current

    async def unsupported_baseline(**kwargs):
        composition, rendered, prepared = await original(**kwargs)
        # Fault at the verified reader's adapter input, not a release fixture.
        first = prepared.receiver.grants[0]
        grants = (
            (replace(first, permissions=("invoke", "read")), *prepared.receiver.grants[1:])
            if unsupported == "invoke"
            else (*prepared.receiver.grants, replace(first, id="builtin:outside", name="outside"))
        )
        return composition, rendered, replace(prepared, receiver=prepared.receiver.model_copy(update={"grants": grants}))

    monkeypatch.setattr(module, "_load_current", unsupported_baseline)
    adapter = Adapter(["MUST_NOT_RUN"])
    with receiver(case.case.handle.subagent_id), pytest.raises(LLMInputAdmissionError, match="authority_unavailable"):
        await call(router_for(adapter), request(case))
    assert adapter.calls == []


async def test_receiver_context_cannot_be_removed_while_the_database_check_is_waiting(model_case, monkeypatch):
    case = model_case
    original = type(case.case.access.sources).revalidate_in_connection

    async def changed(self, db, proof):
        await original(self, db, proof)
        case.case.ctx.reference_skill_access = None

    monkeypatch.setattr(type(case.case.access.sources), "revalidate_in_connection", changed)
    adapter = Adapter(["MUST_NOT_RUN"])
    with receiver(case.case.handle.subagent_id), pytest.raises(LLMInputAdmissionError, match="authority_unavailable"):
        await call(router_for(adapter), request(case))
    assert adapter.calls == []


async def test_private_source_failures_are_not_model_errors_with_private_details(model_case, monkeypatch):
    case = model_case

    async def fail(*args, **kwargs):
        raise ValueError("PRIVATE_METHOD_PARAMETER_MUST_NOT_LEAK")

    monkeypatch.setattr(type(case.case.access.sources), "revalidate_in_connection", fail)
    adapter = Adapter(["MUST_NOT_RUN"])
    with receiver(case.case.handle.subagent_id), pytest.raises(LLMInputAdmissionError) as error:
        await call(router_for(adapter), request(case))
    assert "PRIVATE_METHOD_PARAMETER" not in "".join(traceback.format_exception(error.value))
    assert adapter.calls == []


@pytest.mark.parametrize("family", ["openai", "responses", "anthropic"])
async def test_dispatch_checks_the_actual_body_after_it_was_built(model_case, family, monkeypatch):
    import bowhead_router.adapters.anthropic_compat as anthropic
    import bowhead_router.adapters.openai_compat as openai

    case = model_case
    module = anthropic if family == "anthropic" else openai
    original = module.begin_physical_invocation

    async def changed_body(**kwargs):
        body = kwargs["request"]
        user = next(m for m in body["input" if family == "responses" else "messages"] if m.get("role") == "user")
        user["content"] = "LOST_REQUIRED_METHOD"
        return await original(**kwargs)

    monkeypatch.setattr(module, "begin_physical_invocation", changed_body)
    adapter = adapter_for(family)
    try:
        with respx.mock(assert_all_called=False) as mock, receiver(case.case.handle.subagent_id):
            route = mock.post(url__regex=r"https://api\.example\.com/.*").mock(side_effect=lambda _: success(family, "invoke"))
            with pytest.raises(LLMInputAdmissionError, match="input_changed"):
                await call(router_for(adapter), request(case))
            assert route.call_count == 0
    finally:
        await adapter.close()
