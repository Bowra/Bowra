"""Request-scoped hooks around each physical LLM transport dispatch.

The Router owns the protocol-neutral boundary. Core installs an authority hook
for durable executions and may separately observe physical attempts. Neither
observer identity nor a recorded receipt grants execution authority. Without
either scope the adapter retains its legacy behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Protocol

from bowhead_router.errors import InvocationOutcomeUnknownError, LLMTargetFailure
from bowhead_router.input_admission import LLMInputAdmissionError, admit_dispatch_input
from bowhead_router.request_record import record_request
from bowhead_router.types import LLMRequest, TokenUsage


@dataclass(frozen=True, slots=True)
class PhysicalInvocationSpec:
    provider: str
    model: str
    adapter_contract_key: str
    mode: str
    request_digest: str
    # Opaque server receipt from this dispatch's input admission. No Core import,
    # transport serialization or change to existing Terminal effect identity.
    input_receipt: object | None = field(default=None, repr=False, compare=False)
    credit_rates: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    usage_family: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.provider,
                self.model,
                self.adapter_contract_key,
                self.mode,
                self.request_digest,
            )
        ):
            raise ValueError("physical invocation identity is incomplete")
        if self.mode not in {"invoke", "stream"}:
            raise ValueError("physical invocation mode is invalid")
        if len(self.request_digest) != 64 or any(char not in "0123456789abcdef" for char in self.request_digest):
            raise ValueError("physical invocation request digest is invalid")


class UnknownPhysicalInvocationAttempt(Protocol):
    async def unknown(self, *, reason_code: str) -> None: ...


class PhysicalInvocationAttempt(UnknownPhysicalInvocationAttempt, Protocol):
    async def confirmed_not_applied(
        self,
        *,
        evidence_code: str,
        evidence_digest: str,
    ) -> bool: ...

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool: ...


class PhysicalInvocationObserver(Protocol):
    async def begin(
        self, spec: PhysicalInvocationSpec, *, request: LLMRequest | None, body: Mapping[str, object], family: str | None
    ) -> PhysicalInvocationAttempt: ...


_CURRENT_OBSERVER: ContextVar[PhysicalInvocationObserver | None] = ContextVar("bowhead_physical_invocation_observer", default=None)


@contextmanager
def physical_observation_scope(observer: PhysicalInvocationObserver | None) -> Iterator[None]:
    token = _CURRENT_OBSERVER.set(observer)
    try:
        yield
    finally:
        _CURRENT_OBSERVER.reset(token)


def physical_observation_active() -> bool:
    return _CURRENT_OBSERVER.get() is not None


class _ObservedAttempt:
    """Observe the existing authority attempt without replacing its decisions."""

    def __init__(self, observation: PhysicalInvocationAttempt, authority: PhysicalInvocationAttempt | None):
        self.observation = observation
        self.authority = authority

    async def confirmed_not_applied(self, *, evidence_code: str, evidence_digest: str) -> bool:
        try:
            allowed = self.authority is None or await self.authority.confirmed_not_applied(
                evidence_code=evidence_code, evidence_digest=evidence_digest
            )
        finally:
            recorded = await self.observation.confirmed_not_applied(evidence_code=evidence_code, evidence_digest=evidence_digest)
        return allowed and recorded

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        try:
            allowed = self.authority is None or await self.authority.complete(usage=usage, response_digest=response_digest)
        finally:
            recorded = await self.observation.complete(usage=usage, response_digest=response_digest)
        return allowed and recorded

    async def unknown(self, *, reason_code: str) -> None:
        try:
            if self.authority is not None:
                await self.authority.unknown(reason_code=reason_code)
        finally:
            await self.observation.unknown(reason_code=reason_code)


class PhysicalInvocationHook(Protocol):
    async def begin(self, spec: PhysicalInvocationSpec) -> PhysicalInvocationAttempt: ...


_CURRENT_FAILURE_OBSERVER: ContextVar[Callable[[LLMTargetFailure], Awaitable[None]] | None] = ContextVar(
    "bowhead_physical_failure_observer",
    default=None,
)

_CURRENT_HOOK: ContextVar[PhysicalInvocationHook | None] = ContextVar(
    "bowhead_physical_invocation_hook",
    default=None,
)
_ENCLOSING_GUARDS: ContextVar[tuple[PhysicalInvocationHook, ...]] = ContextVar("bowhead_enclosing_invocation_guards", default=())


def physical_invocation_required() -> bool:
    """Return whether this context requires the durable physical-send contract."""

    return _CURRENT_HOOK.get() is not None or bool(_ENCLOSING_GUARDS.get())


@contextmanager
def enclosing_physical_guard_scope(guard: PhysicalInvocationHook) -> Iterator[None]:
    """Retain parent authority when a nested workflow installs its own hook."""
    current = _ENCLOSING_GUARDS.get()
    token = _ENCLOSING_GUARDS.set(current if any(item is guard for item in current) else (*current, guard))
    try:
        yield
    finally:
        _ENCLOSING_GUARDS.reset(token)


async def _begin_authorities(spec: PhysicalInvocationSpec, hook: PhysicalInvocationHook | None) -> PhysicalInvocationAttempt | None:
    guards = _ENCLOSING_GUARDS.get()
    hooks = (*guards, hook) if hook is not None and not any(item is hook for item in guards) else guards
    attempt = None
    try:
        for item in hooks:
            next_attempt = await item.begin(spec)
            attempt = _ObservedAttempt(attempt, next_attempt) if attempt is not None else next_attempt
    except BaseException:
        if attempt is not None:
            # No transport has run. Keep all parent reservations accounted for
            # even if a child authority denies admission or task setup is cancelled.
            try:
                await asyncio.shield(
                    attempt.confirmed_not_applied(evidence_code="nested_authority_admission_failed/v1", evidence_digest=spec.request_digest)
                )
            except BaseException:
                await abandon_physical_invocation(attempt, reason_code="nested_authority_settlement_unknown")
        raise
    return attempt


def canonical_transport_bytes(value: object) -> bytes:
    """Canonical JSON of secret-free transport semantics; `canonical_transport_digest` hashes these bytes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_transport_digest(value: object) -> str:
    """Hash canonical, secret-free transport semantics without a Core import."""

    return hashlib.sha256(canonical_transport_bytes(value)).hexdigest()


