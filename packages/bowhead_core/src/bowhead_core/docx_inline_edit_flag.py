"""FEATURE_DOCX_INLINE_EDIT: Word page previews and in-browser Word editing.

Default off (rollout register: Word inline edit; docx inline edit design §7 and §11). Read on every
request, so a changed value applies without a restart.
"""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def docx_inline_edit_enabled() -> bool:
    return os.environ.get("FEATURE_DOCX_INLINE_EDIT", "0").strip().lower() in _TRUTHY


__all__ = ["docx_inline_edit_enabled"]
