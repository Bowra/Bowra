"""Translate personal-chat cards into the canonical same-Turn clarification contract."""

from __future__ import annotations

import os
from typing import Any

from bowhead_core.clarification_store import resume_context_is_readable
from bowhead_core.space_governance import SpaceOperation
from fastapi import HTTPException, Request, Response

from bowhead_gateway._entitlement_gate import gate_turn_http
from bowhead_gateway.access_context import build_access_context_for_user_from_request
from bowhead_gateway.im.error_messages import im_interface_message
from bowhead_gateway.routes.clarifications import ClarificationAnswerDTO, ResolveRequest, _to_dto, resolve_for_user
from bowhead_gateway.turn_service import clarification_requires_legacy_resume_fence


def _enabled() -> bool:
    return os.getenv("FEATURE_TEAMS_CHAT_CLARIFICATION", "").strip().lower() in {"1", "true", "on", "yes"}


def _questions(card):
    if (
        card.kind not in {"ambiguous_turn", "options"}
        or not clarification_requires_legacy_resume_fence(card)
        or not resume_context_is_readable(card.resume_context)
    ):
        return []
    try:
        questions = _to_dto(card).questions
    except ValueError:
        return []
    if not questions and card.candidates and card.rationale:
        return [
            {
                "question": card.rationale,
                "type": "multi_select" if card.multi_select else "single_select",
                "options": [candidate.value for candidate in card.candidates],
                "option_labels": [candidate.label for candidate in card.candidates],
                "legacy_choices": True,
            }
        ]
    for question in questions:
        if question.get("type") not in {"free_text", "single_select", "multi_select"} or not question.get("question"):
            return []
        options = question.get("options")
        labels = question.get("option_labels", options)
        if not isinstance(options, list) or not isinstance(labels, list) or len(labels) != len(options):
            return []
    return questions


async def question_card(app, request_id, turn_id, locale):
    if not _enabled() or not request_id:
        return None
    card = await app.state.clarification_store.get(request_id)
    if card is None or card.turn_id != turn_id or card.status != "pending" or not (questions := _questions(card)):
        return None
    body = []
    for index, question in enumerate(questions):
        field = {"id": f"q{index}", "label": question["question"], "isRequired": True}
        if question["type"] == "free_text":
            field.update(type="Input.Text", maxLength=256, isMultiline=True)
        else:
            field.update(
                type="Input.ChoiceSet",
                style="expanded",
                isMultiSelect=question["type"] == "multi_select",
                choices=[{"title": label, "value": str(i)} for i, label in enumerate(question.get("option_labels", question["options"]))],
            )
        body.append(field)
    return {
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": [
            {
                "type": "Action.Execute",
                "title": im_interface_message("teams_submit_answers", locale),
                "verb": "bowhead.clarification.answer",
                "data": {"request_id": request_id},
            }
        ],
    }


def _answers(questions, data):
    answers = []
    for index, question in enumerate(questions):
        value = data.get(f"q{index}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Missing answer")
        if question["type"] == "free_text":
            answers.append(ClarificationAnswerDTO(question=question["question"], source="custom", answer=value.strip()))
            continue
        items = value.split(",")
        choices = question["options"]
        if len(items) != len(set(items)) or (len(items) > 1 and question["type"] != "multi_select"):
            raise ValueError("Invalid selection")
        for item in items:
            if item not in {str(i) for i in range(len(choices))}:
                raise ValueError("Unknown option")
            option = int(item)
            answers.append(ClarificationAnswerDTO(question=question["question"], source="candidate", option_index=option, answer=choices[option]))
    if questions[0].get("legacy_choices"):
        return ResolveRequest(chosen_values=[answer.answer for answer in answers])
    return ResolveRequest(answers=answers)


async def submit_answer(request: Request, activity, connection, tenant, oid, data: dict[str, Any]):
    from bowhead_gateway.routes.teams import personal_session_id

    if not _enabled():
        raise HTTPException(409, {"error": {"code": "teams_clarify_in_web"}})
    state = request.app.state
    binding = await state.teams_store.get_identity(tenant, oid, connection.team_id)
    request_id = data.get("request_id")
    card = await state.clarification_store.get(request_id) if isinstance(request_id, str) else None
    if card is None or binding != (card.user_id, connection.team_id):
        raise HTTPException(404, {"error": {"code": "clarification_not_found"}})
    conversation = activity["conversation"]["id"]
    selected = await state.teams_store.get_workspace(tenant, conversation, oid, connection.team_id)
    if selected is None:
        spaces = await state.user_store.list_workspaces_for_user(card.user_id, team_id=connection.team_id)
        selected = spaces[0]["workspace_id"] if len(spaces) == 1 else None
    if selected != card.workspace_id:
        raise HTTPException(409, {"error": {"code": "teams_select_original_space"}})
    await build_access_context_for_user_from_request(
        request, user_id=card.user_id, workspace_id=selected, expected_team_id=connection.team_id, operation=SpaceOperation.EXECUTE
    )
    session = await personal_session_id(request.app, connection, conversation, card.user_id, selected)
    turn = await state.orchestrator.session_store.get_turn(card.turn_id)
    snapshot = card.resume_context
    if not isinstance(snapshot, dict):
        raise HTTPException(409, {"error": {"code": "teams_clarify_in_web"}})
    if (
        turn is None
        or turn.session_id != session
        or (card.session_id or snapshot.get("session_id")) != session
        or snapshot.get("session_id", session) != session
        or snapshot.get("workspace_id", selected) != selected
        or snapshot.get("team_id", connection.team_id) != connection.team_id
        or getattr(turn, "terminal_protocol_version", None) is not None
    ):
        raise HTTPException(404, {"error": {"code": "clarification_not_found"}})
    if not (questions := _questions(card)):
        raise HTTPException(409, {"error": {"code": "teams_clarify_in_web"}})
    try:
        answers = _answers(questions, data)
    except ValueError as exc:
        raise HTTPException(422, {"error": {"code": "teams_invalid_answers"}}) from exc
    response = Response()
    await gate_turn_http(request, connection.team_id, response)
    request.state.im_clarification = True
    await resolve_for_user(request_id, answers, request, response, state.clarification_store, user_id=card.user_id, team_id=connection.team_id)
    from bowhead_gateway.im.copy_locale import resolve_channel_copy_locale

    locale = await resolve_channel_copy_locale(
        request.app, user_id=card.user_id, team_id=connection.team_id, workspace_id=selected, client_language=activity.get("locale")
    )
    return {
        "statusCode": 200,
        "type": "application/vnd.microsoft.card.adaptive",
        "value": {
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [{"type": "TextBlock", "wrap": True, "text": im_interface_message("choice_submitted", locale)}],
        },
    }


async def handle_answer_action(request, activity, connection, tenant, oid, data):
    from bowhead_gateway.im.error_messages import im_error_message

    try:
        return await submit_answer(request, activity, connection, tenant, oid, data)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = (detail.get("error") or detail).get("code", "teams_clarify_in_web")
        locale = activity.get("locale")
        if code in {"teams_select_original_space", "teams_invalid_answers", "teams_clarify_in_web"}:
            message = im_interface_message(code, locale)
        elif exc.status_code == 404:
            message = im_interface_message("clarification_missing", locale)
        else:
            message = im_error_message(code, locale)
        return {"statusCode": exc.status_code, "type": "application/vnd.microsoft.error", "value": {"code": code, "message": message}}
