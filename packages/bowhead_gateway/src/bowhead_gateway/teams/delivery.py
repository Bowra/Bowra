"""Deliver committed Teams Turn events, resuming from a leased SQLite cursor."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import httpx2
from bowhead_core.llm_config_crypto import decrypt_llm_config, encrypt_llm_config
from bowhead_core.space_governance import SpaceOperation
from fastapi import HTTPException

from bowhead_gateway.access_context import AccessContextError, build_access_context
from bowhead_gateway.im.turn_render import NeutralTurnState, apply_event

log = logging.getLogger(__name__)


async def track_delivery(app, key, activity, connection, turn, locale, answer_locale):
    reference = {
        "tenant": connection.entra_tenant_id,
        "app": connection.microsoft_app_id,
        "connection": connection.id,
        "team": turn.team_id,
        "space": turn.workspace_id,
        "user": turn.user_id,
        "session": turn.session_id,
        "turn": turn.turn_id,
        "oid": activity["from"]["aadObjectId"],
        "conversation": activity["conversation"]["id"],
        "service_url": activity["serviceUrl"],
        "locale": locale,
        "answer_locale": answer_locale,
    }
    frozen = decrypt_llm_config(await app.state.teams_store.track_delivery(key, encrypt_llm_config(reference), turn.user_id))
    if any(frozen[field] != reference[field] for field in ("team", "space", "user", "session", "turn", "oid", "conversation", "connection")):
        raise HTTPException(409, {"error": {"code": "teams_activity_scope_changed"}})


async def deliver_next(app):
    from bowhead_gateway.routes.teams import _send_activity, event_card

    store = app.state.teams_store
    owner = uuid4().hex
    row = await store.take_delivery(owner)
    if row is None:
        return False
    seq = row["delivery_seq"]
    delay = 3
    try:
        ref = decrypt_llm_config(row["delivery"])
        connection = await store.get_enabled_connection(ref["app"], ref["tenant"])
        binding = await store.get_identity(ref["tenant"], ref["oid"], ref["team"])
        if connection is None or connection.id != ref["connection"] or connection.team_id != ref["team"] or binding != (ref["user"], ref["team"]):
            delay = None
            return True
        sessions = app.state.orchestrator.session_store
        turn = await sessions.get_turn(ref["turn"])
        if turn is None:
            if datetime.now(UTC) - datetime.fromisoformat(row["received_at"]) > timedelta(minutes=10):
                delay = None
            return True
        session = await sessions.get_session(ref["session"])
        if (
            turn.session_id != ref["session"]
            or session is None
            or session.user_id != ref["user"]
            or session.workspace_id != ref["space"]
            or session.deleted_at is not None
        ):
            delay = None
            return True
        await build_access_context(
            user_store=app.state.user_store,
            access_store=getattr(app.state, "access_control_store", None),
            user_id=ref["user"],
            workspace_id=ref["space"],
            expected_team_id=ref["team"],
            operation=SpaceOperation.READ_HISTORY,
        )
        selected = await store.get_workspace(ref["tenant"], ref["conversation"], ref["oid"], ref["team"])
        if selected and selected != ref["space"]:
            delay = 30
            return True
        logs = app.state.turn_log_store
        if await logs.max_seq(ref["turn"]) <= seq and turn.status in {"running", "clarification_pending"}:
            delay = 30 if turn.status == "clarification_pending" else 3
            return True
        events = await logs.list_events(ref["turn"])
        neutral = NeutralTurnState(turn_id=ref["turn"])
        for event in events:
            apply_event(neutral, event)
            if event.seq <= seq:
                continue
            if event.event_type == "clarification_required" and (request_id := event.payload.get("request_id")):
                question = await app.state.clarification_store.get(request_id)
                if question and (
                    question.status != "pending"
                    or (question.user_id, question.workspace_id, question.turn_id) != (ref["user"], ref["space"], ref["turn"])
                ):
                    seq = event.seq
                    continue
            card = await event_card(app, neutral, event, ref["locale"], ref["answer_locale"])
            if card:
                if event.event_type == "clarification_required":
                    from bowhead_gateway.im.error_messages import im_error_labels
                    from bowhead_gateway.public_url import PublicEntryRequiredError, team_public_app_url

                    try:
                        url = await team_public_app_url(
                            SimpleNamespace(app=app), team_id=ref["team"], path="/?" + urlencode({"session": ref["session"]})
                        )
                    except PublicEntryRequiredError:
                        pass
                    else:
                        card.setdefault("actions", []).append(
                            {"type": "Action.OpenUrl", "url": url, "title": im_error_labels(ref["answer_locale"])["open_web"]}
                        )
                await _send_activity(
                    SimpleNamespace(app=app), {"serviceUrl": ref["service_url"], "conversation": {"id": ref["conversation"]}}, connection, card
                )
                seq = event.seq
                delay = 0
                break
            seq = event.seq
        else:
            if neutral.terminal and neutral.terminal_status != "clarification_pending" and turn.status not in {"running", "clarification_pending"}:
                delay = None
    except AccessContextError as exc:
        delay = 30 if exc.status_code >= 500 else None
    except httpx2.HTTPStatusError as exc:
        delay = 30
        if exc.response.status_code in {403, 404, 410}:
            delay = None
        elif exc.response.status_code == 429:
            seconds = exc.response.headers.get("Retry-After", "")
            if seconds.isdecimal():
                delay = max(1, min(int(seconds), 86400))
        log.warning("teams.delivery_http_failure status=%s retry_seconds=%s", exc.response.status_code, delay)
    except Exception:
        delay = 30
        log.warning("teams.delivery_deferred activity=%s", row["activity_id"])
    finally:
        await store.advance_delivery(row["activity_id"], owner, seq, delay)
    return True


async def run_delivery(app):
    while True:
        try:
            if await deliver_next(app):
                continue
        except Exception:
            log.exception("teams.delivery_worker_unavailable")
        await asyncio.sleep(2)
