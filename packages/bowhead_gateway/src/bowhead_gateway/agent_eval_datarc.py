"""The Datarc identity of an evaluation attempt (FEATURE_AGENT_EVAL_DATARC_READ_ONLY, design §3).

An attempt reads Datarc as the Team Agent's **current effective Owner**, and only when that same person
started the run. Each attempt resolves this again at its first Datarc call, so an ownership transfer, a
revoked membership or an unverified email stops later attempts:

1. The effective Owner now (``COALESCE(owner_user_id, created_by)``, active user, active Team member).
2. ``run.created_by`` must be that Owner, else ``starter_not_owner``; no Datarc request is made.
3. The Owner's email must be verified, else ``owner_identity_unverified``.
4. The connection comes only from the Team connection registry (``resolve_team_datarc_client``). There is
   no deployment, member, default-workspace or environment-variable fallback.
5. The workspace is the Owner's ladder in ``read_only`` mode with no session: no auto-select, no writes.
6. The member comes from the Owner's verified email in that workspace (``MemberUidResolver``).

Failures raise ``EvalDatarcUnavailable`` with a cause, never Datarc's message text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import aiosqlite
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_datarc import EvalDatarcUnavailable, ResolvedDatarcIdentity
from bowhead_core.agent_eval_store import EvalRun

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ErrorRecorder:
    """Wraps the connection's client while the workspace ladder runs, keeping the typed failures it saw.

    The ladder turns Datarc failures into a "transient" snapshot; the recorded exception says whether the
    connection key itself was refused. Calls pass straight through; nothing is filtered here.
    """

    client: Any
    errors: list[BaseException]

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.client, name)
        if not callable(target):
            return target

        async def _call(*args: Any, **kwargs: Any) -> Any:
            try:
                return await target(*args, **kwargs)
            except Exception as exc:
                self.errors.append(exc)
                raise

        return _call


def _datarc_failure_cause(errors: list[BaseException]) -> str:
    from bowhead_bi_bridge.errors import BIPermissionError

    if any(isinstance(exc, BIPermissionError) and getattr(exc, "source", None) == "http" for exc in errors):
        return "connection_rejected"
    return "datarc_unreachable"


@dataclass(slots=True)
class TeamOwnerDatarcResolver:
    """``EvalDatarcIdentityResolver`` for Team Agents: the current Owner through the Team's connection."""

    db_path: str
    state: Any

    async def resolve(self, run: EvalRun) -> ResolvedDatarcIdentity:
        from bowhead_gateway.datarc_context import (
            DatarcResolutionError,
            _resolve_per_user_datarc_workspace,
            resolve_team_datarc_binding,
        )
        from bowhead_gateway.member_resolver import MemberNotFoundError

        team_id = run.subject.team_id
        if run.subject.agent_source_kind != "team":
            # System Agents are out of scope; the runtime never builds this resolver for them.
            raise EvalDatarcUnavailable("starter_not_owner")
        owner = await self._current_owner(team_id=team_id, agent_id=run.subject.agent_id)
        if owner is None or owner["user_id"] != run.created_by:
            name = str(owner["display_name"] or "") if owner is not None else ""
            raise EvalDatarcUnavailable("starter_not_owner", owner_name=name or None)
        email = str(owner["email"] or "")
        if not email or not bool(owner["email_verified"]):
            raise EvalDatarcUnavailable("owner_identity_unverified")

        app = SimpleNamespace(state=self.state)
        try:
            binding = await resolve_team_datarc_binding(app, team_id)
            client = binding.client if binding else None
        except DatarcResolutionError:
            raise EvalDatarcUnavailable("connection_unusable") from None
        if client is None:
            raise EvalDatarcUnavailable("no_connection")

        errors: list[BaseException] = []
        try:
            workspace_id, workspace_name, _accessible, _reason = await _resolve_per_user_datarc_workspace(
                self.state,
                _ErrorRecorder(client, errors),
                owner["user_id"],
                session_id=None,
                read_only=True,
                log=logger,
                connection_binding=binding,
            )
        except Exception as exc:
            errors.append(exc)
            raise EvalDatarcUnavailable(_datarc_failure_cause(errors)) from None
        if errors and not workspace_id:
            raise EvalDatarcUnavailable(_datarc_failure_cause(errors))
        if not workspace_id:
            raise EvalDatarcUnavailable("no_workspace")

        resolver = getattr(self.state, "member_resolver", None)
        if resolver is None:
            raise EvalDatarcUnavailable("datarc_unreachable")
        try:
            member_uid = await resolver.resolve(email, str(workspace_id), datarc=client)
        except MemberNotFoundError:
            raise EvalDatarcUnavailable("owner_not_member", workspace=workspace_name or str(workspace_id)) from None
        except Exception as exc:
            raise EvalDatarcUnavailable(_datarc_failure_cause([exc])) from None
        if not isinstance(member_uid, str) or not member_uid.strip():
            # Never hand a Tool an empty member: that turns the term permission filter off.
            raise EvalDatarcUnavailable("owner_not_member", workspace=workspace_name or str(workspace_id))

        vector_client = await binding.vector_resolver([str(workspace_id)]) if binding and binding.vector_resolver else None
        return ResolvedDatarcIdentity(
            client=client,
            vector_client=vector_client,
            member_uid=member_uid,
            workspace_id=str(workspace_id),
            workspace_name=str(workspace_name or ""),
        )

    async def _current_owner(self, *, team_id: str, agent_id: str) -> aiosqlite.Row | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT owner.id AS user_id,owner.email,owner.email_verified,owner.display_name
                FROM team_subagent agent
                JOIN users owner
                  ON owner.id=COALESCE(agent.owner_user_id,agent.created_by) AND owner.status='active'
                JOIN team_members member
                  ON member.user_id=owner.id AND member.team_id=agent.team_id AND member.status='active'
                WHERE agent.team_id=? AND agent.id=?
                """,
                (team_id, agent_id),
            ) as cursor:
                return await cursor.fetchone()


__all__ = ["TeamOwnerDatarcResolver"]
