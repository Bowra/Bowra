"""Feature flag for the access-request + approval flow (content-permissions wave 2).

Mirrors ``share_service.content_sharing_enabled`` — env-cached, default ON.
Gates the access-request router mount + the requester entry points + the
/settings/access-requests capability. See docs/sprints/access-requests/.
"""

from __future__ import annotations

import os

_ENV = "BOWHEAD_ACCESS_REQUESTS"
_cache: bool | None = None


def access_requests_enabled() -> bool:
    """Default ON; set BOWHEAD_ACCESS_REQUESTS=0 to disable. Cached per process."""
    global _cache  # noqa: PLW0603 — process-level flag cache (same pattern as share_service)
    if _cache is None:
        _cache = os.environ.get(_ENV, "1").strip() != "0"
    return _cache
