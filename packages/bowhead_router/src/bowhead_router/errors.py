"""Typed, bounded LLM provider failures shared by adapters and the Router.

Exception strings are deliberately safe and low-cardinality. Provider response
details live in structured fields. Only a bounded, screened provider message may
cross into authenticated user-visible diagnostics.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_KEY_SHAPE_RE = re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9._-]{6,}")
_SECRET_FIELD_RE = re.compile(
    r"(?i)(authorization|auth[-_ ]?key|api[-_ ]?key|access[-_ ]?token|token|secret|password)"
    r"([\"'\s:=]+)[^\s\"',&}]+",
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,79}$")
# The **failing parameter name** identified by the provider: `temperature` / `max_tokens` / `messages[0].content`.
# Accept bracket subscripts in addition to _SAFE_ID_RE, but still reject spaces, quotes, angle brackets, and slashes. To pass
# the published_diagnostics boundary (which rejects URLs / paths / markup), restrict the grammar to identifiers + subscripts.
_SAFE_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]-]{0,63}$")
_MAX_UPSTREAM_MESSAGE = 500


class ProviderFailureKind(StrEnum):
    """Finite provider failure taxonomy used for retry and UI projection."""

    AUTH = "auth"
    PERMISSION = "permission"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK = "network"
    UPSTREAM = "upstream"
    REQUEST_REJECTED = "request_rejected"
    RESPONSE_INVALID = "response_invalid"
    CONFIGURATION = "configuration"
    # Context engineering sprint batch 4-c: the request exceeds the model context window. Recognize only structured codes (see
    # CONTEXT_OVERFLOW_CODES below), not prose. Anthropic errors that only say
    # "prompt is too long" in message remain REQUEST_REJECTED; the pre-request window guard covers them.
    CONTEXT_OVERFLOW = "context_overflow"
    UNKNOWN = "unknown"


# Known upstream **structured code** spellings for context overflow (OpenAI-compatible surface: DeepSeek / Qwen-compat /
# Doubao use `code: context_length_exceeded`). Normalize as in failure_kind_for_error_payload:
# lowercase, replacing `-`/spaces with `_`. Never recognize prose.
CONTEXT_OVERFLOW_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "max_context_length_exceeded",
        "prompt_too_long",
    }
)


# Three upstream **fixed templates** (machine-generated, with numbers) for context overflow. These are anchored whole-sentence templates, not free prose;
# accept only full-template matches and extract the numbers (review fix: on 2026-09-05, no tested fleet provider used a structured code for overflow):
#   · OpenAI family template; **observed** on the DeepSeek Anthropic surface (116 production path):
#     "This model's maximum context length is 1048576 tokens. However, you requested 3600023 tokens (…)"
#   · DashScope compat **observed** (qwen-max, window 30720): "<400> InternalError.Algo.InvalidParameter: Range of input length should be [1, 30720]"
#   · Anthropic official documentation template (not observed): "prompt is too long: 210000 tokens > 200000 maximum"
#   · Doubao / Ark: **not observed, not covered**.
_CONTEXT_OVERFLOW_TEMPLATES: tuple[tuple[re.Pattern[str], int | None, int | None], ...] = (
    # (pattern, group index of maximum, group index of requested)
    (re.compile(r"maximum context length is (\d+) tokens\. However, you requested (\d+) tokens", re.IGNORECASE), 1, 2),
    # The second OpenAI template when max_tokens is absent: "…However, your messages resulted in N tokens"
    (re.compile(r"maximum context length is (\d+) tokens\. However, your messages resulted in (\d+) tokens", re.IGNORECASE), 1, 2),
    (re.compile(r"Range of input length should be \[1, (\d+)\]", re.IGNORECASE), 1, None),
    (re.compile(r"prompt is too long: (\d+) tokens > (\d+) maximum", re.IGNORECASE), 2, 1),
)


def context_overflow_from_message(message: object) -> tuple[int | None, int | None] | None:
    """若 ``message`` 命中已知超窗模板，返回 ``(maximum, requested)``（缺的位置为 None）；否则 None。"""
    if not isinstance(message, str) or not message:
        return None
    for pattern, gi_max, gi_req in _CONTEXT_OVERFLOW_TEMPLATES:
        m = pattern.search(message)
        if not m:
            continue
        maximum = int(m.group(gi_max)) if gi_max else None
        requested = int(m.group(gi_req)) if gi_req else None
        return (maximum, requested)
    return None


def _normalize_code_token(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def refine_failure_kind(
    kind: ProviderFailureKind,
    *,
    upstream_error_code: str | None,
    upstream_error_type: str | None = None,
    upstream_message: str | None = None,
) -> ProviderFailureKind:
    """HTTP 层按状态码分类之后，把 400 里的「超窗」挑出来：先看结构化 code，再看锚定的消息模板。其余原样返回。"""
    if kind is not ProviderFailureKind.REQUEST_REJECTED:
        return kind
    tokens = {_normalize_code_token(upstream_error_code), _normalize_code_token(upstream_error_type)}
    if tokens & CONTEXT_OVERFLOW_CODES:
        return ProviderFailureKind.CONTEXT_OVERFLOW
    if context_overflow_from_message(upstream_message) is not None:
        return ProviderFailureKind.CONTEXT_OVERFLOW
    return kind


class InvocationOutcomeUnknownError(RuntimeError):
    """A physical provider dispatch may have applied and must never be retried.

    This sentinel deliberately lives below Core so every Router retry/fallback
    boundary can preserve it without introducing a reverse dependency.
    """

    def __init__(self, reason_code: str = "provider_outcome_unknown") -> None:
        self.reason_code = reason_code if isinstance(reason_code, str) and reason_code else "provider_outcome_unknown"
        super().__init__("physical provider invocation outcome is unknown")


_STREAM_FALLBACK_ELIGIBLE_KINDS = frozenset(
    {
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.TIMEOUT,
        ProviderFailureKind.NETWORK,
        ProviderFailureKind.UPSTREAM,
        ProviderFailureKind.RESPONSE_INVALID,
    }
)


def sanitize_upstream_text(value: object, *, max_length: int = _MAX_UPSTREAM_MESSAGE) -> str | None:
    """Return a bounded credential-scrubbed log field, never a UI message."""

    if not isinstance(value, str):
        return None
    clean = _CONTROL_RE.sub(" ", value)
    clean = _BEARER_RE.sub("Bearer [REDACTED]", clean)
    clean = _KEY_SHAPE_RE.sub("[REDACTED_KEY]", clean)
    clean = _SECRET_FIELD_RE.sub(r"\1\2[REDACTED]", clean)
    clean = " ".join(clean.split()).strip()
    return clean[:max_length] or None


def safe_upstream_id(value: object) -> str | None:
    """Accept only IDs with a conservative non-secret grammar."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_ID_RE.fullmatch(candidate) else None


