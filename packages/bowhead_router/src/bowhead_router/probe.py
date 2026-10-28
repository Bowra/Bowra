"""Single-capability probe planning and execution for verification V2."""

from __future__ import annotations

import json
import math
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx2

from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.outbound import ProtocolFamily, apply_outbound_params, rejected_param, resolve_outbound_params, wire_param_name
from bowhead_router.ssrf_guard import (
    SsrfBlocked,
    build_guarded_async_client,
)
from bowhead_router.transport import (
    CanonicalProviderTransportPlan,
    build_provider_transport_plan,
    materialize_transport_headers,
    model_api_surface,
    render_custom_http_body,
    uses_max_completion_tokens,
)

ModelCapability = Literal["chat", "tool", "vision", "embed", "rerank"]
ProbeClassification = Literal["factual_evidence", "attempt_error"]
ProbeOutcome = Literal["verified", "failed", "not_testable"]
ProbeTerminalState = Literal[
    "verified",
    "failed",
    "not_testable",
    "errored",
]

PROBE_EXECUTION_POLICY_VERSION = "probe-policy-v1"
NO_REQUEST_POLICY_VERSION = "no-provider-request-v1"
_PROBE_RESPONSE_MAX_BYTES = 1024 * 1024
_GPT5_PROBE_MAX_OUTPUT_TOKENS = 128
_VISION_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAI0lEQVR4nGP8//8/AymAiSTVDKMaiANMRKqDg1ENxACSNQAAVW0DHUvDbUgAAAAASUVORK5CYII="

# chat-v2 (2026-08-17): chat probes now stream in the same mode as runtime calls (see `probe_stream`
# in `plan()`). A request shape change must bump the version; otherwise historical evidence falsely claims validation
# under the new shape, and `probe_contract_version` is part of config_signature.
_PROBE_CONTRACTS = {
    # Versions bumped together on 2026-08-17: `openai_compatible` / `azure_openai` probes previously never streamed
    # (their adapter_contract_version is `openai-compatible-v1`, not SSE), but now stream in the same mode
    # as runtime calls. The request shapes of all three capabilities (chat / tool / vision) changed.
    # Shape and contract version must change together: `probe_contract_version` enters config_signature;
    # failing to bump it makes historical evidence falsely claim validation under the new shape.
    # embed has no streaming form and is unchanged, so keep v1.
    "chat": "chat-v2",
    "tool": "tool-call-v2",
    "vision": "vision-v3",
    "embed": "embedding-v1",
}


@dataclass(frozen=True, slots=True)
class ProbeRequestPlan:
    transport: CanonicalProviderTransportPlan
    provider_id: str
    provider_display_name: str
    model_id: str
    capability: ModelCapability
    testability_contract_version: str
    probe_contract_version: str
    probe_execution_policy_version: str
    body_shape: dict[str, Any]
    provider_request_count: int = 1


@dataclass(frozen=True, slots=True)
class NotTestable:
    transport: CanonicalProviderTransportPlan
    provider_id: str
    provider_display_name: str
    model_id: str
    capability: ModelCapability
    testability_contract_version: str
    probe_execution_policy_version: str
    safe_code: str = "probe_not_supported"
    probe_contract_version: None = None
    provider_request_count: Literal[0] = 0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    classification: ProbeClassification
    outcome: ProbeOutcome | None
    code: str
    safe_http_status: int | None
    safe_summary_code: str | None
    duration_ms: int
    vector_dimension: int | None = None
    actual_model_id: str | None = None

    @property
    def terminal_state(self) -> ProbeTerminalState:
        if self.classification == "attempt_error":
            return "errored"
        assert self.outcome is not None
        return self.outcome


def _provider_type(provider: Any) -> str:
    return str(provider.get("type") if isinstance(provider, dict) else getattr(provider, "type", ""))


def _testability_version(
    adapter_type: str,
    capability: ModelCapability,
) -> str:
    short = (
        "openai"
        if adapter_type == "openai_compatible"
        else "anthropic"
        if adapter_type == "anthropic_compatible"
        else adapter_type.replace("_compatible", "")
    )
    return f"{short}-{capability}-testability-v1"