def exact_token_count(value: object) -> int | None:
    """Return a provider count only when it is an exact non-negative integer."""

    return value if type(value) is int and value >= 0 else None


def exact_authoritative_usage(
    raw: Mapping[str, object] | None,
    *,
    input_key: str,
    output_key: str,
    cached_value: object = 0,
) -> TokenUsage | None:
    """Validate provider billing evidence without legacy coercion."""

    if raw is None:
        return None
    input_tokens = exact_token_count(raw.get(input_key))
    output_tokens = exact_token_count(raw.get(output_key))
    cached_tokens = exact_token_count(cached_value)
    if input_tokens is None or output_tokens is None or cached_tokens is None:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
    )


async def begin_physical_invocation(
    *,
    provider: str,
    model: str,
    adapter_contract_key: str,
    mode: str,
    request: Mapping[str, object],
    input_request: LLMRequest | None = None,
    input_family: str | None = None,
) -> PhysicalInvocationAttempt | None:
    # Input authority is required even for a caller without a Terminal hook.
    # A denied local preparation must not reserve a billable invocation ticket.
    input_receipt = None
    if input_request is not None:
        input_receipt = await admit_dispatch_input(input_request, dict(request), provider=provider, model=model, family=input_family)
    canonical_body = canonical_transport_bytes(dict(request))
    request_digest = hashlib.sha256(canonical_body).hexdigest()

    def _record(usage_event_id: str | None) -> None:
        record_request(
            provider=provider,
            model=model,
            family=input_family,
            mode=mode,
            canonical_body=canonical_body,
            request_digest=request_digest,
            usage_event_id=usage_event_id,
            subagent_id=getattr(input_request, "invocation_subagent_id", None),
        )

    hook = _CURRENT_HOOK.get()
    observer = _CURRENT_OBSERVER.get()
    if hook is None and observer is None and not _ENCLOSING_GUARDS.get():
        _record(None)
        return None
    spec = PhysicalInvocationSpec(
        provider=provider,
        model=model,
        adapter_contract_key=adapter_contract_key,
        mode=mode,
        request_digest=request_digest,
        input_receipt=input_receipt,
        credit_rates=input_request.credit_rates if input_request is not None else None,
        usage_family=input_family,
    )
    observation = None
    if observer is not None:
        try:
            observation = await observer.begin(spec, request=input_request, body=request, family=input_family)
            if input_request is not None:
                input_request.physical_receipt_recorded = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # Admission failure is local: neither fallback nor transport may run.
            raise LLMInputAdmissionError("physical_observation_unavailable") from None
    try:
        authority = await _begin_authorities(spec, hook)
    except BaseException:
        if observation is not None:
            await abandon_physical_invocation(observation, reason_code="authority_admission_failed")
        raise
    _record(getattr(observation, "identity", None))
    return _ObservedAttempt(observation, authority) if observation is not None else authority


