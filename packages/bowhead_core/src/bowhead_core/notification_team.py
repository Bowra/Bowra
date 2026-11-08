"""Which Team a notification belongs to, and who in it may receive it.

The authority is always the notification's subject: the workspace an approval or a scheduled task
runs in, the Team an access request or a Team Skill belongs to. The legacy ``users.team_id`` is the
user's default Team under multi-Team, so it would route an item raised in Team B to Team A's
administrators; it is used only as a namespace for items addressed to their owner alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bowhead_core.user_store import UserStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationSubject:
    """What a notification is about, plus the authority that decides its Team.

    ``team_id`` is set when the caller already holds the subject's Team (an access request's
    stored Team, a Team Skill's Team). Otherwise ``workspace_id`` resolves through the workspace's
    Team. ``owner_user_id`` is the person the item is ultimately for: when no Team resolves, the
    item may still reach this person alone, under their own namespace.
    """

    subject_type: str | None
    subject_id: str | None
    team_id: str | None = None
    workspace_id: str | None = None
    owner_user_id: str | None = None


@dataclass(frozen=True)
class ResolvedTeam:
    team_id: str
    # False for an owner's own namespace (no Team resolved, or a personal type): such an item
    # never reaches anyone but its owner and never carries a Team label.
    authoritative: bool


@dataclass
class NotificationTeamResolver:
    user_store: UserStore

    async def workspace_team(self, workspace_id: str | None, *, subject: str, subject_id: str | None) -> str | None:
        """The Team that owns a workspace, as the approval route and scheduled runs resolve it."""
        if not workspace_id:
            return None
        try:
            return await self.user_store.get_workspace_team_id(workspace_id)
        except Exception as exc:
            logger.warning(
                "notification.workspace_team_lookup_failed subject=%s id=%s err=%s",
                subject,
                subject_id,
                exc,
            )
            return None

    async def owner_namespace(self, user_id: str | None) -> str | None:
        """The owner's legacy default Team, used only as a namespace for items addressed to them alone."""
        if not user_id:
            return None
        return await self.user_store.get_user_team_id(user_id)

    async def resolve(self, subject: NotificationSubject, *, personal: bool) -> ResolvedTeam | None:
        if personal:
            namespace = await self.owner_namespace(subject.owner_user_id)
            return ResolvedTeam(namespace, authoritative=False) if namespace else None
        team_id = subject.team_id or await self.workspace_team(
            subject.workspace_id,
            subject=subject.subject_type or "unknown",
            subject_id=subject.subject_id,
        )
        if team_id:
            return ResolvedTeam(team_id, authoritative=True)
        namespace = await self.owner_namespace(subject.owner_user_id)
        return ResolvedTeam(namespace, authoritative=False) if namespace else None

    async def closing_teams(self, subject_team_id: str | None, user_id: str | None) -> list[str]:
        """Teams to search when closing an item: the subject's Team, then the owner's legacy default Team.

        Items written before notifications followed the subject's Team live under the legacy Team;
        searching it only closes them and never delivers anything there.
        """
        team_ids = [subject_team_id] if subject_team_id else []
        legacy_team_id = await self.owner_namespace(user_id)
        if legacy_team_id and legacy_team_id not in team_ids:
            team_ids.append(legacy_team_id)
        return team_ids

    async def active_members(self, team_id: str, user_ids: Iterable[str]) -> set[str]:
        """The subset of ``user_ids`` who are active members of an active Team."""
        wanted = {uid for uid in user_ids if uid}
        if not wanted:
            return set()
        return await self.user_store.filter_active_team_members(team_id, wanted)