def _probe_contract_version(
    adapter_type: str,
    capability: ModelCapability,
    model_id: str,
    *,
    responses_surface: bool = False,
) -> str:
    if responses_surface:
        # New contract string = new evidence identity (the entire shape changed); no effect on existing evidence: models switched in batches 0/1
        # had only not_testable results (tech-design §10.1).
        return f"openai-responses-{capability}-v1"
    if adapter_type == "anthropic_compatible" and capability == "chat":
        return "anthropic-chat-v2"
    if adapter_type == "anthropic_compatible" and capability == "tool":
        return "anthropic-tool-call-v2"
    if adapter_type == "azure_openai" and capability in {"chat", "vision"} and uses_max_completion_tokens(model_id):
        # Bump chat to v3 for streaming; vision shape is unchanged, so keep v2.
        return f"azure-gpt5-{capability}-{'v3' if capability == 'chat' else 'v2'}"
    if adapter_type == "custom_http" and capability in {"chat", "vision"} and uses_max_completion_tokens(model_id):
        return f"custom-http-gpt5-{capability}-{'v3' if capability == 'chat' else 'v2'}"
    return _PROBE_CONTRACTS[capability]


def _openai_body(
    model_id: str,
    capability: ModelCapability,
    *,
    max_completion_tokens: bool = False,
    include_output_limit: bool = True,
) -> dict[str, Any]:
    if capability == "embed":
        return {
            "model": model_id,
            "input": "bowhead connection probe",
        }
    content: Any = "hello"
    # GPT-5 counts hidden reasoning in max_completion_tokens. A one-token
    # budget can fail at the gateway before the assistant emits probe text.
    max_tokens = _GPT5_PROBE_MAX_OUTPUT_TOKENS if max_completion_tokens else 1
    if capability == "tool":
        content = "You must call the bowhead_connection_probe tool now. Do not answer with text."
        max_tokens = _GPT5_PROBE_MAX_OUTPUT_TOKENS
    elif capability == "vision":
        content = [
            {"type": "text", "text": "Reply with OK"},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{_VISION_PNG_BASE64}",
                },
            },
        ]
    body: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": content}],
    }
    if include_output_limit:
        body["max_completion_tokens" if max_completion_tokens else "max_tokens"] = max_tokens
    if capability == "tool":
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "bowhead_connection_probe",
                    "description": "connection probe",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
        ]
        body["tool_choice"] = "auto"
    return body


