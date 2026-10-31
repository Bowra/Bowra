"""Finite projection from Router failures to user-safe Chat failure facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from bowhead_router.errors import (
    InvocationTargetsExhaustedError,
    LLMTargetFailure,
    ProviderFailureKind,
    RouteTargetsExhaustedError,
    StreamingInterruptedError,
    StreamingTargetsExhaustedError,
    sanitize_upstream_text,
)

LLM_REASON_CODES = frozenset(
    {
        "llm_provider_auth_failed",
        "llm_provider_rate_limited",
        "llm_provider_timeout",
        "llm_provider_unavailable",
        "llm_request_rejected",
        # Context-engineering sprint batch 4-c. ⚠ This is an **allowlist**: published_diagnostics calls
        # _drop("chat_llm_failed", "parent_code") for reason_codes outside it, leaving generic user-facing failure;
        # a new reason code omitted here is effectively unpublished (first version missed this point).
        "llm_context_overflow",
        "llm_response_invalid",
        "llm_stream_interrupted",
        # 116 dogfood 2026-09-24: 10/10 "LLM failures" over nearly 90 minutes were **our own**
        # per-stream deadlines (`asyncio.wait_for(_drain(), timeout=llm_timeout)`, default 300s),
        # with zero actual provider failures. Previously reported as `llm_stream_interrupted`
        # ("model answer transport interrupted; restart this turn"), but identical retries inevitably hit the same deadline.
        "llm_stream_deadline_exceeded",
    }
)

#: `LLMTargetFailure.provider` sentinel: timeout is **our own** per-stream deadline,
#: not provider timeout or remote stream termination. Raised in `subagent._stream_invoke`.
#:
#: ⚠ Criterion recognizes only this constant, **without `kind is TIMEOUT` conjunction**: raise site always pairs TIMEOUT,
#: so that conjunction is always true and untestable (mutation survived); if ever false, behavior
#: silently returns to generic classification, exactly the bug being fixed. Sentinel present = our deadline expired.
#: Conversely, `kind is TIMEOUT` alone is insufficient: real provider timeouts are TIMEOUT too,
#: where "retry later" is correct; waiting never resolves our deadline.
ORCHESTRATOR_DEADLINE_PROVIDER = "orchestrator_deadline"

_PROVIDER_FAMILIES = frozenset(
    {
        "anthropic_compatible",
        "custom",
        "mock",
        "openai_compatible",
    }
)


# Per-field cap for provider-identified fields in safe_detail. Total strategy length is bounded at 360;
# an excessive provider type name must not displace those bounded facts.
_UPSTREAM_FIELD_MAX = 48
# Shapes allowed in **user-visible diagnostics**: identifiers only, including subscripts.
#
# ⚠ Stricter than Router `safe_upstream_type`: different purpose, not duplication. Its grammar serves
# logs, allowing spaces and `/`, so "see https://…/leak" passes, while
# published_diagnostics content checks reject the **whole diagnostic** as unsafe_detail upon seeing URLs,
# losing safe phase / http_status facts too. This occurred while writing the test.
# Disclosure therefore checks independently: nonidentifiers omit only that field; publish the diagnostic normally (degrade, never erase).
_DISCLOSABLE_UPSTREAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\[\]:-]{0,63}$")


def _disclosable(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate[:_UPSTREAM_FIELD_MAX] if _DISCLOSABLE_UPSTREAM_RE.fullmatch(candidate) else None


@dataclass(frozen=True, slots=True)
class LLMFailureProjection:
    reason_code: str
    phase: Literal["stream", "invoke"]
    failure_class: str
    provider_family: str
    http_status: int | None
    stream_state: Literal["not_started", "pre_visible", "interrupted"]
    fallback_state: Literal["not_attempted", "succeeded", "failed"]
    attempts: int
    retryable: bool
    # Provider-identified failure point. **Only bounded-grammar identifiers** (Router
    # `safe_upstream_type` / `safe_upstream_id` / `safe_upstream_param` already check them).
    # The message is screened again by the publication boundary before disclosure.
    upstream_error_type: str | None = None
    upstream_error_code: str | None = None
    upstream_error_param: str | None = None
    upstream_message: str | None = None
    api_surface: str | None = None
    outcome_unknown: bool = False

    @property
    def safe_detail(self) -> str:
        status = str(self.http_status) if self.http_status is not None else "none"
        detail = (
            f"phase={self.phase}; failure_class={self.failure_class}; "
            f"provider_family={self.provider_family}; http_status={status}; "
            f"stream_state={self.stream_state}; fallback={self.fallback_state}; "
            f"attempts={self.attempts}"
        )
        if self.api_surface in {"responses", "chat_completions", "messages"}:
            detail += f"; api_surface={self.api_surface}"
        if self.outcome_unknown:
            detail += "; outcome=unknown"
        # State fields only when present; avoid noise like `upstream_param=none`.
        # Readers must see no distinction between "field absent" and "provider did not identify it".
        for key, value in (
            ("upstream_type", self.upstream_error_type),
            ("upstream_code", self.upstream_error_code),
            ("upstream_param", self.upstream_error_param),
        ):
            if value:
                detail += f"; {key}={value[:_UPSTREAM_FIELD_MAX]}"
        return detail


class LLMDeliveryFailedError(RuntimeError):
    """Streaming failed safely, then the single-attempt invoke fallback failed."""

    def __init__(
        self,
        *,
        stream_error: StreamingTargetsExhaustedError,
        invoke_error: BaseException,
    ) -> None:
        self.stream_error = stream_error
        self.invoke_error = invoke_error
        super().__init__("LLM stream and invoke fallback failed")


def target_failures(exc: BaseException) -> tuple[LLMTargetFailure, ...]:
    if isinstance(exc, LLMDeliveryFailedError):
        return (*exc.stream_error.failures, *target_failures(exc.invoke_error))
    if isinstance(exc, RouteTargetsExhaustedError):
        return exc.failures
    return ()


def _pick_terminal(failures: tuple[LLMTargetFailure, ...]) -> LLMTargetFailure | None:
    if not failures:
        return None
    last = failures[-1]
    # Context-engineering batch 4-c review fix: primary fails for another reason, fallback overflows its smaller window; terminal state must not be fallback overflow,
    # which directs users to a new session despite primary capacity. Fallback overflow counts only when **all** targets overflow.
    if last.kind is ProviderFailureKind.CONTEXT_OVERFLOW and last.target_index > 1:
        non_overflow = [f for f in failures if f.kind is not ProviderFailureKind.CONTEXT_OVERFLOW]
        if non_overflow:
            return non_overflow[-1]
    return last


def _terminal_failure(exc: BaseException) -> LLMTargetFailure | None:
    if isinstance(exc, LLMDeliveryFailedError):
        return _pick_terminal(target_failures(exc.invoke_error))
    return _pick_terminal(target_failures(exc))


def _reason_for(
    exc: BaseException,
    failure: LLMTargetFailure,
) -> tuple[str, str]:
    # ★★ Must precede `StreamingInterruptedError`. Originally exception-type short-circuit on the first line
    # ignored `failure`, reporting our own deadline (wrapped as StreamingInterruptedError
    # because visible content was already emitted) as "transport interrupted". Observed 10/10 on 116.
    if failure.provider == ORCHESTRATOR_DEADLINE_PROVIDER:
        return "llm_stream_deadline_exceeded", "timeout"
    if isinstance(exc, StreamingInterruptedError):
        return "llm_stream_interrupted", "interrupted"
    match failure.kind:
        case ProviderFailureKind.AUTH | ProviderFailureKind.PERMISSION:
            return "llm_provider_auth_failed", "access"
        case ProviderFailureKind.RATE_LIMITED:
            return "llm_provider_rate_limited", "rate_limit"
        case ProviderFailureKind.TIMEOUT:
            return "llm_provider_timeout", "timeout"
        case ProviderFailureKind.REQUEST_REJECTED | ProviderFailureKind.CONFIGURATION:
            return "llm_request_rejected", "request"
        case ProviderFailureKind.CONTEXT_OVERFLOW:
            # Batch 4-c: overflow is not "model configuration problem"; directing users to admins is wrong, so give it separate wording.
            return "llm_context_overflow", "request"
        case ProviderFailureKind.RESPONSE_INVALID:
            return "llm_response_invalid", "response"
        case ProviderFailureKind.NETWORK:
            return "llm_provider_unavailable", "network"
        case ProviderFailureKind.UPSTREAM | ProviderFailureKind.UNKNOWN:
            return "llm_provider_unavailable", "upstream"


def project_llm_failure(
    exc: BaseException,
    *,
    fallback_state: Literal["not_attempted", "succeeded", "failed"] | None = None,
) -> LLMFailureProjection | None:
    """Project typed Router failures; arbitrary exception prose is rejected."""

    from bowhead_router.output_budget import ContextCapacityError

    if isinstance(exc, ContextCapacityError):
        return LLMFailureProjection(
            reason_code="llm_context_overflow",
            phase="invoke",
            failure_class="request",
            provider_family="custom",
            http_status=None,
            stream_state="not_started",
            fallback_state=fallback_state or "not_attempted",
            attempts=0,
            retryable=False,
        )

    failure = _terminal_failure(exc)
    if failure is None:
        return None
    reason_code, failure_class = _reason_for(exc, failure)
    is_interrupted = isinstance(exc, StreamingInterruptedError)
    if isinstance(exc, LLMDeliveryFailedError):
        phase: Literal["stream", "invoke"] = "invoke"
        stream_state: Literal["not_started", "pre_visible", "interrupted"] = "pre_visible"
        resolved_fallback = "failed"
    elif isinstance(exc, StreamingTargetsExhaustedError):
        phase = "stream"
        stream_state = "pre_visible"
        resolved_fallback = fallback_state or "not_attempted"
    elif is_interrupted:
        phase = "stream"
        stream_state = "interrupted"
        resolved_fallback = "not_attempted"
    elif isinstance(exc, InvocationTargetsExhaustedError):
        phase = "invoke"
        stream_state = "not_started"
        resolved_fallback = fallback_state or "not_attempted"
    else:
        return None
    family = failure.provider_family if failure.provider_family in _PROVIDER_FAMILIES else "custom"
    return LLMFailureProjection(
        reason_code=reason_code,
        phase=phase,
        failure_class=failure_class,
        provider_family=family,
        http_status=failure.status_code,
        stream_state=stream_state,
        fallback_state=resolved_fallback,
        attempts=len(target_failures(exc)),
        retryable=failure.retryable,
        # Check again, **more strictly than Router** (see `_disclosable`): these three fields enter user-visible
        # diagnostics; an invalid shape must omit only its field, never cause the whole diagnostic to be dropped.
        upstream_error_type=_disclosable(failure.upstream_error_type),
        upstream_error_code=_disclosable(failure.upstream_error_code),
        upstream_error_param=_disclosable(failure.upstream_error_param),
        upstream_message=sanitize_upstream_text(failure.upstream_message),
        api_surface=failure.api_surface,
    )


__all__ = [
    "LLM_REASON_CODES",
    "LLMDeliveryFailedError",
    "LLMFailureProjection",
    "project_llm_failure",
    "target_failures",
]
