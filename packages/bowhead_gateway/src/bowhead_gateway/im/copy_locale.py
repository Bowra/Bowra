"""Presentation locale for IM card copy rendered outside a turn's language snapshot.

Feishu, Slack, and Teams render some copy before a turn exists (account binding,
space pickers, refusals before a space is chosen, Slack command acknowledgements)
and some in reply to a card button. Both use the shared channel resolver
(``bowhead_gateway.channel_language``), so this copy agrees with the turn that the
same message would start:

- With an incoming message, the message stands in for the browser language.
- A card button carries the locale its card was rendered in. That snapshot wins,
  so the reply matches the card (and, for turn cards, the turn's language).
- A button from an older card without a snapshot resolves without a message:
  the saved interface preference, then the Space default interface language when a
  ``workspace_id`` is passed, then the session's previous answer when a ``session_id``
  is passed, then English. Team answer-language defaults do not change interface copy.

Turn-scoped copy uses the turn's ``LanguageContext.ui_language`` directly.
"""

from __future__ import annotations

from typing import Any

from bowhead_core.presentation_locale import canonicalize_supported_locale

from bowhead_gateway.channel_language import resolve_channel_turn_language


async def resolve_channel_copy_locale(
    app: Any,
    *,
    user_id: str | None,
    message_text: str = "",
    rendered_locale: object = None,
    workspace_id: str | None = None,
    team_id: str | None = None,
    session_id: str | None = None,
    client_language: str | None = None,
) -> str:
    """Copy locale outside a turn snapshot; settings lookup failures never block."""
    snapshot = canonicalize_supported_locale(rendered_locale)
    if snapshot is not None:
        return snapshot
    context = await resolve_channel_turn_language(
        app,
        user_id=user_id,
        workspace_id=workspace_id,
        team_id=team_id,
        session_id=session_id,
        message_text=message_text,
        client_language=client_language,
    )
    return context.ui_language


__all__ = ["resolve_channel_copy_locale"]
