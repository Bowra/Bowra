"""Internal admission before each router attempt enters a provider adapter.

Admission failure is a local decision, not a provider failure: never retry it or
fall through to another provider without the required input/authority contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from bowhead_router.types import LLMRequest


class LLMInputAdmissionError(ValueError):
    """Finite local rejection; contains no input, credentials or provider error."""


class LLMInputAdmission(Protocol):
    async def validate(self, request: LLMRequest, *, provider: str, model: str, context_window: int | None) -> None: ...

    def validate_outbound(self, request: LLMRequest, body: dict[str, Any], *, family: str) -> None: ...

    async def validate_dispatch(self, request: LLMRequest, body: dict[str, Any], *, provider: str, model: str, family: str) -> object | None: ...


async def admit_request_input(request: LLMRequest, *, provider: str, model: str, context_window: int | None, adapter_supported: bool = False) -> None:
    request.input_admission_window = context_window
    admission = request.input_admission
    if admission is None:
        return
    if not adapter_supported:
        raise LLMInputAdmissionError("llm_input_admission_adapter_unavailable")
    try:
        await admission.validate(request, provider=provider, model=model, context_window=context_window)
    except LLMInputAdmissionError:
        raise
    except Exception:
        raise LLMInputAdmissionError("llm_input_admission_failed") from None


def admit_outbound_input(request: LLMRequest, body: dict[str, Any], *, family: str) -> None:
    """Adapters call after final overrides, including each self-heal rebuild."""
    if request.input_admission is None:
        return
    try:
        request.input_admission.validate_outbound(request, body, family=family)
    except LLMInputAdmissionError:
        raise
    except Exception:
        raise LLMInputAdmissionError("llm_input_admission_failed") from None


async def admit_dispatch_input(request: LLMRequest, body: dict[str, Any], *, provider: str, model: str, family: str | None) -> object | None:
    """Await current authority before any physical invocation ticket or send."""
    if request.input_admission is None:
        return None
    try:
        if not family:
            raise LLMInputAdmissionError("llm_input_admission_adapter_unavailable")
        return await request.input_admission.validate_dispatch(request, body, provider=provider, model=model, family=family)
    except LLMInputAdmissionError:
        raise
    except Exception:
        raise LLMInputAdmissionError("llm_input_admission_failed") from None