def _responses_body(
    model_id: str,
    capability: ModelCapability,
) -> dict[str, Any]:
    """Responses 面探针 body（与 runtime head 同形，tech-design §10.1；embed 不走这里）。

    chat 预算直接用 GPT-5 档：Responses 面以推理模型为主，隐藏推理计入
    `max_output_tokens`（qwen3.6 实测 reasoning_tokens 会吃掉小预算），1 token
    会在模型说出探针文本之前就被扣光。
    """

    content: Any = "hello"
    max_output_tokens = _GPT5_PROBE_MAX_OUTPUT_TOKENS
    if capability == "tool":
        content = "You must call the bowhead_connection_probe tool now. Do not answer with text."
    elif capability == "vision":
        # ⚠ Responses image_url is a **string**, not an object. This is the documented shape, without a real endpoint sample;
        # if a gateway rejects it, the probe honestly fails, and admins may leave vision unchecked (tech-design §10.1).
        content = [
            {"type": "input_text", "text": "Reply with OK"},
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{_VISION_PNG_BASE64}",
            },
        ]
    body: dict[str, Any] = {
        "model": model_id,
        "input": [{"role": "user", "content": content}],
        # Match runtime: probes must not silently enable provider-side retention (tech-design §8.4).
        "store": False,
        "max_output_tokens": max_output_tokens,
    }
    if capability == "tool":
        body["tools"] = [
            {
                "type": "function",
                "name": "bowhead_connection_probe",
                "description": "connection probe",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        body["tool_choice"] = "auto"
    return body


def _anthropic_body(
    model_id: str,
    capability: ModelCapability,
) -> dict[str, Any]:
    content: Any = "Reply with OK"
    max_tokens = 8
    if capability == "vision":
        content = [
            {"type": "text", "text": "Reply with OK"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _VISION_PNG_BASE64,
                },
            },
        ]
    elif capability == "tool":
        content = "You must call the bowhead_connection_probe tool now. Do not answer with text."
        max_tokens = 128
    body: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "disabled"},
    }
    if capability == "tool":
        body["tools"] = [
            {
                "name": "bowhead_connection_probe",
                "description": "connection probe",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        body["tool_choice"] = {
            "type": "auto",
        }
    return body


class CompatibleProbeRegistry:
    """Plan and execute one bounded request for compatible adapters."""

    def plan(
        self,
        provider: Any,
        *,
        provider_id: str,
        provider_display_name: str,
        model_id: str,
        capability: ModelCapability,
    ) -> ProbeRequestPlan | NotTestable:
        adapter_type = _provider_type(provider)
        testability = _testability_version(adapter_type, capability)
        not_testable = capability == "rerank" or (adapter_type == "anthropic_compatible" and capability == "embed")
        # Responses surface (real probes from batch 2): batch 0 not_testable was removed; Responses decoding was absent then,
        # but probes now use the same mode as runtime calls (streaming /responses), and transport routes by surface.
        responses_probe = adapter_type == "openai_compatible" and capability != "embed" and model_api_surface(provider, model_id) == "responses"
        transport_purpose = "probe_chat" if not_testable else f"probe_{capability}"
        transport = build_provider_transport_plan(
            provider,
            purpose=transport_purpose,  # type: ignore[arg-type]
            model_id=model_id,
            capability=capability,
        )
        if not_testable:
            return NotTestable(
                transport=transport,
                provider_id=provider_id,
                provider_display_name=provider_display_name,
                model_id=model_id,
                capability=capability,
                testability_contract_version=testability,
                probe_execution_policy_version=NO_REQUEST_POLICY_VERSION,
            )
        if adapter_type in {"openai_compatible", "azure_openai", "custom_http"}:
            # Probes must use the same mode as runtime calls; a non-streaming success cannot verify a streaming request.
            #
            # ⚠ Do not check only `adapter_contract_version == "openai_chat_completions_sse"`:
            # that value belongs to the **custom_http declarative contract**; `openai_compatible` / `azure_openai`
            # have adapter_contract_version `openai-compatible-v1`, so their probes
            # **never streamed** (including tool probes), although their real conversations stream.
            #
            # embed has no streaming form and stays excluded. Shape changed → chat contract version was bumped to v2 alongside it.
            probe_stream = capability != "embed" and (
                adapter_type in {"openai_compatible", "azure_openai"} or transport.adapter_contract_version == "openai_chat_completions_sse"
            )
            gpt5_reasoning_probe = capability in {"chat", "tool", "vision"} and uses_max_completion_tokens(model_id)
            if responses_probe:
                body = _responses_body(model_id, capability)
            else:
                body = _openai_body(
                    model_id,
                    capability,
                    max_completion_tokens=(adapter_type == "azure_openai" and gpt5_reasoning_probe),
                    include_output_limit=(capability != "chat" or adapter_type == "custom_http"),
                )
            if probe_stream:
                body["stream"] = True
            if adapter_type == "custom_http":
                body = render_custom_http_body(
                    provider,
                    model_id=model_id,
                    capability=capability,
                    variables={
                        "model": model_id,
                        "deployment": _deployment_for(provider, model_id),
                        "messages": body.get("messages"),
                        "tools": body.get("tools"),
                        "tool_choice": body.get("tool_choice"),
                        "stream": probe_stream,
                        "max_output_tokens": body.get(
                            "max_completion_tokens",
                            (_GPT5_PROBE_MAX_OUTPUT_TOKENS if gpt5_reasoning_probe else body.get("max_tokens")),
                        ),
                        "input": body.get("input"),
                    },
                )
        elif adapter_type == "anthropic_compatible":
            body = _anthropic_body(model_id, capability)
        else:
            raise ValueError(f"unsupported probe adapter: {adapter_type!r}")
        # Probes and runtime calls **share the same source**: one outbound pipeline (base body → model overrides → outbound declarations).
        # Ignoring a model override can falsely reject a deployment that already has a valid administrator-supplied parameter.
        family = "anthropic" if adapter_type == "anthropic_compatible" else ("openai_responses" if responses_probe else "openai")
        if adapter_type != "custom_http":
            # custom_http renders the body from a declarative template; render_custom_http_body already consumes overrides.
            _apply_model_body_overrides(
                provider,
                body,
                model_id=model_id,
                capability=capability,
                responses_surface=responses_probe,
            )
        apply_outbound_params(
            body,
            resolve_outbound_params(provider, model_id=model_id, family=family),  # type: ignore[arg-type]
            family=family,  # type: ignore[arg-type]
        )
        from bowhead_router.runtime_parameters import model_spec

        if capability in {"chat", "tool", "vision"} and model_spec(provider, model_id).get("runtime_parameters"):
            from bowhead_router.parameter_preview import compile_request_body, synthetic_request
            from bowhead_router.runtime_parameters import RuntimeParametersError, effective_policy

            definition = provider if isinstance(provider, dict) else vars(provider)
            policy = effective_policy(model_spec(provider, model_id), capability)
            budget = policy.get("thinking_budget") or 0
            try:
                body = compile_request_body(definition, synthetic_request(model_id, capability, max_output_tokens=max(128, budget + 128)))
                output = body.get("max_output_tokens", body.get("max_completion_tokens", body.get("max_tokens")))
                thinking = body.get("thinking")
                wire_budgets = [budget, body.get("thinking_budget"), thinking.get("budget_tokens") if isinstance(thinking, dict) else None]
                bounded = (
                    isinstance(output, int)
                    and not isinstance(output, bool)
                    and 0 < output <= 4096
                    and all(
                        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2048 for value in wire_budgets if value is not None
                    )
                )
            except (RuntimeParametersError, LLMInputAdmissionError):
                bounded = False
            if not bounded:
                return NotTestable(
                    transport=transport,
                    provider_id=provider_id,
                    provider_display_name=provider_display_name,
                    model_id=model_id,
                    capability=capability,
                    testability_contract_version=testability,
                    probe_execution_policy_version=NO_REQUEST_POLICY_VERSION,
                    safe_code="parameter_policy_exceeds_probe_limits",
                )
        return ProbeRequestPlan(
            transport=transport,
            provider_id=provider_id,
            provider_display_name=provider_display_name,
            model_id=model_id,
            capability=capability,
            testability_contract_version=testability,
            probe_contract_version=_probe_contract_version(
                adapter_type,
                capability,
                model_id,
                responses_surface=responses_probe,
            )
            + (
                "/runtime-parameters-v1"
                if model_spec(provider, model_id).get("runtime_parameters") and capability in {"chat", "tool", "vision"}
                else ""
            ),
            probe_execution_policy_version=PROBE_EXECUTION_POLICY_VERSION,
            body_shape=body,
        )

    async def execute(
        self,
        plan: ProbeRequestPlan,
        *,
        timeout: float,
    ) -> ProbeResult:
        started = time.monotonic()

        def result(
            *,
            classification: ProbeClassification,
            outcome: ProbeOutcome | None,
            code: str,
            http_status: int | None = None,
            summary: str | None = None,
            vector_dimension: int | None = None,
            actual_model_id: str | None = None,
        ) -> ProbeResult:
            return ProbeResult(
                classification=classification,
                outcome=outcome,
                code=code,
                safe_http_status=http_status,
                safe_summary_code=summary,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                vector_dimension=vector_dimension,
                actual_model_id=actual_model_id,
            )

        try:
            async with (
                build_guarded_async_client(
                    timeout=timeout,
                    headers=materialize_transport_headers(plan.transport),
                ) as client,
                client.stream(
                    plan.transport.method,
                    plan.transport.request_url,
                    json=plan.body_shape,
                ) as response,
            ):
                status = response.status_code
                raw = bytearray()
                response_too_large = False
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _PROBE_RESPONSE_MAX_BYTES:
                        response_too_large = True
                        break
                if status == 401:
                    return result(
                        classification="factual_evidence",
                        outcome="failed",
                        code="auth_failed",
                        http_status=status,
                    )
                if status == 403:
                    return result(
                        classification="factual_evidence",
                        outcome="failed",
                        code="forbidden",
                        http_status=status,
                    )
                if status == 404:
                    return result(
                        classification="factual_evidence",
                        outcome="failed",
                        code=("deployment_not_found" if plan.transport.adapter_type == "azure_openai" else "endpoint_not_found"),
                        http_status=status,
                    )
                if status == 429:
                    return result(
                        classification="attempt_error",
                        outcome=None,
                        code="rate_limited",
                        http_status=status,
                    )
                if status >= 500:
                    return result(
                        classification="attempt_error",
                        outcome=None,
                        code="provider_unavailable",
                        http_status=status,
                    )
                if status >= 400:
                    return result(
                        classification="factual_evidence",
                        outcome="failed",
                        code=_invalid_request_code(
                            plan.transport.adapter_type,
                            bytes(raw),
                            responses_surface=plan.transport.adapter_contract_version == "openai-responses-v1",
                        ),
                        http_status=status,
                    )
                if response_too_large:
                    return result(
                        classification="factual_evidence",
                        outcome="failed",
                        code="invalid_response",
                        http_status=status,
                        summary="response_too_large",
                    )
        except SsrfBlocked:
            return result(
                classification="attempt_error",
                outcome=None,
                code="blocked_by_security_policy",
            )
        except httpx2.TimeoutException:
            return result(
                classification="attempt_error",
                outcome=None,
                code="connection_timeout",
            )
        except httpx2.ConnectError as exc:
            code = _connect_error_code(exc)
            return result(
                classification="attempt_error",
                outcome=None,
                code=code,
            )
        except httpx2.HTTPError:
            return result(
                classification="attempt_error",
                outcome=None,
                code="internal_probe_error",
            )

        try:
            payload = _decode_success_payload(plan, bytes(raw))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return result(
                classification="factual_evidence",
                outcome="failed",
                code="invalid_response",
                http_status=status,
            )
        actual_model = str(payload.get("model")) if isinstance(payload, dict) and payload.get("model") else None
        # Name mismatches **no longer fail** (the `model_identity_mismatch` short circuit was removed on 2026-08-25).
        # Gateway renaming is an observed fact, not a configuration error: 116 observed Doubao returning a versioned ID
        # (2026-08-14, HTTP 200 classified as failure); DeepSeek receives `deepseek-chat` and returns
        # `deepseek-v4-flash` (both chat/responses surfaces); UPTIMIZE explicitly documents three layers of canonical
        # name rewriting. Machines cannot distinguish benign aliases from silent fallbacks, so record only facts: persist observations
        # (`actual_model_id` is visible throughout from 0018), and annotate name mismatches in the overview with
        # "Server returned: X", leaving judgment to people. `_classify_success` still determines actual capability failures.
        return _classify_success(
            plan,
            payload,
            status=status,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            actual_model=actual_model,
        )


def not_testable_result() -> ProbeResult:
    return ProbeResult(
        classification="factual_evidence",
        outcome="not_testable",
        code="probe_not_supported",
        safe_http_status=None,
        safe_summary_code=None,
        duration_ms=0,
    )


def _decode_responses_sse(text: str) -> Any:
    """Responses 事件流 ⇒ 终帧 `response.completed` 自带的完整 response 对象。

    真流样本：docs/sprints/responses-api-2026-08/samples/*-stream*.txt——completed 帧
    的 `response` 携带全量 output[]/usage/model，探针不需要自己攒增量。
    `response.failed` / `error` / 无终帧 ⇒ ValueError ⇒ `invalid_response`。
    """

    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        frame = json.loads(data)
        if not isinstance(frame, dict):
            raise ValueError("invalid SSE response frame")
        ftype = frame.get("type")
        if ftype == "response.completed" and isinstance(frame.get("response"), dict):
            return frame["response"]
        if ftype in {"response.failed", "error"}:
            raise ValueError("provider reported stream failure")
    raise ValueError("SSE response did not reach a terminal frame")


def _connect_error_code(exc: httpx2.ConnectError) -> str:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return "tls_error"
        if isinstance(cause, socket.gaierror):
            return "dns_error"
        cause = cause.__cause__
    return "provider_unavailable"


def _invalid_request_code(adapter_type: str, raw: bytes, *, responses_surface: bool = False) -> str:
    """Classify a bounded provider 4xx body without publishing its contents.

    Prefer a structured parameter identifier to prose keywords so an unsupported
    parameter directs the administrator to model parameters, not protocol templates."""

    if rejected_param(raw, family="openai_responses" if responses_surface else None) is not None:
        return "parameter_incompatible"
    text = raw[:65536].decode("utf-8", errors="replace").lower()
    if adapter_type == "azure_openai" and ("api-version" in text or "api version" in text or "apiversion" in text):
        return "api_version_invalid"
    if (
        "max_completion_tokens" in text
        or "max_tokens" in text
        or (
            "parameter" in text
            and any(
                marker in text
                for marker in (
                    "unsupported",
                    "unrecognized",
                    "unknown",
                    "not allowed",
                    "not support",
                    "invalid",
                )
            )
        )
    ):
        return "parameter_incompatible"
    return "invalid_request"


def _decode_success_payload(
    plan: ProbeRequestPlan,
    raw: bytes,
) -> Any:
    # Check the **actual response body shape**, not the contract version. In the 2026-08-17 incident, chat-v2 made
    # `openai_compatible` probes send `stream: true`; the provider correctly returned 200 + SSE,
    # but this code previously checked `adapter_contract_version == "openai_chat_completions_sse"`, a value
    # exclusive to azure / custom_http declarative contracts. `openai_compatible` uses
    # `openai-compatible-v1`, so `json.loads` consumed the SSE body and classified the successful probe as
    # `invalid_response`. Request and decoding shapes **must share a source**, or changing only one creates a latent failure.
    #
    # JSON bodies cannot start with `data:` (only `{` / `[`), so this check does not broaden false acceptance;
    # the reverse still holds: if streaming was requested but the provider returns ordinary JSON, decode it as JSON as before (the existing
    # negative control `test_azure_chat_probe_accepts_a_non_streaming_response` guards this).
    # Broaden SSE detection to all SSE field lines: DashScope Responses streams start with `id:1`,
    # and DeepSeek streams with `event:` (observed in real stream samples). Checking only `data:` feeds the SSE body into
    # json.loads, causing invalid_response. JSON bodies cannot start with these prefixes (only `{`/`[`),
    # so this check does not broaden false acceptance.
    head = raw.lstrip()[:8]
    if not any(head.startswith(prefix) for prefix in (b"data:", b"event:", b"id:", b"retry:", b":")):
        return json.loads(raw)

    text = raw.decode("utf-8")
    # Dispatch by shape, not contract version (the same incident lesson as above): every Responses data frame
    # has `type: "response.*"`; chat streams use `choices`. The first parseable frame determines the family.
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            first = json.loads(data)
        except json.JSONDecodeError:
            break
        if isinstance(first, dict) and isinstance(first.get("type"), str) and first["type"].startswith("response."):
            return _decode_responses_sse(text)
        break
    content: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    model: str | None = None
    usage: Any = None
    terminal_seen = False
    frame_seen = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            terminal_seen = True
            continue
        frame = json.loads(data)
        if not isinstance(frame, dict) or frame.get("error") is not None:
            raise ValueError("invalid SSE response frame")
        frame_seen = True
        if frame.get("model"):
            model = str(frame["model"])
        if frame.get("usage") is not None:
            usage = frame["usage"]
        choices = frame.get("choices") or []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason") is not None:
                terminal_seen = True
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            for tool_call in delta.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                index = int(tool_call.get("index") or 0)
                slot = tool_calls.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tool_call.get("id"):
                    slot["id"] = str(tool_call["id"])
                function = tool_call.get("function") or {}
                if isinstance(function, dict):
                    if function.get("name"):
                        slot["function"]["name"] = str(function["name"])
                    if function.get("arguments"):
                        slot["function"]["arguments"] += str(function["arguments"])
    if not frame_seen or not terminal_seen:
        raise ValueError("SSE response did not reach a terminal frame")
    payload: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": "".join(content),
                    "tool_calls": [tool_calls[index] for index in sorted(tool_calls)],
                }
            }
        ],
    }
    if model is not None:
        payload["model"] = model
    if usage is not None:
        payload["usage"] = usage
    return payload


