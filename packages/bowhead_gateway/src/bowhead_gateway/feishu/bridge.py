"""Bridge between Feishu IM callbacks and Bowhead chat turns."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote
from uuid import uuid4

from bowhead_core.presentation_locale import resolve_presentation_locale
from bowhead_core.source_linkage_copy import (
    format_figure_review_caveat,
    format_report_lifecycle,
    format_source_linkage,
)

from bowhead_core import Orchestrator, TurnRequest, get_event_registry
from bowhead_gateway.feishu import renderer
from bowhead_gateway.feishu.client import FeishuAPIError, FeishuClient
from bowhead_gateway.im.blocks import Caveat, ReportLifecycleLine, SourceLinkageLine, SourcesBlock, format_source_lines
from bowhead_gateway.im.copy_locale import resolve_channel_copy_locale
from bowhead_gateway.im.error_messages import im_error_message, im_interface_message
from bowhead_gateway.im.turn_render import NeutralTurnState, apply_event, refresh_kb_citation_labels, refresh_report_lifecycle, render_blocks
from bowhead_gateway.member_resolver import MemberNotFoundError
from bowhead_gateway.public_url import public_app_url, public_app_url_for_base

if TYPE_CHECKING:
    from bowhead_gateway.feishu.config import FeishuAppConfigResolver
    from bowhead_gateway.feishu.security import FeishuSecurityConfig
    from bowhead_gateway.feishu.store import FeishuEventStore

logger = logging.getLogger(__name__)


_AT_TAG_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class FeishuInboundMessage:
    event_id: str
    event_type: str
    tenant_key: str
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    text: str
    sender_open_id: str
    sender_union_id: str | None = None
    sender_user_id: str | None = None
    sender_email: str | None = None
    sender_name: str | None = None
    mentions_bot: bool = True


@dataclass(frozen=True)
class FeishuBoundUser:
    user_id: str
    team_id: str | None
    email: str | None
    display_name: str | None
    bowhead_workspace_id: str
    datarc_workspace_id: str
    workspace_role: str
    member_uid: str


def parse_message_event(body: dict[str, Any], *, bot_open_id: str = "") -> FeishuInboundMessage | None:
    """Parse Feishu `im.message.receive_v1` into Bowhead-friendly fields."""

    header = body.get("header") if isinstance(body.get("header"), dict) else {}
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    event_type = str(header.get("event_type") or body.get("type") or "")
    if event_type and event_type != "im.message.receive_v1":
        return None
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    sender_open_id = str(sender_id.get("open_id") or sender_id.get("user_id") or event.get("sender_open_id") or "")
    message_id = str(message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    chat_type = str(message.get("chat_type") or "")
    if not (sender_open_id and message_id and chat_id):
        return None
    text = extract_text(message)
    return FeishuInboundMessage(
        event_id=str(header.get("event_id") or body.get("uuid") or ""),
        event_type=event_type or "im.message.receive_v1",
        tenant_key=str(header.get("tenant_key") or body.get("tenant_key") or ""),
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        message_type=str(message.get("message_type") or ""),
        text=text,
        sender_open_id=sender_open_id,
        sender_union_id=sender_id.get("union_id"),
        sender_user_id=sender_id.get("user_id"),
        sender_email=_normalize_email(
            sender.get("email") or sender.get("tenant_key_email") or event.get("email"),
        ),
        sender_name=sender.get("sender_name") or sender.get("name"),
        mentions_bot=message_mentions_bot(message, bot_open_id=bot_open_id, chat_type=chat_type),
    )


def extract_text(message: dict[str, Any]) -> str:
    raw = message.get("content")
    content: Any = raw
    if isinstance(raw, str):
        try:
            content = json.loads(raw)
        except Exception:
            content = {"text": raw}
    text = ""
    if isinstance(content, dict):
        text = str(content.get("text") or "")
    elif content is not None:
        text = str(content)
    text = _AT_TAG_RE.sub("", text)
    for mention in message.get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        key = str(mention.get("key") or "")
        if key:
            text = text.replace(key, "")
    return " ".join(text.split()).strip()


def message_mentions_bot(
    message: dict[str, Any],
    *,
    bot_open_id: str = "",
    chat_type: str = "",
) -> bool:
    if chat_type not in {"group", "chat"}:
        return True
    mentions = message.get("mentions") or []
    if not mentions:
        return False
    if not bot_open_id:
        return True
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        mention_id = mention.get("id") if isinstance(mention.get("id"), dict) else {}
        values = {
            str(mention.get("open_id") or ""),
            str(mention.get("user_id") or ""),
            str(mention_id.get("open_id") or ""),
            str(mention_id.get("user_id") or ""),
        }
        if bot_open_id in values:
            return True
    return False


class FeishuTurnBridge:
    """Owns Feishu identity, turn start, event mirroring, and card callbacks."""

    def __init__(
        self,
        *,
        app: Any,
        client: FeishuClient,
        store: FeishuEventStore,
        config: FeishuSecurityConfig,
        app_config_resolver: FeishuAppConfigResolver | None = None,
    ) -> None:
        self.app = app
        self.client = client
        self.store = store
        self.config = config
        self.app_config_resolver = app_config_resolver
        self._mirror_tasks: set[asyncio.Task[None]] = set()

    async def handle_message_event(self, body: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0911
        inbound = parse_message_event(body, bot_open_id=self.config.bot_open_id)
        if inbound is None:
            return {"status": "ignored", "reason": "not_message_event"}
        if inbound.chat_type in {"group", "chat"} and not inbound.mentions_bot:
            return {"status": "ignored", "reason": "not_mentioned"}
        locale = await self._copy_locale(
            tenant_key=inbound.tenant_key,
            open_id=inbound.sender_open_id,
            message_text=inbound.text,
            session_id=self._session_id(inbound),
        )
        if inbound.chat_type in {"group", "chat"} and not self.config.group_public_answers_enabled:
            await self._send_onboarding(
                inbound,
                title=im_interface_message("group_disabled_title", locale),
                body=(im_interface_message("group_disabled", locale)),
            )
            return {"status": "denied", "reason": "group_public_answers_disabled"}
        if inbound.message_type != "text":
            await self._reply_card(
                inbound.message_id,
                renderer.error_card(
                    title=im_interface_message("unsupported_title", locale),
                    body=im_interface_message("unsupported", locale),
                ),
                tenant_key=inbound.tenant_key,
            )
            return {"status": "unsupported_message_type"}
        text = inbound.text.strip()
        if not text:
            await self._reply_card(
                inbound.message_id,
                renderer.error_card(title=im_interface_message("empty_title", locale), body=im_interface_message("empty_message", locale)),
                tenant_key=inbound.tenant_key,
            )
            return {"status": "empty_message"}
        if _is_workspace_switch_command(text):
            status = await self._handle_workspace_switch_request(inbound)
            return {"status": status}

        bound, resolution_status = await self._resolve_identity_and_workspace(inbound)
        if bound is None:
            return {"status": resolution_status}

        session_id = self._session_id(inbound)
        # Resolve the turn language once, before any turn-scoped card: the same snapshot
        # reaches the TurnRequest (persisted at admission) and every card of this turn.
        language_context = await self._resolve_im_language_context(bound, text, session_id)
        locale = language_context.ui_language
        entitlement_error = await self._preflight_credits_and_entitlement(bound, locale=locale)
        if entitlement_error:
            await self._send_user_scoped_card(
                inbound,
                renderer.error_card(title=im_interface_message("start_failed_title", locale), body=entitlement_error),
            )
            return {"status": "denied"}

        orchestrator: Orchestrator | None = getattr(self.app.state, "orchestrator", None)
        if orchestrator is None:
            await self._reply_card(
                inbound.message_id,
                renderer.error_card(
                    title=im_interface_message("service_unavailable_title", locale), body=im_interface_message("service_unavailable", locale)
                ),
                tenant_key=inbound.tenant_key,
            )
            return {"status": "orchestrator_not_ready"}

        await orchestrator.session_store.ensure_session(
            session_id=session_id,
            workspace_id=bound.bowhead_workspace_id,
            user_id=bound.user_id,
        )
        # feedback-0713 F2: deterministic IM thread↔session binding (_session_id). After soft deletion (R1 report
        # deletion linkage), cannot return 410 as Web does (IM thread cannot switch binding). Another message
        # explicitly requests continued use -> revive (clear deleted_at, restore listing, remove invisible billing).
        from bowhead_gateway.session_lifecycle import revive_session_for_im

        lifecycle_result = await revive_session_for_im(
            self.app,
            orchestrator,
            session_id=session_id,
            user_id=bound.user_id,
            request_id=f"feishu-message:{inbound.message_id}",
        )
        if lifecycle_result.error_code is not None:
            await self._send_im_error_card(
                inbound,
                code=lifecycle_result.error_code,
                locale=locale,
            )
            return {
                "status": "rejected",
                "code": lifecycle_result.error_code,
            }
        if lifecycle_result.changed:
            logger.info("feishu.session_revived_on_message session=%s", session_id)
        # Steer-mode intercept via the channel-neutral TurnService.
        # user_msg_id=inbound.message_id targets the shared 🤝 emoji acknowledgement.
        from bowhead_gateway.turn_service import steer_or_proceed

        _steer = await steer_or_proceed(
            orchestrator,
            session_id=session_id,
            text=text,
            user_msg_id=inbound.message_id,
        )
        if _steer.kind == "protocol_active":
            return {
                "status": "rejected",
                "code": ("session_turn_active" if _steer.active_protocol_version == 1 else "terminal_protocol_future_version"),
                "turn_id": _steer.active_turn_id,
            }
        if _steer.kind == "lookup_failed":
            return {
                "status": "rejected",
                "code": "terminal_dependency_recovering",
            }
        if _steer.kind == "buffered":
            # Feishu-specific UX: a FISTBUMP reaction on the user's own message (the emoji_ack
            # bus event for the card mirror is already emitted by steer_or_proceed).
            await self._best_effort_reaction(
                inbound.message_id,
                tenant_key=inbound.tenant_key,
            )
            return {"status": "buffered", "turn_id": _steer.active_turn_id}
        # _steer.kind == "proceed" → fall through to the fresh-turn flow below.

        datarc_client, datarc_unavailable, vector_client = await self._resolve_team_runtime_clients(bound.team_id)
        turn_req = TurnRequest(
            session_id=session_id,
            workspace_id=bound.bowhead_workspace_id,
            active_datarc_project=bound.datarc_workspace_id,
            datarc_client=datarc_client,
            datarc_unavailable=datarc_unavailable,
            vector_client=vector_client,
            user_id=bound.user_id,
            member_uid=bound.member_uid,
            text=text,
            attachments=[],
            user_profile={"display_name": bound.display_name or inbound.sender_name or ""},
            language_context=language_context,
            team_id=bound.team_id,
            kb_role="ws_admin" if bound.workspace_role in {"admin", "owner"} else "user",
        )
        # byo-no-clone: carry the team's BYO router on the request; keep orchestrator shared.
        try:
            _team_router = await self._resolve_team_router(bound.team_id)
        except RuntimeError as exc:
            if str(exc) != "team_model_runtime_unavailable":
                raise
            # Gate only **with a team**; teamless dev/legacy bindings retain the old path
            # (aligned with chat /send's 409 only when resolved_team_id is set).
            # credential-readiness batch 2 (tech-design §16 register): unconfigured team model ->
            # explicit channel message, no turn creation. Previously continued without team_router,
            # reaching RefusalAdapter in prod for a loud turn failure without channel attribution. Copy:
            # Finite IM error copy uses this turn's resolved interface language.
            from bowhead_gateway.im.error_messages import im_error_message

            await self._reply_card(
                inbound.message_id,
                renderer.error_card(
                    title=im_interface_message("start_failed_title", locale),
                    body=im_error_message("team_model_unconfigured", locale),
                ),
                tenant_key=inbound.tenant_key,
            )
            return {"status": "team_model_unconfigured"}
        turn_req = replace(turn_req, team_router=_team_router)
        from bowhead_gateway.turn_service import (
            _retire_admitted_legacy_bus,
            admit_legacy_chat_execution,
            spawn_turn,
        )

        legacy_bus = await admit_legacy_chat_execution(
            orchestrator,
            turn_req,
        )
        try:
            card_resp = await self._reply_card(
                inbound.message_id,
                renderer.processing_card(text=text, locale=locale),
                uuid=f"feishu_card_{turn_req.turn_id}",
                tenant_key=inbound.tenant_key,
            )
            card_message_id = _response_message_id(card_resp)
            await self.store.upsert_turn_link(
                turn_id=turn_req.turn_id,
                session_id=session_id,
                tenant_key=inbound.tenant_key,
                chat_id=inbound.chat_id,
                sender_open_id=inbound.sender_open_id,
                source_message_id=inbound.message_id,
                card_message_id=card_message_id,
                meta={
                    "chat_type": inbound.chat_type,
                    "workspace_id": bound.bowhead_workspace_id,
                    "datarc_workspace_id": bound.datarc_workspace_id,
                },
            )
            if card_message_id:
                await self.store.set_card_message_id(
                    turn_id=turn_req.turn_id,
                    card_message_id=card_message_id,
                )

            # The mirror must subscribe to the admitted bus before the worker.
            self._start_mirror(
                turn_id=turn_req.turn_id,
                tenant_key=inbound.tenant_key,
                card_message_id=card_message_id,
                source_message_id=inbound.message_id,
                sender_open_id=inbound.sender_open_id,
                locale=locale,
                answer_locale=language_context.output_language,
            )
            await asyncio.sleep(0)

            user_store_for_deduct = getattr(self.app.state, "user_store", None) if bound.team_id else None
            await spawn_turn(
                orchestrator,
                turn_req,
                user_store=user_store_for_deduct,
                team_id=bound.team_id,
                entitlements=getattr(self.app.state, "entitlements", None),
                name_prefix="turn_run_feishu",
                admitted_legacy_bus=legacy_bus,
            )
        except BaseException:
            await _retire_admitted_legacy_bus(
                orchestrator.event_registry,
                turn_id=turn_req.turn_id,
                producer_bus=legacy_bus,
            )
            raise
        return {"status": "started", "turn_id": turn_req.turn_id, "session_id": session_id}

    async def handle_card_callback(self, body: dict[str, Any]) -> dict[str, Any]:
        event = body.get("event") if isinstance(body.get("event"), dict) else body
        action = event.get("action") if isinstance(event.get("action"), dict) else {}
        value = action.get("value") if isinstance(action.get("value"), dict) else {}
        operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
        operator_id = operator.get("operator_id") if isinstance(operator.get("operator_id"), dict) else {}
        open_id = str(operator_id.get("open_id") or operator.get("open_id") or "")
        tenant_key = str(
            body.get("tenant_key")
            or (body.get("header") if isinstance(body.get("header"), dict) else {}).get("tenant_key")
            or event.get("tenant_key")
            or ""
        )
        tenant_key = tenant_key or str(value.get("tenant_key") or "")
        # Buttons carry the locale their card was rendered in (the triggering message's language).
        locale = await self._copy_locale(tenant_key=tenant_key, open_id=open_id, rendered_locale=value.get("locale"))
        allowed = str(value.get("allowed_open_id") or "")
        if allowed and allowed != open_id:
            return _toast(im_interface_message("owner_only", locale), level="warning")
        action_name = str(value.get("bowhead_action") or "")
        if action_name == "workspace_select":
            return await self._handle_workspace_select(
                tenant_key=tenant_key,
                open_id=open_id,
                value=value,
                locale=locale,
            )
        if action_name == "clarification_resolve":
            return await self._handle_clarification_resolve(
                tenant_key=tenant_key,
                open_id=open_id,
                value=value,
                locale=locale,
            )
        if action_name in {"retry_step", "skip_step"}:
            return await self._handle_retry_skip(value=value, action_name=action_name, locale=locale)
        if action_name == "approval_open_web":
            return _toast(im_interface_message("approval_web", locale), level="warning")
        return _toast(im_interface_message("unknown_action", locale), level="warning")

    async def _resolve_identity_and_workspace(
        self,
        inbound: FeishuInboundMessage,
    ) -> tuple[FeishuBoundUser | None, str]:
        user_store, user, email, status = await self._resolve_user_context(inbound)
        if user_store is None or user is None:
            return None, status
        locale = await self._user_copy_locale(user, inbound)
        workspaces = await user_store.list_workspaces_for_user(user.id)
        if not workspaces:
            await self._send_onboarding(
                inbound,
                title=im_interface_message("no_spaces_title", locale),
                body=im_interface_message("no_spaces", locale),
            )
            return None, "needs_workspace"
        selected = await self._select_workspace_for_inbound(
            inbound=inbound,
            bowhead_user_id=user.id,
            workspaces=workspaces,
        )
        if selected is None:
            await self._send_user_scoped_card(
                inbound,
                renderer.workspace_picker_card(
                    workspaces,
                    allowed_open_id=inbound.sender_open_id,
                    tenant_key=inbound.tenant_key,
                    chat_id=inbound.chat_id,
                    locale=locale,
                ),
            )
            return None, "needs_workspace"
        bowhead_workspace_id = str(selected["workspace_id"])
        datarc_workspace_id = str(selected.get("datarc_workspace_id") or "")
        if not datarc_workspace_id:
            datarc_workspace_id = bowhead_workspace_id
        if datarc_workspace_id.startswith("ws_"):
            await self._reply_card(
                inbound.message_id,
                renderer.error_card(
                    title=im_interface_message("datarc_missing_title", locale),
                    body=(im_interface_message("datarc_missing", locale)),
                ),
                tenant_key=inbound.tenant_key,
            )
            return None, "denied"
        role = str(selected.get("role") or "member")
        try:
            member_uid = await self._resolve_member_uid(
                identity=self._datarc_member_identity(user=user, email=email),
                datarc_workspace_id=datarc_workspace_id,
                team_id=getattr(user, "team_id", None),
            )
        except Exception as exc:
            logger.warning(
                "feishu.member_uid_resolve_transient_failed user_id=%s ws=%s err=%s",
                getattr(user, "id", None),
                datarc_workspace_id,
                exc,
            )
            await self._reply_card(
                inbound.message_id,
                renderer.error_card(
                    title=im_interface_message("data_permissions_unavailable_title", locale),
                    body=im_interface_message("data_permissions_unavailable", locale),
                ),
                tenant_key=inbound.tenant_key,
            )
            return None, "failed"
        if not member_uid:
            await self._reply_card(
                inbound.message_id,
                renderer.error_card(
                    title=im_interface_message("data_permissions_missing_title", locale),
                    body=im_interface_message("data_permissions_missing", locale),
                ),
                tenant_key=inbound.tenant_key,
            )
            return None, "denied"
        return FeishuBoundUser(
            user_id=user.id,
            team_id=getattr(user, "team_id", None),
            email=email or getattr(user, "email", None),
            display_name=getattr(user, "display_name", None),
            bowhead_workspace_id=bowhead_workspace_id,
            datarc_workspace_id=datarc_workspace_id,
            workspace_role=role,
            member_uid=member_uid,
        ), "ok"

    async def _resolve_user_context(
        self,
        inbound: FeishuInboundMessage,
    ) -> tuple[Any | None, Any | None, str | None, str]:
        # No Bowhead account is known yet: follow the message language, then English.
        locale = await resolve_channel_copy_locale(self.app, user_id=None, message_text=inbound.text, session_id=self._session_id(inbound))
        user_store = getattr(self.app.state, "user_store", None)
        if user_store is None:
            await self._send_onboarding(
                inbound,
                title=im_interface_message("users_unavailable_title", locale),
                body=im_interface_message("users_unavailable", locale),
            )
            return None, None, None, "needs_binding"
        binding = await self.store.get_binding(
            tenant_key=inbound.tenant_key,
            open_id=inbound.sender_open_id,
        )
        user = None
        email = inbound.sender_email
        if binding is not None:
            user = await user_store.get_user(binding.bowhead_user_id)
            email = binding.email or email
        if user is None:
            email = email or await self._best_effort_email(
                inbound.sender_open_id,
                tenant_key=inbound.tenant_key,
            )
            if email:
                user = await user_store.get_user_by_email(email)
        if user is None:
            binding_session = await self.store.create_binding_session(
                tenant_key=inbound.tenant_key,
                open_id=inbound.sender_open_id,
                union_id=inbound.sender_union_id,
                email=email,
                chat_id=inbound.chat_id,
                source_message_id=inbound.message_id,
            )
            try:
                bind_url = await self._binding_session_url(
                    binding_session.id,
                    tenant_key=inbound.tenant_key,
                )
            except Exception as exc:
                logger.warning(
                    "feishu.binding_url_unavailable tenant=%s session=%s err=%s",
                    inbound.tenant_key,
                    binding_session.id,
                    exc,
                )
                await self._send_user_scoped_card(
                    inbound,
                    renderer.error_card(
                        title=im_interface_message("binding_link_unavailable_title", locale),
                        body=im_interface_message("binding_link_unavailable", locale),
                    ),
                )
                return user_store, None, email, "needs_binding"
            await self._send_user_scoped_card(
                inbound,
                renderer.binding_required_card(
                    bind_url=bind_url,
                    feishu_email=email,
                    locale=locale,
                ),
            )
            return user_store, None, email, "needs_binding"
        locale = await self._user_copy_locale(user, inbound)
        unavailable_reason = _feishu_user_unavailable_reason(user, locale)
        if unavailable_reason:
            await self._send_onboarding(
                inbound,
                title=im_interface_message("account_unavailable_title", locale),
                body=unavailable_reason,
            )
            logger.info(
                "feishu.user_unavailable tenant=%s open_id=%s user_id=%s reason=%s",
                inbound.tenant_key,
                inbound.sender_open_id,
                getattr(user, "id", ""),
                unavailable_reason,
            )
            return user_store, None, email, "account_unavailable"
        if binding is None:
            await self.store.upsert_binding(
                tenant_key=inbound.tenant_key,
                open_id=inbound.sender_open_id,
                union_id=inbound.sender_union_id,
                email=email,
                bowhead_user_id=user.id,
                binding_method="email_match",
            )
        return user_store, user, email, "ok"

    async def _handle_workspace_switch_request(
        self,
        inbound: FeishuInboundMessage,
    ) -> str:
        user_store, user, _email, status = await self._resolve_user_context(inbound)
        if user_store is None or user is None:
            return status
        locale = await self._user_copy_locale(user, inbound)
        workspaces = await user_store.list_workspaces_for_user(user.id)
        if not workspaces:
            await self._send_onboarding(
                inbound,
                title=im_interface_message("no_spaces_title", locale),
                body=im_interface_message("no_spaces", locale),
            )
            return "needs_workspace"
        await self._send_user_scoped_card(
            inbound,
            renderer.workspace_picker_card(
                workspaces,
                allowed_open_id=inbound.sender_open_id,
                tenant_key=inbound.tenant_key,
                chat_id=inbound.chat_id,
                locale=locale,
            ),
        )
        return "needs_workspace"

    async def _select_workspace_for_inbound(
        self,
        *,
        inbound: FeishuInboundMessage,
        bowhead_user_id: str,
        workspaces: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if len(workspaces) == 1:
            selected = workspaces[0]
            await self.store.upsert_workspace_binding(
                tenant_key=inbound.tenant_key,
                chat_id=inbound.chat_id,
                open_id=inbound.sender_open_id,
                bowhead_user_id=bowhead_user_id,
                workspace_id=str(selected["workspace_id"]),
            )
            return selected
        binding = await self.store.get_workspace_binding(
            tenant_key=inbound.tenant_key,
            chat_id=inbound.chat_id,
            open_id=inbound.sender_open_id,
        )
        if binding is None or binding.bowhead_user_id != bowhead_user_id:
            return None
        return next(
            (workspace for workspace in workspaces if str(workspace.get("workspace_id") or "") == binding.workspace_id),
            None,
        )

    async def _handle_workspace_select(
        self,
        *,
        tenant_key: str,
        open_id: str,
        value: dict[str, Any],
        locale: object,
    ) -> dict[str, Any]:
        chat_id = str(value.get("chat_id") or "")
        workspace_id = str(value.get("workspace_id") or "")
        if not (tenant_key and open_id and chat_id and workspace_id):
            return _toast(im_interface_message("space_selection_missing", locale), level="warning")
        binding = await self.store.get_binding(tenant_key=tenant_key, open_id=open_id)
        if binding is None:
            return _toast(im_interface_message("binding_required", locale), level="warning")
        user_store = getattr(self.app.state, "user_store", None)
        if user_store is None:
            return _toast(im_interface_message("users_unavailable", locale), level="warning")
        workspaces = await user_store.list_workspaces_for_user(binding.bowhead_user_id)
        selected = next(
            (workspace for workspace in workspaces if str(workspace.get("workspace_id") or "") == workspace_id),
            None,
        )
        if selected is None:
            return _toast(im_interface_message("space_access_lost", locale), level="warning")
        datarc_workspace_id = str(selected.get("datarc_workspace_id") or "")
        if not datarc_workspace_id or datarc_workspace_id.startswith("ws_"):
            return _toast(im_interface_message("space_no_datarc", locale), level="warning")
        await self.store.upsert_workspace_binding(
            tenant_key=tenant_key,
            chat_id=chat_id,
            open_id=open_id,
            bowhead_user_id=binding.bowhead_user_id,
            workspace_id=workspace_id,
        )
        try:
            await user_store.touch_workspace_member_active(workspace_id, binding.bowhead_user_id)
        except Exception as exc:
            logger.debug("feishu.workspace_touch_failed ws=%s err=%s", workspace_id, exc)
        return _toast(im_interface_message("space_selected", locale), level="success")

    async def _resolve_member_uid(
        self,
        *,
        identity: str,
        datarc_workspace_id: str,
        team_id: str | None = None,
    ) -> str | None:
        resolver = getattr(self.app.state, "member_resolver", None)
        datarc_client, unavailable, _vector = await self._resolve_team_runtime_clients(team_id)
        if unavailable or datarc_client is None:
            return None
        if resolver is not None and identity:
            try:
                try:
                    return await resolver.resolve(identity, datarc_workspace_id, datarc=datarc_client)
                except TypeError:
                    # Test and adapter implementations that predate the explicit
                    # client parameter may hold a team-scoped client themselves.
                    return await resolver.resolve(identity, datarc_workspace_id)
            except MemberNotFoundError:
                logger.warning(
                    "feishu.member_uid_not_found identity=%s ws=%s",
                    _redact_identity(identity),
                    datarc_workspace_id,
                )
                return None
            except Exception as exc:
                logger.warning(
                    "feishu.member_uid_resolve_failed identity=%s ws=%s err=%s",
                    _redact_identity(identity),
                    datarc_workspace_id,
                    exc,
                )
                raise
        return None

    def _datarc_member_identity(self, *, user: Any, email: str | None) -> str:
        """Datarc member lookup uses the bound user's verified email."""
        return str(email or getattr(user, "email", "") or getattr(user, "id", "") or "")

    async def _resolve_im_language_context(self, bound: Any, text: str, session_id: str) -> Any:
        """Resolve this turn's language through the shared channel resolver.

        Without saved account preferences or a team default/lock, the turn follows the
        message language, then the session's previous answer language, then English.
        """
        from bowhead_gateway.channel_language import resolve_channel_turn_language

        return await resolve_channel_turn_language(
            self.app,
            user_id=bound.user_id,
            workspace_id=bound.bowhead_workspace_id,
            team_id=bound.team_id,
            session_id=session_id,
            message_text=text or "",
        )

    async def _resolve_team_runtime_clients(
        self,
        team_id: str | None,
    ) -> tuple[Any | None, bool, Any | None]:
        """Mirror Web chat's per-team Datarc resolution for Feishu turns.

        A configured-but-broken team Datarc connection must fail closed via
        datarc_unavailable=True; otherwise turn_runner would fall back to the process-global
        Datarc client and break tenant isolation.
        """
        if not team_id:
            return None, False, None
        from bowhead_gateway._turn_runtime import _resolve_turn_datarc_runtime

        request = SimpleNamespace(app=self.app)
        datarc_client, datarc_unavailable, vector_client = await _resolve_turn_datarc_runtime(request, team_id)
        return datarc_client, datarc_unavailable, vector_client

    async def _preflight_credits_and_entitlement(self, bound: FeishuBoundUser, *, locale: str) -> str | None:
        if not bound.team_id:
            return None
        from bowhead_gateway.credit_admission import check_credit_admission

        credit_denial = await check_credit_admission(
            getattr(self.app.state, "user_store", None),
            team_id=bound.team_id,
            user_id=bound.user_id,
            workspace_id=bound.bowhead_workspace_id,
            metrics_log=getattr(self.app.state, "metrics_log", None),
        )
        if credit_denial == "user_credit_exhausted":
            return im_error_message(credit_denial, locale)
        if credit_denial == "team_pool_exhausted":
            return im_error_message(credit_denial, locale)
        entitlements = getattr(self.app.state, "entitlements", None)
        if entitlements is not None:
            try:
                await entitlements.assert_can_run_turn(bound.team_id)
            except Exception as exc:
                logger.warning("feishu.entitlement_denied team=%s error_type=%s", bound.team_id, type(exc).__name__)
                return im_error_message("entitlement_denied", locale)
        return None

    async def _resolve_team_router(
        self,
        team_id: str | None,
        *,
        entrypoint: Literal["clarification", "tool_continuation"] = "tool_continuation",
    ) -> Any:
        """Resolve the team's Router for this physical turn, or fail closed.

        Returned router is carried on the TurnRequest (``team_router``), NOT via reconstructing
        the shared Orchestrator singleton — ``dataclasses.replace(orch, router=…)`` dropped orch's
        dynamically-attached sidecars (subagent_runner / plan_store / …), crashing every BYO-team
        turn's subagent_spawn. See turn_runner._effective_router.
        """
        if not team_id:
            return None
        from bowhead_gateway.turn_service import resolve_team_model_runtime

        resolved = await resolve_team_model_runtime(
            self.app.state,
            team_id,
            entrypoint=entrypoint,
        )
        if not resolved.available:
            raise RuntimeError("team_model_runtime_unavailable")
        return resolved.router

    def _start_mirror(
        self,
        *,
        turn_id: str,
        tenant_key: str,
        card_message_id: str | None,
        source_message_id: str,
        sender_open_id: str,
        locale: str | None = None,
        answer_locale: str | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._mirror_turn_events(
                turn_id=turn_id,
                tenant_key=tenant_key,
                card_message_id=card_message_id,
                source_message_id=source_message_id,
                sender_open_id=sender_open_id,
                locale=locale,
                answer_locale=answer_locale,
            ),
            name=f"feishu_mirror:{turn_id}",
        )
        self._mirror_tasks.add(task)
        task.add_done_callback(self._mirror_tasks.discard)

    async def _mirror_turn_events(
        self,
        *,
        turn_id: str,
        tenant_key: str,
        card_message_id: str | None,
        source_message_id: str,
        sender_open_id: str,
        locale: str | None = None,
        answer_locale: str | None = None,
    ) -> None:
        # Turn cards use the turn's language snapshot; mirrors started without one fall back
        # to the shared resolver without a message. The answer card's title and empty-answer
        # fallback follow the answer language; everything else follows the interface language.
        if not locale:
            locale = await self._presentation_locale(tenant_key=tenant_key, open_id=sender_open_id)
        answer_locale = answer_locale or locale
        state = renderer.FeishuTurnCardState(locale=locale)
        # Accumulate the canvas section tree + citations the old renderer
        # drops, so the answer card can carry the trust line + sources (§4.1/§4.3).
        neutral = NeutralTurnState(turn_id=turn_id)
        answer_message_id: str | None = None
        last_patch_at = 0.0
        pending_patch = False
        bus = await get_event_registry().get_or_create(turn_id, producer=False)
        async for event in bus.subscribe():
            payload = event.payload if isinstance(event.payload, dict) else {}
            apply_event(neutral, event)  # fold canvas.section_*/citation/etc. into the neutral state
            if event.event_type == "canvas.opened" or event.event_type in {
                "done",
                "error",
                "turn_failed",
                "turn_aborted",
            }:
                await refresh_report_lifecycle(neutral, getattr(self.app.state, "canvas_store", None))
            if event.event_type == "clarification_required":
                await self._reply_card(
                    source_message_id,
                    renderer.clarification_card(payload, allowed_open_id=sender_open_id, locale=locale),
                    uuid=f"feishu_clarify_{payload.get('request_id') or turn_id}",
                    tenant_key=tenant_key,
                )
                continue
            if event.event_type in {"approval.required", "approval_required"}:
                await self._reply_card(
                    source_message_id,
                    renderer.error_card(
                        title=im_interface_message("approval_web_title", locale),
                        body=im_interface_message("approval_web_body", locale),
                    ),
                    tenant_key=tenant_key,
                )
                continue
            changed = renderer.apply_event_to_state(state, event.event_type, payload)
            if changed:
                pending_patch = True
            if event.event_type == "final_answer":
                answer = state.final_answer or str(payload.get("text") or "")
                answer = answer_text_for_card(neutral, answer)
                _link = await self.store.get_turn_link(turn_id)
                _chat_type = str((getattr(_link, "meta", None) or {}).get("chat_type") or "")
                await refresh_kb_citation_labels(neutral, self.app, private_chat=_chat_type == "p2p")
                source_linkage_line, lifecycle_line, caveat, sources_heading, sources_lines = _neutral_trust_parts(
                    neutral,
                    locale=locale,
                )
                resp = await self._reply_card(
                    source_message_id,
                    renderer.answer_card(
                        answer,
                        artifacts=state.inline_artifacts,
                        locale=locale,
                        answer_locale=answer_locale,
                        trust_line=source_linkage_line,
                        lifecycle_line=lifecycle_line,
                        caveat=caveat,
                        sources_heading=sources_heading,
                        sources_lines=sources_lines,
                    ),
                    uuid=f"feishu_answer_{turn_id}",
                    tenant_key=tenant_key,
                )
                answer_message_id = _response_message_id(resp)
                if answer_message_id:
                    await self.store.set_answer_message_id(
                        turn_id=turn_id,
                        answer_message_id=answer_message_id,
                    )
            if pending_patch and card_message_id:
                terminal = event.event_type in {
                    "final_answer",
                    "step_list_card_finalized",
                    "turn_failed",
                    "turn_aborted",
                    "error",
                    "done",
                }
                now = asyncio.get_event_loop().time()
                if terminal or now - last_patch_at >= 1.0:
                    await self._patch_card(
                        card_message_id,
                        renderer.turn_status_card(state),
                        tenant_key=tenant_key,
                    )
                    last_patch_at = now
                    pending_patch = False
            if event.event_type in {"done", "error", "turn_failed", "turn_aborted"}:
                if card_message_id and pending_patch:
                    await self._patch_card(
                        card_message_id,
                        renderer.turn_status_card(state),
                        tenant_key=tenant_key,
                    )
                # Late canvas sections (arriving after final_answer) → refresh the answer card's
                # trust line + sources so a report turn's card isn't stale (best-effort patch).
                if answer_message_id and neutral.section_list():
                    source_linkage_line, lifecycle_line, caveat, sources_heading, sources_lines = _neutral_trust_parts(
                        neutral,
                        locale=locale,
                    )
                    await self._patch_card(
                        answer_message_id,
                        renderer.answer_card(
                            state.final_answer or "",
                            locale=locale,
                            answer_locale=answer_locale,
                            artifacts=state.inline_artifacts,
                            trust_line=source_linkage_line,
                            lifecycle_line=lifecycle_line,
                            caveat=caveat,
                            sources_heading=sources_heading,
                            sources_lines=sources_lines,
                        ),
                        tenant_key=tenant_key,
                    )
                await self.store.mark_message_status(
                    message_id=source_message_id,
                    status="done" if event.event_type == "done" else "failed",
                )
                break

    async def _bound_user_scope(self, *, tenant_key: str, open_id: str) -> tuple[str | None, str | None]:
        """(user_id, team_id) of the Bowhead account bound to this Feishu identity, if any."""
        user_id: str | None = None
        team_id: str | None = None
        try:
            binding = await self.store.get_binding(tenant_key=tenant_key, open_id=open_id)
            user_id = binding.bowhead_user_id if binding is not None else None
            user_store = getattr(self.app.state, "user_store", None)
            if user_id and user_store is not None:
                team_id = getattr(await user_store.get_user(user_id), "team_id", None)
        except Exception:  # noqa: BLE001 - The lookups only select the copy language.
            logger.warning("feishu.copy_locale_lookup_failed tenant=%s", tenant_key)
        return user_id, team_id

    async def _copy_locale(
        self,
        *,
        tenant_key: str,
        open_id: str,
        message_text: str = "",
        rendered_locale: object = None,
        session_id: str | None = None,
    ) -> str:
        """Locale for out-of-turn copy via the shared channel resolver; a card's snapshot wins."""
        user_id, team_id = await self._bound_user_scope(tenant_key=tenant_key, open_id=open_id)
        return await resolve_channel_copy_locale(
            self.app,
            user_id=user_id,
            team_id=team_id,
            message_text=message_text,
            rendered_locale=rendered_locale,
            session_id=session_id,
        )

    async def _user_copy_locale(self, user: Any, inbound: FeishuInboundMessage) -> str:
        """Out-of-turn copy locale for a known Bowhead user and an incoming message."""
        return await resolve_channel_copy_locale(
            self.app,
            user_id=getattr(user, "id", None),
            message_text=inbound.text,
            team_id=getattr(user, "team_id", None),
            session_id=self._session_id(inbound),
        )

    async def _presentation_locale(self, *, tenant_key: str, open_id: str) -> str:
        """Fallback locale for a mirror started without a turn snapshot.

        Uses the shared channel resolver without a message: the saved interface preference,
        then English. The account ``users.locale`` column is not read.
        """
        return await self._copy_locale(tenant_key=tenant_key, open_id=open_id)

    async def _handle_clarification_resolve(
        self,
        *,
        tenant_key: str,
        open_id: str,
        value: dict[str, Any],
        locale: object,
    ) -> dict[str, Any]:
        request_id = str(value.get("request_id") or "")
        if not request_id:
            return _toast(im_interface_message("clarification_missing", locale), level="warning")
        binding = await self.store.get_binding(tenant_key=tenant_key, open_id=open_id)
        if binding is None:
            return _toast(im_interface_message("binding_required", locale), level="warning")
        clarify = getattr(self.app.state, "clarification_store", None)
        pre = await clarify.get(request_id) if clarify is not None else None
        if pre is not None:
            from bowhead_gateway.turn_service import (
                TerminalProtocolSurfaceUnavailable,
                require_terminal_surface_capability,
            )

            try:
                await require_terminal_surface_capability(
                    self.app,
                    pre,
                    user_id=binding.bowhead_user_id,
                    surface="feishu",
                )
            except TerminalProtocolSurfaceUnavailable:
                return _toast(im_interface_message("continue_web", locale), level="warning")

            # Same position as Web /resolve: block credit before consuming the answer; leave the card pending,
            # so clicking the same card after recharge resumes. Keep the same-criterion gate in `_resume_clarification_turn` as fallback.
            from bowhead_gateway.turn_service import (
                clarification_requires_legacy_resume_fence,
            )

            if clarification_requires_legacy_resume_fence(pre):
                from bowhead_gateway.credit_admission import check_credit_admission

                credit_store = getattr(self.app.state, "user_store", None)
                credit_team_id = None
                if credit_store is not None:
                    credit_user = await credit_store.get_user(binding.bowhead_user_id)
                    credit_team_id = getattr(credit_user, "team_id", None)
                credit_denial = await check_credit_admission(
                    credit_store,
                    team_id=credit_team_id,
                    user_id=binding.bowhead_user_id,
                    workspace_id=pre.workspace_id,
                    metrics_log=getattr(self.app.state, "metrics_log", None),
                    turn_id=pre.turn_id,
                )
                if credit_denial is not None:
                    return _toast(im_error_message(credit_denial, locale), level="warning")
        result = await self._resolve_clarification(
            request_id=request_id,
            user_id=binding.bowhead_user_id,
            chosen_value=value.get("chosen_value"),
            chosen_values=value.get("chosen_values"),
        )
        if result.get("action") == "turn_resume":
            error_code = await self._resume_clarification_turn(
                request_id,
                team_router=result.get("_team_router"),
            )
            if error_code is not None:
                message = im_error_message(error_code, locale)
                return _toast(im_interface_message("choice_resume_failed", locale, message=message), level="warning")
        return _toast(im_interface_message("choice_submitted", locale), level="success")

    async def _resolve_clarification(
        self,
        *,
        request_id: str,
        user_id: str,
        chosen_value: Any = None,
        chosen_values: Any = None,
    ) -> dict[str, Any]:
        clarify = getattr(self.app.state, "clarification_store", None)
        if clarify is None:
            raise RuntimeError("clarification_store not configured")
        pre = await clarify.get(request_id)
        if not pre:
            raise ValueError("clarification not found")
        if pre.user_id != user_id:
            raise PermissionError("clarification belongs to another user")
        if pre.status not in {"pending", "resolved"}:
            raise ValueError("clarification_not_resolvable")
        from bowhead_gateway.turn_service import (
            clarification_requires_legacy_resume_fence,
            wait_for_legacy_turn_resume_ready,
        )

        resume_context = pre.resume_context if isinstance(pre.resume_context, dict) else None
        is_resume = clarification_requires_legacy_resume_fence(pre)
        resume_team_router = None
        if is_resume:
            # Fail closed before ClarificationStore.resolve consumes the card.
            # The exact router is then passed through to the worker so a config
            # failure cannot surface only after the callback says success.
            resume_team_router = await self._resolve_team_router(
                resume_context.get("team_id"),
                entrypoint="clarification",
            )
            if pre.status == "pending":
                orchestrator = getattr(self.app.state, "orchestrator", None)
                session_store = getattr(orchestrator, "session_store", None)
                if not await wait_for_legacy_turn_resume_ready(
                    session_store,
                    pre.turn_id,
                    timeout_seconds=0.0,
                ):
                    raise RuntimeError("clarification_resume_not_ready")
        values: list[str] = []
        if isinstance(chosen_values, list):
            values = [str(v) for v in chosen_values if v]
        elif chosen_value:
            values = [str(chosen_value)]
        if not values:
            raise ValueError("empty clarification choice")
        primary = values[0]
        resolution_owner = f"feishu_{uuid4().hex}"
        req, transitioned = await clarify.resolve_once(
            request_id,
            primary,
            chosen_values=values,
            resolution_channel="feishu",
            resolution_work={"chosen_values": values},
            resolution_owner=resolution_owner,
        )
        if req is None:
            raise ValueError("clarification not found")
        values = list(req.chosen_values or ([req.chosen_value] if req.chosen_value else []))
        primary = req.chosen_value or (values[0] if values else "")
        if not transitioned:
            claim_work = getattr(clarify, "claim_resolution_work", None)
            if callable(claim_work):
                transitioned = (
                    await claim_work(
                        request_id=request_id,
                        channel="feishu",
                        resolution_owner=resolution_owner,
                    )
                    is not None
                )
        event_payload = {
            "request_id": request_id,
            "chosen_value": primary,
            "chosen_values": values,
            "kind": req.kind,
            "workspace_id": req.workspace_id,
        }
        if transitioned:
            event_payload = await clarify.record_resolution_outcome(
                request_id=request_id,
                turn_id=req.turn_id,
                payload=event_payload,
                resolution_owner=resolution_owner,
            )
        else:
            for _ in range(100):
                stored_outcome = await clarify.get_resolution_outcome(request_id)
                if stored_outcome is not None:
                    event_payload = stored_outcome
                    break
                await asyncio.sleep(0.02)
            else:
                raise RuntimeError("clarification_resolution_pending")
        # dingsiyan #11: same boundary as Web resolve_one; post-pause emits must be out-of-band
        # (closed-bus fallback + seq seeding), or persisted payload seq collides and Web replay deduplication drops events.
        # Batch A resume receipt: persist-before-delivery failure must propagate; card cannot return success
        # before a durable resolved event / resume handoff is established.
        try:
            from bowhead_gateway.turn_service import (
                emit_clarification_resolution_once,
            )

            await emit_clarification_resolution_once(
                self.app.state,
                turn_id=req.turn_id,
                request_id=request_id,
                payload=event_payload,
            )
        except Exception as e:
            logger.warning(
                "feishu.clarify_resolved_emit_failed req=%s err=%s",
                request_id,
                e,
            )
            raise
        action = "noop"
        if clarification_requires_legacy_resume_fence(req):
            action = "turn_resume"
        return {
            "action": action,
            "turn_id": req.turn_id,
            "_team_router": resume_team_router,
        }

    async def _resume_clarification_turn(
        self,
        request_id: str,
        *,
        team_router: Any = None,
    ) -> str | None:
        clarify = getattr(self.app.state, "clarification_store", None)
        orchestrator: Orchestrator | None = getattr(self.app.state, "orchestrator", None)
        if clarify is None or orchestrator is None:
            raise RuntimeError("clarification resume dependencies not configured")
        req = await clarify.get(request_id)
        if req is None or req.status != "resolved":
            return None
        rc = req.resume_context if isinstance(req.resume_context, dict) else None
        if not rc:
            return None
        # Reconstruct the resume TurnRequest via the channel-neutral helper shared with the web
        # resume path; the user_text merge (B1) + field reconstruction live in one place.
        datarc_client, datarc_unavailable, vector_client = await self._resolve_team_runtime_clients(rc.get("team_id"))
        from bowhead_gateway.turn_service import build_resume_turn_request

        if rc.get("team_id") and team_router is None:
            team_router = await self._resolve_team_router(
                rc.get("team_id"),
                entrypoint="clarification",
            )

        turn_req = build_resume_turn_request(
            req,
            datarc_client=datarc_client,
            datarc_unavailable=datarc_unavailable,
            vector_client=vector_client,
            team_router=team_router,
        )
        if turn_req is None:
            return None
        # feedback-0713 F2: IM resume (clarification-card click) follows new-message policy: revive soft-deleted sessions,
        # rather than reject (deterministic IM thread binding; see handle_message comment).
        if turn_req.session_id:
            from bowhead_gateway.session_lifecycle import revive_session_for_im

            lifecycle_result = await revive_session_for_im(
                self.app,
                orchestrator,
                session_id=turn_req.session_id,
                user_id=turn_req.user_id,
                request_id=f"feishu-resume:{request_id}",
            )
            if lifecycle_result.error_code is not None:
                logger.warning(
                    "feishu.session_revive_rejected session=%s code=%s",
                    turn_req.session_id,
                    lifecycle_result.error_code,
                )
                return lifecycle_result.error_code
            if lifecycle_result.changed:
                logger.info("feishu.session_revived_on_resume session=%s", turn_req.session_id)
        # Resume via the channel-neutral TurnService shared with the web clarifications route.
        # The bus generation guard (close → get_or_create) lives inside
        # spawn_resume_turn; idempotency is checked first so a duplicate resume doesn't re-close
        # the live resume's bus.
        from bowhead_gateway.turn_service import (
            resume_already_in_flight,
            spawn_resume_turn,
        )

        if resume_already_in_flight(turn_req.turn_id):
            return None

        # Resume is new model work, using new-message credit admission; render returned codes through im_error_message.
        from bowhead_gateway.credit_admission import check_credit_admission

        credit_denial = await check_credit_admission(
            getattr(self.app.state, "user_store", None),
            team_id=rc.get("team_id"),
            user_id=turn_req.user_id,
            workspace_id=turn_req.workspace_id,
            metrics_log=getattr(self.app.state, "metrics_log", None),
            turn_id=turn_req.turn_id,
        )
        if credit_denial is not None:
            return credit_denial

        # Restart the card mirror once the fresh bus exists (on_bus_ready), so it subscribes
        # before the resume worker emits.
        # dingsiyan #11: Feishu resume previously omitted initial_seq seeding (considering only that card mirroring
        # itself does not deduplicate by seq), but persisted payload seq restarted at 0 and collided with earlier turn rows.
        # Later **Web** F5/reconnect replay in the same session would incorrectly drop resume events through FE seq
        # deduplication (cross-channel boundary). Seed consistently through turn_seq_seed.
        from bowhead_gateway.turn_service import turn_seq_seed

        _resume_initial_seq = await turn_seq_seed(
            getattr(self.app.state, "turn_log_store", None),
            turn_req.turn_id,
        )
        link = await self.store.get_turn_link(turn_req.turn_id)

        def _restart_mirror() -> None:
            if link is not None:
                self._start_mirror(
                    turn_id=turn_req.turn_id,
                    tenant_key=link.tenant_key,
                    card_message_id=link.card_message_id,
                    source_message_id=link.source_message_id,
                    sender_open_id=link.sender_open_id,
                    locale=getattr(turn_req.language_context, "ui_language", None),
                    answer_locale=getattr(turn_req.language_context, "output_language", None),
                )

        team_id = rc.get("team_id")
        user_store = getattr(self.app.state, "user_store", None)
        spawned = await spawn_resume_turn(
            orchestrator,
            turn_req,
            user_store=user_store if team_id else None,
            team_id=team_id,
            entitlements=getattr(self.app.state, "entitlements", None),
            on_bus_ready=_restart_mirror,
            initial_seq=_resume_initial_seq,
            # turn.resumed placeholder (Web FE tombstone + silent-window fill; mirror fold tolerates unknown events).
            resume_notice={
                "request_id": request_id,
                "chosen_values": list(req.chosen_values or ([req.chosen_value] if req.chosen_value else [])),
                "skipped": False,
            },
        )
        logger.info(
            "feishu.clarification_resume_spawned turn=%s execution_generation=%d",
            turn_req.turn_id,
            spawned.execution_generation,
        )
        return None

    async def _handle_retry_skip(self, *, value: dict[str, Any], action_name: str, locale: object) -> dict[str, Any]:
        turn_id = str(value.get("turn_id") or "")
        step_id = str(value.get("step_id") or "")
        if not (turn_id and step_id):
            return _toast(im_interface_message("step_missing", locale), level="warning")
        store = getattr(self.app.state, "turn_log_store", None)
        if store is None:
            return _toast(im_interface_message("step_service_unavailable", locale), level="warning")
        from starlette.requests import Request as StarletteRequest

        scope = {"type": "http", "app": self.app, "headers": [], "method": "POST", "path": "/"}
        req = StarletteRequest(scope)
        try:
            if action_name == "retry_step":
                from bowhead_gateway.routes.turns import (
                    StepRetryRequest,
                    retry_step,
                )

                await retry_step(
                    turn_id,
                    step_id,
                    req,
                    StepRetryRequest(reason="feishu_card"),
                    store,
                )
            else:
                from bowhead_gateway.routes.turns import StepSkipRequest, skip_step

                await skip_step(
                    turn_id,
                    step_id,
                    req,
                    StepSkipRequest(reason="feishu_card"),
                    store,
                )
        except Exception as exc:
            logger.warning("feishu.retry_skip_failed action=%s err=%s", action_name, exc)
            return _toast(im_interface_message("step_ack_failed", locale), level="warning")
        return _toast(im_interface_message("step_ack", locale), level="success")

    async def _send_onboarding(
        self,
        inbound: FeishuInboundMessage,
        *,
        title: str,
        body: str,
    ) -> None:
        await self._send_user_scoped_card(
            inbound,
            renderer.onboarding_card(title=title, body=body),
        )

    async def _send_im_error_card(
        self,
        inbound: FeishuInboundMessage,
        *,
        code: str,
        locale: object,
    ) -> None:
        from bowhead_gateway.im.error_messages import im_error_labels, im_error_message

        labels = im_error_labels(locale)
        await self._send_user_scoped_card(
            inbound,
            renderer.error_card(
                title=labels["error_title"],
                body=im_error_message(code, locale),
            ),
        )

    async def _send_user_scoped_card(
        self,
        inbound: FeishuInboundMessage,
        card: dict[str, Any],
    ) -> None:
        try:
            if inbound.chat_type in {"group", "chat"}:
                await self.client.send_ephemeral_card(
                    chat_id=inbound.chat_id,
                    open_id=inbound.sender_open_id,
                    card=card,
                    tenant_key=inbound.tenant_key,
                )
                return
        except Exception as exc:
            logger.info("feishu.ephemeral_card_failed fallback_reply err=%s", exc)
        await self._reply_card(inbound.message_id, card, tenant_key=inbound.tenant_key)

    async def _reply_card(
        self,
        message_id: str,
        card: dict[str, Any],
        *,
        uuid: str | None = None,
        tenant_key: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await self.client.reply_message(
                message_id=message_id,
                msg_type="interactive",
                content=card,
                tenant_key=tenant_key,
                uuid=uuid,
            )
        except FeishuAPIError:
            raise
        except Exception as exc:
            logger.warning("feishu.reply_card_failed msg=%s err=%s", message_id, exc)
            return {}

    async def _patch_card(
        self,
        message_id: str,
        card: dict[str, Any],
        *,
        tenant_key: str | None = None,
    ) -> None:
        try:
            await self.client.patch_card(message_id=message_id, card=card, tenant_key=tenant_key)
        except Exception as exc:
            logger.warning("feishu.patch_card_failed msg=%s err=%s", message_id, exc)

    async def _best_effort_email(
        self,
        open_id: str,
        *,
        tenant_key: str | None = None,
    ) -> str | None:
        try:
            return await self.client.get_user_email(open_id=open_id, tenant_key=tenant_key)
        except Exception as exc:
            logger.info("feishu.email_lookup_failed open_id=%s err=%s", open_id, exc)
            return None

    async def _binding_session_url(self, session_id: str, *, tenant_key: str) -> str:
        path = f"/me/integrations/feishu/bind/?session={quote(session_id, safe='')}"
        public_entry_store = getattr(self.app.state, "public_entry_store", None)
        if public_entry_store is None:
            return public_app_url(path)

        stored_config = await self.store.get_app_config(tenant_key=tenant_key)
        if stored_config is None and tenant_key:
            stored_config = await self.store.get_app_config(tenant_key="")
        if stored_config is not None:
            entry = await public_entry_store.default_for_team(stored_config.team_id)
            if entry is None:
                raise RuntimeError("Feishu team has no active default public entry")
            return public_app_url_for_base(entry.base_url, path)

        defaults = [entry for entry in await public_entry_store.list_active() if entry.is_default]
        if len(defaults) != 1:
            raise RuntimeError("Feishu tenant cannot be mapped to one public entry")
        return public_app_url_for_base(defaults[0].base_url, path)

    async def _best_effort_reaction(
        self,
        message_id: str,
        *,
        tenant_key: str | None = None,
    ) -> None:
        try:
            await self.client.add_reaction(
                message_id=message_id,
                emoji_type="FISTBUMP",
                tenant_key=tenant_key,
            )
        except Exception as exc:
            logger.debug("feishu.reaction_failed msg=%s err=%s", message_id, exc)

    @staticmethod
    def _session_id(inbound: FeishuInboundMessage) -> str:
        return f"feishu:{inbound.tenant_key}:{inbound.chat_id}:{inbound.sender_open_id}"


def answer_text_for_card(neutral: NeutralTurnState, answer: str) -> str:
    """报告轮：卡片的来源列表来自报告自己的 sources 章（另一套编号），对话摘要里的 [cite:N]
    与之对不上 ⇒ 摘要去掉角标，出处以报告为准。对话轮原样（来源列表与角标同一套编号）。"""
    if not neutral.section_list():
        return answer
    from bowhead_core.citation_marks import CITE_MARKER_RE

    return CITE_MARKER_RE.sub("", answer)


def _neutral_trust_parts(
    neutral: NeutralTurnState,
    *,
    locale: object = None,
) -> tuple[str | None, str | None, str | None, str | None, list[str] | None]:
    """Extract the Feishu answer-card trust strings from the accumulated neutral state.

    Reuses the fully-tested ``render_blocks`` fold (trust computation + caveat aggregation + sources)
    and formats only the source-linkage/lifecycle/Caveat/Sources blocks for the Feishu card; section text blocks
    are ignored (the Feishu card keeps the chat answer + chart chip; the full report lives on canvas).
    Returns five empty slots for a chat turn (no sections / silent) so the card stays clean.
    """
    selected_locale = resolve_presentation_locale(locale)
    source_linkage_line: str | None = None
    lifecycle_line: str | None = None
    caveat: str | None = None
    sources_heading: str | None = None
    sources_lines: list[str] | None = None
    for block in render_blocks(neutral, locale=selected_locale):
        if isinstance(block, SourceLinkageLine):
            source_linkage_line = format_source_linkage(block.presentation, block.locale)
        elif isinstance(block, ReportLifecycleLine):
            lifecycle_line = format_report_lifecycle(block.effective_status, block.locale)
        elif isinstance(block, Caveat):
            caveat = format_figure_review_caveat(block.items, block.locale)
        elif isinstance(block, SourcesBlock):
            sources_heading, sources_lines = format_source_lines(block.entries, block.locale)
    return source_linkage_line, lifecycle_line, caveat, sources_heading, sources_lines


def _response_message_id(resp: dict[str, Any]) -> str | None:
    data = resp.get("message") if isinstance(resp.get("message"), dict) else resp
    value = data.get("message_id") if isinstance(data, dict) else None
    return str(value) if value else None


def _toast(content: str, *, level: str = "info") -> dict[str, Any]:
    return {"toast": {"type": level, "content": content}}


def _is_workspace_switch_command(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    return normalized in {
        "/workspace",
        "/switch workspace",
        "/switch_workspace",
        "switch workspace",
        "切换 workspace",
        "切换workspace",
        "切换工作区",
        "选择 workspace",
        "选择workspace",
        "选择工作区",
    }


def _feishu_user_unavailable_reason(user: Any, locale: str) -> str | None:
    if getattr(user, "status", "active") != "active":
        return im_interface_message("account_unavailable", locale)
    if os.environ.get("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0") == "1" and not bool(getattr(user, "email_verified", False)):
        return im_interface_message("email_unverified", locale)
    return None


def _normalize_email(value: Any) -> str | None:
    if not value:
        return None
    email = str(value).strip().lower()
    return email or None


def _redact_identity(value: str) -> str:
    if not value:
        return ""
    if "@" in value:
        head, _, domain = value.partition("@")
        return f"{head[:2]}***@{domain}"
    return f"{value[:8]}..."
