"""Context-scoped admission and reporting for Router retries.

The Router owns retry counts and backoff. A caller may install an observer that
decides whether a scheduled retry still fits its own deadline and reports it to
the user. Without an observer every retry inside the policy proceeds.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from bowhead_router.errors import ProviderFailureKind

DEFAULT_MAX_RETRIES = 3
MAX_RETRIES_LIMIT = 5


@dataclass(frozen=True, slots=True)
class RetryNotice:
    """One retry the Router is about to schedule for a logical call."""

    retry: int
    max_retries: int
    kind: ProviderFailureKind
    delay_s: float
    mode: str
    provider: str
    model: str
    # The adapter's transport timeout: the longest one more attempt can wait.
    attempt_timeout_s: float | None = None


class RetryObserver(Protocol):
    async def admit_retry(self, notice: RetryNotice) -> bool: ...

    def attempt_fits(self, *, delay_s: float, attempt_timeout_s: float | None) -> bool: ...


_CURRENT_RETRY_OBSERVER: ContextVar[RetryObserver | None] = ContextVar("bowhead_retry_observer", default=None)


@contextmanager
def retry_observer_scope(observer: RetryObserver | None) -> Iterator[None]:
    token = _CURRENT_RETRY_OBSERVER.set(observer)
    try:
        yield
    finally:
        _CURRENT_RETRY_OBSERVER.reset(token)


async def admit_retry(notice: RetryNotice) -> bool:
    observer = _CURRENT_RETRY_OBSERVER.get()
    return True if observer is None else await observer.admit_retry(notice)


def attempt_fits(*, delay_s: float = 0.0, attempt_timeout_s: float | None = None) -> bool:
    """Whether one more attempt of the current call may still start."""
    observer = _CURRENT_RETRY_OBSERVER.get()
    return True if observer is None else observer.attempt_fits(delay_s=delay_s, attempt_timeout_s=attempt_timeout_s)


def adapter_timeout(adapter: object) -> float | None:
    value = getattr(adapter, "timeout_sec", None)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value > 0 else None


def configured_max_retries(config: object) -> int:
    """Read the model configuration's single retry setting."""
    retry = config.get("retry") if isinstance(config, dict) else None
    value = retry.get("max_retries") if isinstance(retry, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_RETRIES_LIMIT:
        return value
    return DEFAULT_MAX_RETRIES