def _classify_success(
    plan: ProbeRequestPlan,
    payload: Any,
    *,
    status: int,
    duration_ms: int,
    actual_model: str | None,
) -> ProbeResult:
    common = {
        "classification": "factual_evidence",
        "safe_http_status": status,
        "safe_summary_code": None,
        "duration_ms": duration_ms,
        "actual_model_id": actual_model,
    }
    if plan.capability == "tool":
        if not _has_tool_call(plan.transport.adapter_type, payload):
            return ProbeResult(
                outcome="failed",
                code="tool_call_missing",
                **common,  # type: ignore[arg-type]
            )
    elif plan.capability == "embed":
        vector = _first_embedding(payload)
        if vector is None:
            return ProbeResult(
                outcome="failed",
                code="embedding_missing",
                **common,  # type: ignore[arg-type]
            )
        if not all(math.isfinite(value) for value in vector):
            return ProbeResult(
                outcome="failed",
                code="embedding_non_finite",
                **common,  # type: ignore[arg-type]
            )
        return ProbeResult(
            outcome="verified",
            code="ok",
            vector_dimension=len(vector),
            **common,  # type: ignore[arg-type]
        )
    elif not _has_completion(plan.transport.adapter_type, payload):
        return ProbeResult(
            outcome="failed",
            code="empty_completion",
            **common,  # type: ignore[arg-type]
        )
    return ProbeResult(
        outcome="verified",
        code="ok",
        **common,  # type: ignore[arg-type]
    )


