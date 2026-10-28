"""Background Coach review calls (point and suite review) that the client polls.

A reasoning model needs well over 30 s for a review, longer than an HTTP request should be held
open (and than common proxy read timeouts). The route starts the review here and answers
``pending`` after a short wait; the client sends the same request again until it gets the result.
Identical requests share one review. Results live in this process only, briefly: a restart or a
second worker simply starts the review again, which is safe because reviews are advice only.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Same deadline as the other Agent evaluation writer calls (the alignment writer, calibration).
REVIEW_TIMEOUT_SECONDS = 120.0
# How long one request waits for the review before answering ``pending``.
REQUEST_WAIT_SECONDS = 8.0
_RESULT_TTL_SECONDS = 600.0
_MAX_JOBS = 256


def review_key(kind: str, *parts: object) -> str:
    encoded = json.dumps([kind, *parts], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(slots=True)
class _Job:
    task: asyncio.Task[Any]
    finished_at: float | None = None


@dataclass(slots=True)
class ReviewJobs:
    timeout_seconds: float = REVIEW_TIMEOUT_SECONDS
    wait_seconds: float = REQUEST_WAIT_SECONDS
    _jobs: OrderedDict[str, _Job] = field(default_factory=OrderedDict)

    async def run(self, key: str, factory: Callable[[], Awaitable[Any]]) -> tuple[bool, Any]:
        """``(True, result)`` once the review finished (result None when it failed); ``(False, None)`` while pending."""

        self._prune()
        job = self._jobs.get(key)
        if job is None:
            job = _Job(task=asyncio.create_task(self._bounded(factory), name=f"agent-eval-review:{key[:12]}", context=contextvars.Context()))
            self._jobs[key] = job

            def _finished(_task: asyncio.Task[Any], job: _Job = job) -> None:
                job.finished_at = time.monotonic()

            job.task.add_done_callback(_finished)
        if not job.task.done():
            await asyncio.wait({job.task}, timeout=self.wait_seconds)
        if not job.task.done():
            return False, None
        # A finished review is handed out once; the next identical request reviews again, as a
        # request did before reviews ran in the background.
        if self._jobs.get(key) is job:
            self._jobs.pop(key, None)
        return True, job.task.result()

    async def _bounded(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await asyncio.wait_for(factory(), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("agent_eval.review_unavailable", exc_info=True)
            return None

    def _prune(self) -> None:
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if job.task.done() and job.finished_at is not None and now - job.finished_at > _RESULT_TTL_SECONDS:
                self._jobs.pop(key, None)
        while len(self._jobs) > _MAX_JOBS:
            key, job = next(iter(self._jobs.items()))
            if not job.task.done():
                job.task.cancel()
            self._jobs.pop(key, None)


def review_jobs(state: Any) -> ReviewJobs:
    jobs = getattr(state, "agent_eval_review_jobs", None)
    if not isinstance(jobs, ReviewJobs):
        jobs = ReviewJobs()
        state.agent_eval_review_jobs = jobs
    return jobs


__all__ = ["REQUEST_WAIT_SECONDS", "REVIEW_TIMEOUT_SECONDS", "ReviewJobs", "review_jobs", "review_key"]
