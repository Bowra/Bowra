"""Request-local capacity projections over unchanged execution evidence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace

from bowhead_router.context_policy import TargetContextBudget
from bowhead_router.types import LLMRequest, Message

from bowhead_core.context_compaction import balanced_boundaries


def capacity_message_groups(messages: list[Message], *, current_request: int, canonical_count: int) -> tuple[tuple[int, int], ...]:
    """Offer oldest complete history turns, then older completed Tool groups.

    The current task, its latest correction, the latest native transaction and
    request-only suffix are never candidates. Callers separately protect their
    immutable coordination prefix before applying these lane-local positions.
    """
    boundaries = balanced_boundaries(messages)
    history = [cut for cut in boundaries if cut <= current_request and (cut == current_request or messages[cut].role == "user")]
    if not history or history[0] != 0:
        history.insert(0, 0)
    start = current_request + 1
    end = max((cut for cut in boundaries if start <= cut < canonical_count), default=start)
    corrections = [i for i in range(start, canonical_count) if messages[i].role == "user" and messages[i].name != "context_summary"]
    if corrections:
        end = min(end, corrections[-1])
    active = [cut for cut in boundaries if start <= cut <= end]
    return (*zip(history, history[1:]), *zip(active, active[1:]))


async def capacity_projection(
    request: LLMRequest,
    *,
    before: TargetContextBudget,
    groups: Sequence[tuple[int, int]],
    measure: Callable[[LLMRequest], Awaitable[TargetContextBudget]],
    rejected_input_estimate: int | None = None,
    rejection: BaseException | None = None,
) -> tuple[LLMRequest, TargetContextBudget, int] | None:
    """Keep the largest recent projection that the actual target can accept.

    No summary, storage write, native transcript edit or model invocation occurs.
    A definite upstream overflow may need a smaller request even if the local
    estimate fits. Reuse the existing numeric-overage recovery policy when known;
    otherwise the 25% recovery reduction is a target, not a new
    rejection boundary: any available smaller, fitting projection can be tried
    by the original bounded recovery path.
    """
    if not groups:
        return None

    async def project(count: int) -> tuple[LLMRequest, TargetContextBudget, int]:
        retained = []
        position = 0
        removed = 0
        for start, end in groups[:count]:
            retained.extend(request.messages[position:start])
            position = end
            removed += end - start
        retained.extend(request.messages[position:])
        candidate = replace(request, messages=retained, context_budget=None, output_budget=None)
        return candidate, await measure(candidate), removed

    smallest = await project(len(groups))
    if not smallest[1].budget.fits:
        return None
    if rejected_input_estimate is not None and smallest[1].budget.input_estimate >= rejected_input_estimate:
        return None
    target = before.budget.input_limit
    if rejected_input_estimate is not None:
        recovery_target = int(rejected_input_estimate * 0.75)
        if rejection is not None:
            from bowhead_router.errors import context_overflow_from_message

            from bowhead_core.llm_failure import target_failures

            failures = list(target_failures(rejection))
            numbers = context_overflow_from_message(failures[-1].upstream_message) if failures else None
            if numbers is not None:
                maximum, requested = numbers
                if maximum and requested and requested > maximum:
                    recovery_target = rejected_input_estimate - int((requested - maximum) * 1.05) - int(maximum * 0.05)
                elif maximum:
                    recovery_target = int(maximum * 0.85)
        target = min(target, recovery_target)
    selected = smallest
    low, high = 1, len(groups) - 1
    while low <= high:
        middle = (low + high) // 2
        proposed = await project(middle)
        if proposed[1].budget.fits and proposed[1].budget.input_estimate <= target:
            selected = proposed
            high = middle - 1
        else:
            low = middle + 1
    return selected
