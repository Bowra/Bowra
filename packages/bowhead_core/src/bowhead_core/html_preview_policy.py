"""Byte limits shared by HTML admission and browser previews."""

from __future__ import annotations

import os
from contextvars import ContextVar

DEFAULT_HTML_PREVIEW_BYTES = 5 * 1024 * 1024
MAX_HTML_PREVIEW_BYTES = 50 * 1024 * 1024
HTML_PREVIEW_LIMIT_HEADER = "X-Bowhead-HTML-Max-Bytes"
html_preview_policy: ContextVar[int | None] = ContextVar("html_preview_policy", default=None)


def html_preview_limit_bytes() -> int:
    """Read the active Team snapshot, otherwise the deployment default."""
    if (selected := html_preview_policy.get()) is not None:
        return selected
    return deployment_html_preview_bytes()


def deployment_html_preview_bytes() -> int:
    """Invalid deployment defaults retain the documented default."""
    try:
        value = int(os.environ.get("BOWHEAD_CANVAS_HTML_MAX_BYTES", str(DEFAULT_HTML_PREVIEW_BYTES)))
    except ValueError:
        return DEFAULT_HTML_PREVIEW_BYTES
    return value if 1 <= value <= MAX_HTML_PREVIEW_BYTES else DEFAULT_HTML_PREVIEW_BYTES
