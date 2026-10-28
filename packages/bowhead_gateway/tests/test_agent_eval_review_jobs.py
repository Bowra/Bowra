from __future__ import annotations

import asyncio

import pytest
from bowhead_gateway.agent_eval_review_jobs import REVIEW_TIMEOUT_SECONDS, ReviewJobs, review_key

pytestmark = pytest.mark.anyio


def test_review_deadline_matches_the_other_writer_calls() -> None:
    # Reasoning models need far more than the old 30 s; the alignment writer and calibration wait 120 s.
    assert REVIEW_TIMEOUT_SECONDS == 120.0


async def test_slow_review_answers_pending_promptly_then_hands_out_its_result_once() -> None:
    jobs = ReviewJobs(wait_seconds=0.05)
    release = asyncio.Event()
    calls = 0

    async def review() -> dict[str, str]:
        nonlocal calls
        calls += 1
        await release.wait()
        return {"status": "ok"}

    key = review_key("point", "team", "user", "agent", {"task": "q"})
    assert await jobs.run(key, review) == (False, None)
    # The same request polls the same review instead of starting another.
    assert await jobs.run(key, review) == (False, None)
    release.set()
    await asyncio.sleep(0)
    assert await jobs.run(key, review) == (True, {"status": "ok"})
    assert calls == 1


async def test_review_over_the_deadline_finishes_as_unavailable() -> None:
    jobs = ReviewJobs(timeout_seconds=0.05, wait_seconds=1.0)

    async def review() -> None:
        await asyncio.sleep(5)

    assert await jobs.run("k", review) == (True, None)


async def test_failed_review_is_unavailable_and_the_next_request_tries_again() -> None:
    jobs = ReviewJobs(wait_seconds=1.0)
    outcomes = [RuntimeError("model down"), {"gaps": []}]

    async def review() -> object:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert await jobs.run("k", review) == (True, None)
    assert await jobs.run("k", review) == (True, {"gaps": []})
