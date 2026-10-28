"""Runtime capability policy for the standard Results experience."""

from __future__ import annotations

from dataclasses import dataclass

from bowhead_gateway.results_cursor import results_cursor_configured


@dataclass(frozen=True)
class ResultsCapabilities:
    workspace: bool
    complete_list: bool
    scale_nav: bool


def resolve_results_capabilities(
    *,
    cursor_ready: bool | None = None,
) -> ResultsCapabilities:
    """Resolve backend support without product rollout or account partitioning.

    Complete List and Scale Navigation are the standard Results behavior.  The
    only sub-capability dependency is a stable cursor key: claiming complete
    pagination without it would make the read contract dishonest.  The
    Workspace is released; cursor storage readiness remains mandatory.
    """

    key_ready = results_cursor_configured() if cursor_ready is None else cursor_ready
    complete_list = key_ready
    return ResultsCapabilities(
        workspace=True,
        complete_list=complete_list,
        scale_nav=complete_list,
    )


__all__ = [
    "ResultsCapabilities",
    "resolve_results_capabilities",
]