def safe_upstream_type(value: object) -> str | None:
    """Keep a short provider error class, including benign names with spaces."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    scrubbed = sanitize_upstream_text(candidate, max_length=80)
    if scrubbed != candidate:
        return None
    return candidate if _SAFE_TYPE_RE.fullmatch(candidate) else None


def safe_upstream_param(value: object) -> str | None:
    """Accept only a parameter *name* — the one field that names the culprit.

    OpenAI 兼容错误体是结构化的：`{"error": {"message", "type", "param", "code"}}`。
    `param` 直接点名被拒的请求字段（`temperature` / `max_tokens` /
    `parallel_tool_calls`），`code` 是短标识（`unsupported_value` / `unknown_parameter`）
    —— 二者都是**有限文法的标识符**，与 `message` 那种自由散文不是一回事，因此可以穿过
    用户可见的诊断边界，而散文不行。

    这两个字段此前被 `upstream_error_fields_from_payload` 直接丢在地上：400 明明自带
    「是哪个参数」的答案，我们却只能回去翻服务器日志里的 message。
    """

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_PARAM_RE.fullmatch(candidate) else None


class ProviderCallError(Exception):
    """One adapter call failure with finite classification and bounded evidence."""

    def __init__(
        self,
        message: str | None = None,
        *,
        kind: ProviderFailureKind = ProviderFailureKind.UNKNOWN,
        retryable: bool = False,
        status_code: int | None = None,
        provider_family: str | None = None,
        upstream_request_id: str | None = None,
        upstream_error_id: str | None = None,
        upstream_error_type: str | None = None,
        upstream_error_code: str | None = None,
        upstream_error_param: str | None = None,
        upstream_message: str | None = None,
        api_surface: str | None = None,
    ) -> None:
        self.api_surface = api_surface if api_surface in {"responses", "chat_completions", "messages"} else None
        self.kind = kind
        self.retryable = retryable
        self.status_code = status_code
        self.provider_family = provider_family
        self.upstream_request_id = safe_upstream_id(upstream_request_id)
        self.upstream_error_id = safe_upstream_id(upstream_error_id)
        self.upstream_error_type = safe_upstream_type(upstream_error_type)
        self.upstream_error_code = safe_upstream_id(upstream_error_code)
        self.upstream_error_param = safe_upstream_param(upstream_error_param)
        self.upstream_message = sanitize_upstream_text(upstream_message or message)
        # Batch 4-c: all adapter HTTP errors pass here, centralizing "400 + structured code = context overflow" classification.
        self.kind = refine_failure_kind(
            self.kind,
            upstream_error_code=self.upstream_error_code,
            upstream_error_type=self.upstream_error_type,
            upstream_message=self.upstream_message,
        )
        super().__init__(self._safe_summary())

    def _safe_summary(self) -> str:
        status = f" status={self.status_code}" if self.status_code is not None else ""
        return f"provider call failed kind={self.kind.value}{status}"


def provider_failure_kind(exc: BaseException) -> ProviderFailureKind:
    """Classify typed and legacy adapter errors without parsing their prose."""

    kind = getattr(exc, "kind", None)
    if isinstance(kind, ProviderFailureKind):
        return kind
    if isinstance(exc, TimeoutError):
        return ProviderFailureKind.TIMEOUT
    status = getattr(exc, "status_code", None)
    if status == 401:
        return ProviderFailureKind.AUTH
    if status == 403:
        return ProviderFailureKind.PERMISSION
    if status == 429:
        return ProviderFailureKind.RATE_LIMITED
    if isinstance(status, int) and status >= 500:
        return ProviderFailureKind.UPSTREAM
    if isinstance(status, int) and status >= 400:
        return ProviderFailureKind.REQUEST_REJECTED
    return ProviderFailureKind.UNKNOWN


def failure_kind_for_status(status_code: int) -> ProviderFailureKind:
    if status_code == 401:
        return ProviderFailureKind.AUTH
    if status_code == 403:
        return ProviderFailureKind.PERMISSION
    if status_code == 429:
        return ProviderFailureKind.RATE_LIMITED
    if status_code in {408, 504}:
        return ProviderFailureKind.TIMEOUT
    if status_code >= 500:
        return ProviderFailureKind.UPSTREAM
    return ProviderFailureKind.REQUEST_REJECTED


def failure_kind_for_error_payload(payload: Any) -> ProviderFailureKind:
    """Classify a structured in-band provider error without parsing prose."""

    error = payload.get("error") if isinstance(payload, dict) else None
    detail = error if isinstance(error, dict) else payload if isinstance(payload, dict) else {}
    status = detail.get("status_code") or detail.get("status")
    if isinstance(status, int):
        return refine_failure_kind(
            failure_kind_for_status(status),
            upstream_error_code=str(detail.get("code") or "") or None,
            upstream_error_type=str(detail.get("type") or "") or None,
            upstream_message=str(detail.get("message") or "") or None,
        )

    tokens = {str(detail.get(name) or "").strip().lower().replace("-", "_").replace(" ", "_") for name in ("type", "code")}
    if tokens & {"authentication_error", "invalid_api_key", "unauthorized"}:
        return ProviderFailureKind.AUTH
    if tokens & {"billing_error", "forbidden", "permission_error"}:
        return ProviderFailureKind.PERMISSION
    if tokens & {"rate_limit_error", "rate_limit_exceeded", "too_many_requests"}:
        return ProviderFailureKind.RATE_LIMITED
    if tokens & {"request_timeout", "timeout_error"}:
        return ProviderFailureKind.TIMEOUT
    if tokens & CONTEXT_OVERFLOW_CODES or context_overflow_from_message(detail.get("message")) is not None:
        return ProviderFailureKind.CONTEXT_OVERFLOW
    if tokens & {
        "bad_request",
        "invalid_request_error",
        "not_found_error",
        "request_too_large",
        "unsupported_parameter",
    }:
        return ProviderFailureKind.REQUEST_REJECTED
    return ProviderFailureKind.UPSTREAM


def stream_failure_kind_allows_fallback(kind: ProviderFailureKind) -> bool:
    return kind in _STREAM_FALLBACK_ELIGIBLE_KINDS


def stream_error_allows_invoke_fallback(exc: BaseException) -> bool:
    """Mirror ``StreamingTargetsExhaustedError.safe_to_fallback`` for probes."""

    return stream_failure_kind_allows_fallback(provider_failure_kind(exc))


def upstream_error_fields_from_payload(
    payload: Any,
    *,
    headers: Any = None,
) -> dict[str, str | None]:
    """Extract bounded evidence from a structured HTTP or SSE error payload."""

    error = payload.get("error") if isinstance(payload, dict) else None
    detail = error if isinstance(error, dict) else payload if isinstance(payload, dict) else {}
    header_values = headers or {}
    request_id = None
    for name in ("x-request-id", "request-id", "x-correlation-id", "x-ms-request-id"):
        request_id = safe_upstream_id(header_values.get(name))
        if request_id:
            break
    message = detail.get("message")
    if message is None and isinstance(error, str):
        message = error
    return {
        "upstream_request_id": request_id,
        "upstream_error_id": safe_upstream_id(detail.get("id")),
        "upstream_error_type": safe_upstream_type(detail.get("type")),
        "upstream_error_code": safe_upstream_id(detail.get("code")),
        "upstream_error_param": safe_upstream_param(detail.get("param")),
        "upstream_message": sanitize_upstream_text(message),
    }


def upstream_error_fields(response: Any) -> dict[str, str | None]:
    """Extract only bounded allowlisted provider evidence from an HTTP response."""

    payload: Any = None
    with contextlib.suppress(TypeError, ValueError):
        payload = response.json()
    return upstream_error_fields_from_payload(
        payload,
        headers=getattr(response, "headers", {}),
    )


def provider_family(adapter: Any) -> str:
    """Return a stable protocol family without exposing configured aliases."""

    explicit = getattr(adapter, "provider_family", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    name = type(adapter).__name__.lower()
    if "anthropic" in name:
        return "anthropic_compatible"
    if "openai" in name:
        return "openai_compatible"
    if "mock" in name:
        return "mock"
    return "custom"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Router-owned retry budget for one logical call."""

    max_attempts_per_target: int = 2
    base_delay_s: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts_per_target < 1:
            raise ValueError("max_attempts_per_target must be >= 1")
        if self.base_delay_s < 0:
            raise ValueError("base_delay_s must be >= 0")


