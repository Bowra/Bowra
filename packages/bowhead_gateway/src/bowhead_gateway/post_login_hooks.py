"""Fail-soft hooks executed after an interactive login response is sent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from bowhead_bi_bridge.errors import BIValidationError
from bowhead_core._email_utils import normalize_email
from fastapi import BackgroundTasks, Request

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PostLoginContext:
    user_id: str
    team_id: str
    auth_method: str


@dataclass(frozen=True, slots=True)
class PreparedHook:
    hook: PostLoginHook
    target: str


class PostLoginHook(Protocol):
    key: str

    async def prepare(self, app: Any, context: PostLoginContext) -> str | None: ...

    async def run(self, app: Any, context: PostLoginContext, target: str) -> None: ...


class DatarcEnsureUserHook:
    key = "datarc.ensure_user"

    async def prepare(self, app: Any, context: PostLoginContext) -> str | None:
        """Only schedule when the Team has an active, credentialed connection."""
        store = getattr(app.state, "datarc_connection_store", None)
        if store is None:
            return None
        connections = await store.list_connections(context.team_id)
        connection = connections[0] if connections else None
        if connection is None or connection.status != "active":
            return None
        if not connection.base_url or not connection.key_hint:
            return None
        return connection.id

    async def run(self, app: Any, context: PostLoginContext, target: str) -> None:
        user_store = getattr(app.state, "user_store", None)
        connection_store = getattr(app.state, "datarc_connection_store", None)
        client_factory = getattr(app.state, "datarc_client_factory", None)
        if user_store is None or connection_store is None or client_factory is None:
            return

        user = await user_store.get_user(context.user_id)
        if user is None or user.status != "active":
            return
        if await user_store.get_team_role(user.id, context.team_id) is None:
            return

        connection = await connection_store.get_connection(target, context.team_id)
        if connection is None or connection.status != "active" or not connection.base_url:
            return
        auth_key = await connection_store.get_connection_auth(target, context.team_id)
        if not auth_key:
            return

        email = normalize_email(user.email)
        if not email:
            return
        metadata = await _identity_metadata(app, context)
        oidc_name = next(
            (value.strip() for key in ("nickname", "name") if isinstance((value := metadata.get(key)), str) and value.strip()),
            "",
        )
        nickname = oidc_name or (user.display_name or "").strip() or email.split("@", 1)[0]
        client = client_factory.get_client(connection.base_url, auth_key)

        account = await client.fetch_account(email)
        created = account is None
        if account is None:
            try:
                account = await client.create_account(
                    uid=str(uuid4()),
                    email=email,
                    nickname=nickname,
                    metadata=metadata,
                )
            except BIValidationError:
                # Two near-simultaneous logins may both observe a miss. If the
                # other request created the account, the post-conflict lookup
                # makes this execution idempotent; otherwise preserve the error.
                account = await client.fetch_account(email)
                if account is None:
                    raise

        context_service = getattr(app.state, "tool_context_service", None)
        if context_service is not None:
            context_service.invalidate_user(user.id)
        logger.info(
            "post_login_hook.succeeded hook=%s user_id=%s team_id=%s target=%s outcome=%s",
            self.key,
            user.id,
            context.team_id,
            target,
            "created" if created else "existing",
        )


async def _identity_metadata(app: Any, context: PostLoginContext) -> dict[str, Any]:
    from bowhead_gateway.edition_identity import identity_adapter

    adapter = identity_adapter(app)
    if adapter is None:
        return {}
    return await adapter.post_login_metadata(app, context.team_id, context.user_id)


_HOOKS: tuple[PostLoginHook, ...] = (DatarcEnsureUserHook(),)


async def schedule_post_login_hooks(
    background_tasks: BackgroundTasks,
    request: Request,
    *,
    user: Any,
    team_id: str,
    auth_method: str,
) -> None:
    """Prepare eligible hooks without ever changing the login result."""
    try:
        from bowhead_gateway.routes.auth import _store

        await _store(request).log_audit(
            user_id=str(user.id),
            team_id=team_id,
            event_type="team_login_success",
        )
    except Exception as exc:
        logger.error("team_login_metric.failed user_id=%s team_id=%s error_type=%s", user.id, team_id, type(exc).__name__)
    context = PostLoginContext(
        user_id=str(user.id),
        team_id=team_id,
        auth_method=auth_method,
    )
    prepared: list[PreparedHook] = []
    for hook in _HOOKS:
        try:
            target = await hook.prepare(request.app, context)
        except Exception as exc:
            logger.error(
                "post_login_hook.prepare_failed hook=%s user_id=%s team_id=%s error_type=%s",
                hook.key,
                context.user_id,
                context.team_id,
                type(exc).__name__,
            )
            continue
        if target is not None:
            prepared.append(PreparedHook(hook=hook, target=target))
    if prepared:
        background_tasks.add_task(_run_prepared_hooks, request.app, context, tuple(prepared))


async def _run_prepared_hooks(
    app: Any,
    context: PostLoginContext,
    prepared: tuple[PreparedHook, ...],
) -> None:
    for item in prepared:
        try:
            await item.hook.run(app, context, item.target)
        except Exception as exc:
            # The HTTP response has already been sent. A hook failure must never
            # affect authentication or prevent later hooks from running.
            logger.exception(
                "post_login_hook.failed hook=%s user_id=%s team_id=%s target=%s error_type=%s",
                item.hook.key,
                context.user_id,
                context.team_id,
                item.target,
                type(exc).__name__,
            )