def _has_completion(adapter_type: str, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    output = payload.get("output")
    if isinstance(output, list) and not payload.get("choices"):
        # Responses object (check shape; prioritize chat for payloads with choices, review R2:
        # unrelated top-level output must not bypass the chat check): a message item has nonempty output_text, or a
        # function_call was emitted (chat also counts tool_calls as completion, keeping semantics aligned).
        return any(
            (
                item.get("type") == "function_call"
                or (
                    item.get("type") == "message"
                    and any(
                        isinstance(block, dict) and block.get("type") == "output_text" and str(block.get("text") or "").strip()
                        for block in (item.get("content") or [])
                    )
                )
            )
            for item in output
            if isinstance(item, dict)
        )
    if adapter_type in {"openai_compatible", "azure_openai", "custom_http"}:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        return isinstance(message, dict) and bool(str(message.get("content") or "").strip() or message.get("tool_calls"))
    content = payload.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and ((block.get("type") == "text" and bool(str(block.get("text") or "").strip())) or block.get("type") == "tool_use")
        for block in content
    )


def _has_tool_call(adapter_type: str, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    output = payload.get("output")
    if isinstance(output, list) and not payload.get("choices"):
        return any(isinstance(item, dict) and item.get("type") == "function_call" for item in output)
    if adapter_type in {"openai_compatible", "azure_openai", "custom_http"}:
        choices = payload.get("choices")
        return isinstance(choices, list) and any(
            isinstance(choice, dict) and isinstance(choice.get("message"), dict) and bool(choice["message"].get("tool_calls")) for choice in choices
        )
    content = payload.get("content")
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)