async def confirm_physical_not_applied(
    attempt: PhysicalInvocationAttempt | None,
    *,
    evidence_code: str,
    evidence: object,
) -> None:
    if attempt is None:
        return
    try:
        allowed = await attempt.confirmed_not_applied(
            evidence_code=evidence_code,
            evidence_digest=canonical_transport_digest(evidence),
        )
    except asyncio.CancelledError:
        await abandon_physical_invocation(
            attempt,
            reason_code="durable_settlement_cancelled",
        )
        raise
    except InvocationOutcomeUnknownError:
        raise
    except Exception as exc:
        await abandon_physical_invocation(
            attempt,
            reason_code="durable_settlement_failed",
        )
        raise InvocationOutcomeUnknownError("durable_settlement_failed") from exc
    if not allowed:
        raise InvocationOutcomeUnknownError("durable_invocation_fenced")


async def confirm_provider_auth_rejection(
    attempt: PhysicalInvocationAttempt | None,
    *,
    status_code: int,
) -> None:
    """Settle an explicit HTTP authentication refusal before propagating it.

    A 401/403 response rejects the request before model execution. Other HTTP
    failures retain unknown-outcome handling unless separately evidenced.
    """
    if status_code not in (401, 403):
        raise ValueError("provider authentication rejection requires HTTP 401 or 403")
    await confirm_physical_not_applied(
        attempt,
        evidence_code="provider_http_auth_rejected/v1",
        evidence={"http_status": status_code},
    )


async def confirm_provider_rate_limit_rejection(
    attempt: PhysicalInvocationAttempt | None,
    *,
    status_code: int,
) -> None:
    """Settle an explicit provider HTTP 429 before propagating its typed error."""
    if status_code != 429:
        raise ValueError("provider rate limit rejection requires HTTP 429")
    await confirm_physical_not_applied(
        attempt,
        evidence_code="provider_http_rate_limited/v1",
        evidence={"http_status": status_code},
    )


def transport_phase(exc: BaseException) -> str:
    """Name the transport step that failed, for settlement evidence."""
    import httpx2

    if isinstance(exc, httpx2.ConnectTimeout | httpx2.ConnectError):
        return "connect"
    if isinstance(exc, httpx2.PoolTimeout):
        return "pool"
    if isinstance(exc, httpx2.WriteTimeout):
        return "write"
    return "read"