@dataclass(frozen=True, slots=True)
class LLMTargetFailure:
    provider: str
    model: str
    provider_family: str
    mode: str
    target_index: int
    attempt: int
    kind: ProviderFailureKind
    retryable: bool
    status_code: int | None = None
    upstream_request_id: str | None = None
    upstream_error_id: str | None = None
    upstream_error_type: str | None = None
    upstream_error_code: str | None = None
    upstream_error_param: str | None = None
    upstream_message: str | None = None
    first_visible_chunk_emitted: bool = False
    api_surface: str | None = None


def target_failure(
    exc: BaseException,
    *,
    adapter: Any,
    provider: str,
    model: str,
    mode: str,
    target_index: int,
    attempt: int,
    first_visible_chunk_emitted: bool = False,
) -> LLMTargetFailure:
    """Project an arbitrary exception into the finite Router failure record."""

    return LLMTargetFailure(
        provider=provider,
        model=model,
        provider_family=provider_family(adapter),
        mode=mode,
        target_index=target_index,
        attempt=attempt,
        kind=provider_failure_kind(exc),
        retryable=bool(getattr(exc, "retryable", isinstance(exc, TimeoutError))),
        status_code=getattr(exc, "status_code", None),
        upstream_request_id=safe_upstream_id(getattr(exc, "upstream_request_id", None)),
        upstream_error_id=safe_upstream_id(getattr(exc, "upstream_error_id", None)),
        upstream_error_type=safe_upstream_type(getattr(exc, "upstream_error_type", None)),
        upstream_error_code=safe_upstream_id(getattr(exc, "upstream_error_code", None)),
        upstream_error_param=safe_upstream_param(getattr(exc, "upstream_error_param", None)),
        upstream_message=sanitize_upstream_text(getattr(exc, "upstream_message", None)),
        first_visible_chunk_emitted=first_visible_chunk_emitted,
        api_surface=getattr(exc, "api_surface", None),
    )


class RouteTargetsExhaustedError(RuntimeError):
    """Base class for one logical call whose configured targets all failed."""

    def __init__(self, failures: list[LLMTargetFailure] | tuple[LLMTargetFailure, ...]) -> None:
        self.failures = tuple(failures)
        super().__init__("LLM route targets exhausted")


class InvocationTargetsExhaustedError(RouteTargetsExhaustedError):
    """All non-streaming targets failed."""


class StreamingTargetsExhaustedError(RouteTargetsExhaustedError):
    """All streaming targets failed before any visible output."""

    first_visible_chunk_emitted = False

    @property
    def safe_to_fallback(self) -> bool:
        if not self.failures:
            return False
        return all(failure.attempt > 0 and stream_failure_kind_allows_fallback(failure.kind) for failure in self.failures)


class StreamingInterruptedError(RouteTargetsExhaustedError):
    """A stream failed after content visible to the caller was emitted."""

    first_visible_chunk_emitted = True
    safe_to_fallback = False
