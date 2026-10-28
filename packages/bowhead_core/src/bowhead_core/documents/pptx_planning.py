"""Bounded planning and protocol repair, without semantic approval or execution.

The caller owns authorized model selection, durable dispatch/usage recording and
provider retry policy. A successful review remains an unapproved proposal. This
module must not be used as a document worker or a publication permission check.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio

from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_plan import (
    PROFILE,
    JsonEvidenceSource,
    PlanReview,
    planning_repair_instruction,
    planning_system_prompt,
    prepare_planning_context,
    review_planning_response,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["PlanningAttempt", "PlanningReply", "PlanningResult", "plan_pptx_text"]

# Only failures attributable to the model's bounded output are repairable.
# Input/package failures, stale identity, transport failures and cancellation
# must never be converted into another call or a fabricated user question.
REPAIRABLE_ERRORS = frozenset(
    {
        "planning_duplicate_key",
        "planning_nonfinite_number",
        "planning_invalid_json",
        "planning_invalid_response",
        "planning_invalid_slots",
        "planning_invalid_parts",
        "planning_ungrounded_literal",
        "planning_quote_mismatch",
        "planning_unknown_evidence",
        "planning_invalid_sum",
        "planning_unusable_value",
        "planning_invalid_replacement",
        "planning_value_includes_fixed_prefix",
        "planning_invalid_question",
        "planning_invalid_update",
        "planning_noop_update",
        "planning_incomplete_scope",
        "planning_numeric_limit",
        "planning_numeric_replacement",
    }
)


@dataclass(frozen=True)
class PlanningReply:
    """A completed provider response; usage remains owned by the caller."""

    content: str
    finish_reason: str


@dataclass(frozen=True)
class PlanningAttempt:
    """Content-free diagnostics bound to a single prompt and provider response."""

    request_digest: str
    response_digest: str
    error: str | None


@dataclass(frozen=True)
class PlanningResult:
    """Unapproved review or internal failure, never a user-facing job status."""

    context_digest: str
    attempts: tuple[PlanningAttempt, ...]
    review: PlanReview | None
    error: str | None


async def plan_pptx_text(
    source: bytes,
    instruction: str,
    data: tuple[JsonEvidenceSource, ...],
    *,
    invoke: Callable[[str, str], Awaitable[PlanningReply]],
    profile: str = PROFILE,
) -> PlanningResult:
    """Propose changes with at most one repair of completed malformed output.

    Args:
        source: Exact immutable PPTX bytes.
        instruction: Canonical user-authored request, including any answers.
        data: Frozen named evidence sources.
        invoke: Authorized, metered caller; records dispatch before network I/O.
            It must not retry unknown outcomes or silently change model identity.

    Returns:
        Provenance review or internal failure with both attempt identities.
        Proposals may feed the trusted private-draft compiler. Neither a
        proposal nor a question grants semantic approval or publication rights.

    Raises:
        OfficePackageError: Input/package/identity validation fails.
        TimeoutError: A call exceeds 90 seconds; no automatic retry follows.
        Exception: Caller transport/recording errors propagate without retry.
    """
    context = prepare_planning_context(source, instruction, data, profile=profile)
    system = planning_system_prompt(profile)
    prompt = context.payload
    attempts = []
    for attempt in range(2):
        request_digest = hashlib.sha256((system + "\n" + prompt).encode()).hexdigest()
        with anyio.fail_after(90):
            reply = await invoke(system, prompt)
        # Oversized or incomplete output is not replayed into another prompt.
        # The caller retains its original response/usage, including failures.
        response_bytes = reply.content.encode("utf-8", errors="surrogatepass")
        response_digest = hashlib.sha256(response_bytes).hexdigest()
        error = "planning_incomplete_response" if reply.finish_reason != "stop" else None
        if len(response_bytes) > 64_000:
            error = "planning_response_limit"
        review = None
        if error is None:
            try:
                review = review_planning_response(source, instruction, data, reply.content, expected_context_digest=context.digest, profile=profile)
            except OfficePackageError as exc:
                if exc.code not in REPAIRABLE_ERRORS and exc.code != "planning_invalid_unicode":
                    raise
                error = exc.code
        attempts.append(PlanningAttempt(request_digest, response_digest, error))
        if review is not None or error not in REPAIRABLE_ERRORS or attempt == 1:
            return PlanningResult(context.digest, tuple(attempts), review, error)
        # Diagnostics contain no suggested values, business answers or expected
        # test output. Failed model text stays untrusted data in a JSON envelope.
        prompt = json.dumps(
            {
                "input": json.loads(context.payload),
                "repair": {
                    "error": error,
                    "previous_response": reply.content,
                    "instruction": planning_repair_instruction(profile),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    raise AssertionError("Planning attempt limit is unreachable")