async def settle_transport_failure(
    attempt: PhysicalInvocationAttempt | None,
    exc: BaseException,
    *,
    output_started: bool,
) -> None:
    """Settle a timeout or connect failure before any model output was yielded.

    No text, reasoning or tool call reached the caller, so nothing was used or
    shown and, by product decision (2026-10-20), nothing is charged: the attempt
    is recorded as not applied and its typed error may be retried. Metadata
    frames may have arrived and the provider may already have been generating;
    provider-side cost is not observable here in either case. Once any output
    was yielded the outcome stays unknown and is never retried. Other transport
    errors after the request was sent also stay unknown.
    """
    import httpx2

    phase = transport_phase(exc)
    timed_out = isinstance(exc, httpx2.TimeoutException)
    if output_started or not (timed_out or phase == "connect"):
        await fail_physical_invocation(attempt, reason_code="transport_timeout" if timed_out else "transport_network_error")
        return
    await confirm_physical_not_applied(
        attempt,
        evidence_code="provider_no_response_timeout/v1",
        evidence={"phase": phase, "timed_out": timed_out},
    )


async def complete_physical_invocation(
    attempt: PhysicalInvocationAttempt | None,
    *,
    usage: TokenUsage | None,
    response: object,
) -> None:
    if attempt is None:
        return
    if usage is None:
        if isinstance(attempt, _ObservedAttempt) and attempt.authority is None:
            await attempt.unknown(reason_code="authoritative_usage_missing")
            return
        await fail_physical_invocation(attempt, reason_code="authoritative_usage_missing")
    assert usage is not None
    try:
        response_digest = canonical_transport_digest(response)
        allowed = await attempt.complete(
            usage=usage,
            response_digest=response_digest,
        )
    except asyncio.CancelledError:
        await abandon_physical_invocation(
            attempt,
            reason_code="durable_settlement_cancelled",
        )
        raise
    except InvocationOutcomeUnknownError:
        raise
    except Exception as exc:
        await abandon_physical_invocation(
            attempt,
            reason_code="durable_settlement_failed",
        )
        raise InvocationOutcomeUnknownError("durable_settlement_failed") from exc
    if not allowed:
        raise InvocationOutcomeUnknownError("durable_invocation_fenced")


async def fail_physical_invocation(
    attempt: UnknownPhysicalInvocationAttempt | None,
    *,
    reason_code: str,
    failure: LLMTargetFailure | None = None,
) -> None:
    if attempt is None:
        return
    try:
        try:
            observer = _CURRENT_FAILURE_OBSERVER.get()
            if failure is not None and observer is not None:
                await observer(failure)
        finally:
            await attempt.unknown(reason_code=reason_code)
    except asyncio.CancelledError:
        await abandon_physical_invocation(
            attempt,
            reason_code=reason_code,
        )
        raise
    except InvocationOutcomeUnknownError:
        raise
    except Exception as exc:
        raise InvocationOutcomeUnknownError(reason_code) from exc
    raise InvocationOutcomeUnknownError(reason_code)


async def abandon_physical_invocation(
    attempt: UnknownPhysicalInvocationAttempt | None,
    *,
    reason_code: str,
) -> None:
    """Finish the poison/UNKNOWN handoff while preserving caller cancellation."""

    if attempt is None:
        return
    task = asyncio.create_task(attempt.unknown(reason_code=reason_code))
    while True:
        try:
            await asyncio.shield(task)
            return
        except asyncio.CancelledError:
            if not task.done():
                continue
            if task.cancelled():
                return
            with suppress(Exception):
                task.result()
            return
        except Exception:
            return


def set_physical_invocation_hook(
    hook: PhysicalInvocationHook | None,
) -> Token[PhysicalInvocationHook | None]:
    return _CURRENT_HOOK.set(hook)


def reset_physical_invocation_hook(token: Token[PhysicalInvocationHook | None]) -> None:
    _CURRENT_HOOK.reset(token)


@contextmanager
def physical_invocation_scope(
    hook: PhysicalInvocationHook | None,
    *,
    failure_observer: Callable[[LLMTargetFailure], Awaitable[None]] | None = None,
) -> Iterator[None]:
    observer_token = _CURRENT_FAILURE_OBSERVER.set(
        failure_observer if failure_observer is not None or hook is None else _CURRENT_FAILURE_OBSERVER.get()
    )
    token = set_physical_invocation_hook(hook)
    try:
        yield
    finally:
        reset_physical_invocation_hook(token)
        _CURRENT_FAILURE_OBSERVER.reset(observer_token)