def _first_embedding(payload: Any) -> list[float] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None
    embedding = data[0].get("embedding") if isinstance(data[0], dict) else None
    if not isinstance(embedding, list) or not embedding or any(not isinstance(value, int | float) or isinstance(value, bool) for value in embedding):
        return None
    return [float(value) for value in embedding]


def _apply_model_body_overrides(
    provider: Any,
    body: dict[str, Any],
    *,
    model_id: str,
    capability: ModelCapability,
    responses_surface: bool = False,
) -> None:
    """按 capability 合并模型级 `request_body_overrides`（与运行时 `_build_body` 同序同保护集）。"""

    models = provider.get("models", []) if isinstance(provider, dict) else getattr(provider, "models", [])
    for model in models or []:
        if not isinstance(model, dict) or str(model.get("id") or "") != model_id:
            continue
        overrides = model.get("request_body_overrides")
        capability_overrides = overrides.get(capability) if isinstance(overrides, dict) else None
        if not isinstance(capability_overrides, dict):
            return
        protected = (
            # store is protected: overrides must not change the invariant store:false back to true
            # (review R4; relax this when batch 3 adds previous_response_id).
            {"model", "input", "instructions", "stream", "store", "tools", "tool_choice"}
            if responses_surface
            else {"model", "messages", "stream", "tools", "tool_choice"}
        )
        family: ProtocolFamily = "openai_responses" if responses_surface else "openai"
        for key, value in capability_overrides.items():
            if key not in protected:
                # Translate chat semantic keys (review R1-P1-1): existing overrides use max_tokens; after a surface switch,
                # leaving it untranslated produces both keys and makes adaptive retries remove the wrong one. Identity mapping on chat.
                body[wire_param_name(key, family)] = value
        return


def _deployment_for(provider: Any, model_id: str) -> str:
    models = provider.get("models", []) if isinstance(provider, dict) else getattr(provider, "models", [])
    for model in models or []:
        if isinstance(model, dict) and str(model.get("id") or "") == model_id:
            return str(model.get("deployment") or model_id)
    return model_id
