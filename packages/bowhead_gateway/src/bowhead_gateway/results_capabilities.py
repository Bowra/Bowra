"""Runtime capability policy for the standard Results experience."""

from __future__ import annotations

import os
from dataclasses import dataclass

from bowhead_gateway.results_cursor import results_cursor_configured


@dataclass(frozen=True)
class ResultsCapabilities:
    workspace: bool
    complete_list: bool
    scale_nav: bool
    interactive_html: bool = False


def resolve_results_capabilities(
    *,
    cursor_ready: bool | None = None,
) -> ResultsCapabilities:
    """Resolve standard Results and explicitly admitted HTML execution support.

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
        interactive_html=os.environ.get("FEATURE_INTERACTIVE_HTML_PREVIEW", "1") == "1",
    )


__all__ = [
    "ResultsCapabilities",
    "resolve_results_capabilities",
]
