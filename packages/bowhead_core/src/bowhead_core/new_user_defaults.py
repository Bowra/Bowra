"""Seed automatic language preferences for newly created accounts.

Account-creation routes own this call because UserStore does not own the
user_settings table. Preserve existing preferences and a UI language explicitly
selected before registration. Seeding failures must not prevent account creation.
"""

from __future__ import annotations

import logging
from typing import Any

from bowhead_core.language_context import SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)

NEW_USER_OUTPUT_LANGUAGE = "auto"
NEW_USER_UI_LANGUAGE = "auto"


async def seed_new_user_settings(
    user_settings_store: Any,
    user_id: str,
    *,
    ui_language: str | None = None,
) -> None:
    """Set missing preferences without overwriting a user's language choices."""
    if user_settings_store is None or not user_id:
        return
    defaults = {
        "ui_language": ui_language if ui_language in SUPPORTED_LANGUAGES else NEW_USER_UI_LANGUAGE,
        "output_language": NEW_USER_OUTPUT_LANGUAGE,
    }
    for key, value in defaults.items():
        try:
            if not await user_settings_store.get(user_id, key):
                await user_settings_store.set(user_id, key, value)
        except Exception as exc:  # noqa: BLE001 — Preferences must not block signup.
            logger.warning("new_user_defaults.seed_failed user=%s key=%s err=%s", user_id, key, type(exc).__name__)
