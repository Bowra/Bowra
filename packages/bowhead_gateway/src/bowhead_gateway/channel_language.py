"""Turn language for channels without browser context (IM bridges and scheduled jobs).

Feishu, Slack, Teams, and scheduled jobs have no Accept-Language header or locale
cookie. They resolve the turn language through the shared resolver with the
incoming message (for scheduled jobs: the job prompt) in the browser's place:

interface: saved account or Space interface preference > message language
> the session's previous answer language > the client's own language (Teams) > English;
answer: team lock > saved account answer preference > message language
> the session's previous answer language > concrete team default > interface language.

The account ``users.locale`` column is not read here: it carries a column default
for every account, so it cannot tell a saved choice from a missing one. Explicit
choices are saved as ``ui_language`` / ``output_language`` account settings.

The returned context is set on the TurnRequest before admission, so the persisted
execution intent, live events, replay, and channel display copy share one value.
"""

from __future__ import annotations

import logging
from typing import Any

from bowhead_core.language_context import LanguageContext

logger = logging.getLogger(__name__)


async def resolve_channel_turn_language(
    app: Any,
    *,
    user_id: str | None,
    workspace_id: str | None,
    team_id: str | None,
    session_id: str | None,
    message_text: str,
    client_language: str | None = None,
) -> LanguageContext:
    """Resolve one channel turn's language; preference lookup failures never block the turn.

    ``client_language`` is a channel client's own language (the Teams activity ``locale``).
    It stands where the browser language stands for web chat: below saved preferences,
    Space defaults, message evidence, and the previous answer; above English.
    Unsupported values are ignored.
    """
    from bowhead_core.effective_language import channel_message_language
    from bowhead_core.language_context import resolve_turn_language
    from bowhead_core.presentation_locale import canonicalize_supported_locale

    user_settings, workspace_settings, team_policy = await load_language_preferences(
        app,
        user_id=user_id,
        workspace_id=workspace_id,
        team_id=team_id,
    )

    text = message_text or ""
    session_last: str | None = None
    # Only a message without language evidence needs the previous answer; skip the read otherwise.
    if not channel_message_language(text):
        session_last = await session_last_output_language(app, session_id)
    return resolve_turn_language(
        user_settings=user_settings,
        workspace_settings=workspace_settings,
        team_language_policy=team_policy,
        message_text=text,
        session_last_language=session_last,
        accept_language=canonicalize_supported_locale(client_language),
        follow_message_when_unset=True,
    )


async def load_language_preferences(
    app: Any,
    *,
    user_id: str | None,
    workspace_id: str | None,
    team_id: str | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    """Saved account settings, Space settings, and team language policy; lookup failures yield empties."""
    state = getattr(app, "state", None)
    user_settings: dict[str, str] = {}
    workspace_settings: dict[str, str] = {}
    team_policy: dict[str, Any] = {}
    try:
        store = getattr(state, "user_settings_store", None)
        if store is not None and user_id:
            user_settings = await store.list_for_user(user_id)
        ws_store = getattr(state, "workspace_settings_store", None)
        if ws_store is not None and workspace_id:
            workspace_settings = await ws_store.list_for_workspace(workspace_id)
        team_store = getattr(state, "team_settings_store", None)
        if team_store is not None and team_id:
            from bowhead_core.team_language_policy import KEY_TEAM_LANGUAGE_POLICY, parse_team_language_policy

            team_policy = parse_team_language_policy(await team_store.get(team_id, KEY_TEAM_LANGUAGE_POLICY))
    except Exception as error:  # noqa: BLE001 - Preference lookup must not interrupt turns.
        logger.warning("channel_language.settings_load_failed user=%s err=%s", user_id, type(error).__name__)
    return user_settings, workspace_settings, team_policy


async def resolve_web_followup_turn_language(
    app: Any,
    *,
    user_id: str,
    workspace_id: str,
    team_id: str,
    session_id: str,
    message_text: str,
    interface_language: str,
) -> LanguageContext:
    """Language of a web turn admitted without a request (a queued steer becoming a new turn).

    Resolves like a direct web send of the same text: saved preferences, team
    policy, the question language, and the previous answer. The interface stays
    the language persisted with the queued input, as the browser had it.
    """
    from bowhead_core.effective_language import channel_message_language
    from bowhead_core.language_context import resolve_turn_language

    user_settings, workspace_settings, team_policy = await load_language_preferences(
        app,
        user_id=user_id,
        workspace_id=workspace_id,
        team_id=team_id,
    )
    text = message_text or ""
    session_last = None if channel_message_language(text) else await session_last_output_language(app, session_id)
    return resolve_turn_language(
        user_settings=user_settings,
        workspace_settings=workspace_settings,
        team_language_policy=team_policy,
        user_profile_language=interface_language,
        message_text=text,
        session_last_language=session_last,
    )


async def session_last_output_language(app: Any, session_id: str | None) -> str | None:
    """Language of the session's latest delivered answer (same judge as web chat); None if unknown."""
    if not session_id:
        return None
    orchestrator = getattr(getattr(app, "state", None), "orchestrator", None)
    store = getattr(orchestrator, "session_store", None)
    if store is None:
        return None
    try:
        turns = await store.list_turns(session_id=session_id, limit=3)
    except Exception as error:  # noqa: BLE001 - The fallback layer must not block the turn.
        logger.warning("channel_language.session_last_load_failed session=%s err=%s", session_id, type(error).__name__)
        return None
    from bowhead_core.effective_language import session_follow_language_of_markdown

    for turn in reversed(turns):
        answer = (getattr(turn, "final_answer", "") or "").strip()
        if answer and (language := session_follow_language_of_markdown(answer)):
            return language
    return None


__all__ = [
    "load_language_preferences",
    "resolve_channel_turn_language",
    "resolve_web_followup_turn_language",
    "session_last_output_language",
]
