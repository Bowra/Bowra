"""E01: POST /chat/send HTTP errors use finite codes and localized messages."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.entitlements import EntitlementDenied
from bowhead_core.error_catalog import (
    default_error_catalog_dir,
    load_error_catalog,
)
from bowhead_core.terminal_protocol import (
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
)
from bowhead_core.terminal_runtime_factory import TerminalRuntimeAdmissionError
from bowhead_gateway._entitlement_gate import _denied_to_http
from bowhead_gateway.chat_send_errors import (
    CHAT_SEND_DYNAMIC_ERROR_CODES,
    chat_submission_receipt_http_error,
)
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import chat as chat_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_gateway import chat_attachment_access, chat_reference_request, chat_reference_start, coach_canvas_target

CHAT_SEND_ROUTE_CODES = frozenset(
    {
        "document_not_found",
        "document_revision_conflict",
        "document_input_unavailable",
        "document_draft_unavailable",
        "document_revision_inputs_unsupported",
        "document_revision_unavailable",
        "coach_canvas_target_unavailable",
        "reference_input_invalid",
        "reference_request_too_large",
        "reference_input_media_type_required",
        "unsupported_entry",
        "coach_mention_unsupported",
        "coach_mention_unavailable",
        "configuration_mention_unsupported",
        "configuration_mention_unavailable",
        "resource_unavailable",
        "plan_execution_claim_lost",
        "attachment_bundle_mismatch",
        "attachment_metadata_stale",
        "attachment_unavailable",
        "attachment_validation_unavailable",
        "result_reference_unavailable",
        "auth_session_generation_inactive",
        "chat_action_not_found",
        "client_refresh_required",
        "chart_selection_unavailable",
        "subagent_draft_unsaved_changes",
        "member_not_in_workspace",
        "datarc_identity_required",
        "datarc_unavailable",
        "datarc_member_not_found",
        "datarc_member_resolve_failed",
        "orchestrator_not_ready",
        "replay_source_not_found",
        "request_validation_failed",
        "message_too_long",
        "empty_message",
        "session_deleted",
        "authz_unavailable",
        "authz_lookup_failed",
        "ws_access_denied",
        "access_context_denied",
        "workspace_archived",
        "workspace_unavailable",
        "planning_unavailable",
        "plan_not_found",
        "plan_not_approvable",
        "user_credit_exhausted",
        "team_pool_exhausted",
        "plan_already_running",
        "steer_parent_not_active",
        "submission_abandoned",
        "submission_contract_inactive",
        "submission_fingerprint_unavailable",
        "submission_id_conflict",
        "submission_kind_unavailable",
        "submission_not_found",
        "submission_pending",
        "submission_rejected",
        "submission_store_unavailable",
        "team_model_unconfigured",
        "too_many_attachments",
        "turn_became_active",
        "turn_dispatch_unavailable",
        "turn_store_unavailable",
        "terminal_protocol_admission_rejected",
        "session_transitioning",
        "session_turn_active",
        "terminal_dependency_recovering",
        "terminal_protocol_future_version",
        "terminal_protocol_malformed",
        "terminal_protocol_writer_not_ready",
    }
)

CHAT_SEND_SHARED_CODES = frozenset(
    {
        # SSE live/replay source projection uses the same client banner even
        # though it is not an HTTP rejection from POST /chat/send.
        "source_projection_unavailable",
        "auth_required",
        "over_grace",
        "suspended",
        "needs_license",
        "tamper",
        "clock_tamper",
        "entitlement_unavailable",
    }
)


class _ReplaceDynamicValues(ast.NodeTransformer):
    def generic_visit(self, node: ast.AST) -> ast.AST:
        if isinstance(node, ast.expr) and not isinstance(
            node,
            (ast.Constant, ast.Dict, ast.List, ast.Set, ast.Tuple),
        ):
            return ast.copy_location(ast.Constant("<dynamic>"), node)
        return super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> ast.Constant:  # noqa: N802
        return ast.copy_location(ast.Constant("<dynamic>"), node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.Constant:  # noqa: N802
        return ast.copy_location(ast.Constant("<dynamic>"), node)

    def visit_Subscript(self, node: ast.Subscript) -> ast.Constant:  # noqa: N802
        return ast.copy_location(ast.Constant("<dynamic>"), node)

    def visit_Call(self, node: ast.Call) -> ast.Constant:  # noqa: N802
        return ast.copy_location(ast.Constant("<dynamic>"), node)


def _chat_http_error_details() -> list[dict[str, Any]]:
    source_path = Path(chat_routes.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    send_scope_end = next(
        node.lineno
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_require_submission_space_access"
    )
    details: list[dict[str, Any]] = []
    # The reference decoder and preparation are part of the same POST boundary.
    # Keep their finite codes in the inventory when the route delegates to them.
    nodes = [node for node in ast.walk(tree) if getattr(node, "lineno", 0) < send_scope_end]
    for module in (chat_reference_request, chat_reference_start, chat_attachment_access, coach_canvas_target):
        nodes.extend(ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8"))))
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name == "ChatAttachmentAccessError":
            # Attachment validation moved out of chat.py. Its typed error is
            # projected unchanged at the same HTTP boundary; inspect both
            # literal branches instead of dropping these still-live codes.
            code = node.args[1]
            alternatives = (code.body, code.orelse) if isinstance(code, ast.IfExp) else (code,)
            for alternative in alternatives:
                assert isinstance(alternative, ast.Constant) and isinstance(alternative.value, str)
                details.append({"error": {"code": alternative.value}})
            continue
        if name != "HTTPException":
            continue
        detail_node = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "detail"),
            node.args[1] if len(node.args) > 1 else None,
        )
        if detail_node is None:
            continue
        literal_node = _ReplaceDynamicValues().visit(copy.deepcopy(detail_node))
        detail = ast.literal_eval(literal_node)
        if isinstance(detail, dict):
            # GET /chat/stream has a separate transport error contract even
            # though it shares this module with POST /chat/send.
            if detail.get("error", {}).get("code") == "source_projection_unavailable":
                continue
            if detail.get("error", {}).get("code") == "<dynamic>":
                continue
            details.append(detail)
    return details


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.state.error_catalog = load_error_catalog(default_error_catalog_dir())
    app.state.user_settings_store = None
    app.state.settings = SimpleNamespace(
        auth_mode="dev",
        dev_default_user="u_test",
    )
    # The two validation branches under test run before the orchestrator is used.
    app.state.orchestrator = SimpleNamespace()
    app.include_router(chat_routes.router, prefix="/api/v1/chat")
    return app


def test_chat_send_route_has_a_closed_code_inventory_without_inline_prose() -> None:
    details = _chat_http_error_details()
    errors = [detail["error"] for detail in details if "error" in detail]
    literal_codes = {error["code"] for error in errors}
    assert "<dynamic>" not in literal_codes
    codes = literal_codes | CHAT_SEND_DYNAMIC_ERROR_CODES

    assert (
        codes
        | chat_routes._TERMINAL_ADMISSION_PUBLIC_CODES
        | {
            "chat_action_not_found",
        }
        == CHAT_SEND_ROUTE_CODES
    )
    for error in errors:
        assert "message" not in error
        assert set(error) <= {"code", "reason", "admission"}
        # The only admission value: preparation refused before any Turn, dispatch or receipt was written.
        assert error.get("admission", "not_started") == "not_started"


def test_chat_send_codes_exist_in_both_runtime_catalogs() -> None:
    catalog = load_error_catalog(default_error_catalog_dir())
    for code in sorted(CHAT_SEND_ROUTE_CODES | CHAT_SEND_SHARED_CODES):
        assert catalog.has(code, "zh-CN"), f"missing zh-CN chat send error: {code}"
        assert catalog.has(code, "en-US"), f"missing en-US chat send error: {code}"
        assert catalog.render(code, "zh-CN") != code
        assert catalog.render(code, "en-US") != code


def test_persisted_rejection_is_projected_through_the_closed_code_map() -> None:
    known = chat_submission_receipt_http_error(
        {
            "http_status": 599,
            "detail": {"error": {"code": "plan_not_found"}},
        },
        fallback_code="submission_rejected",
    )
    assert known.status_code == 404
    assert known.detail == {"error": {"code": "plan_not_found"}}

    unknown = chat_submission_receipt_http_error(
        {
            "http_status": 418,
            "detail": {"error": {"code": "private_database_value"}},
        },
        fallback_code="submission_rejected",
    )
    assert unknown.status_code == 409
    assert unknown.detail == {"error": {"code": "submission_rejected"}}


def test_frontend_chat_send_mapping_covers_the_backend_inventory() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source = (repo_root / "packages" / "bowhead_frontend" / "lib" / "chat-send-errors.ts").read_text(encoding="utf-8")
    match = re.search(
        r"CHAT_SEND_BACKEND_CODES\s*=\s*\[(.*?)\]\s*as const",
        source,
        re.DOTALL,
    )

    assert match is not None
    frontend_codes = set(re.findall(r'"([a-z0-9_]+)"', match.group(1)))
    assert frontend_codes == CHAT_SEND_ROUTE_CODES | CHAT_SEND_SHARED_CODES


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_selected_resource_rejection_uses_the_safe_runtime_message(language: str) -> None:
    app = _app()

    @app.get("/selected-resource-rejection")
    async def reject():
        raise chat_submission_receipt_http_error(
            {"detail": {"error": {"code": "coach_canvas_target_unavailable"}}}, fallback_code="submission_rejected"
        )

    response = TestClient(app, cookies={"NEXT_LOCALE": language}).get("/selected-resource-rejection")
    assert response.status_code == 409
    error = response.json()["detail"]["error"]
    assert error["code"] == "coach_canvas_target_unavailable"
    assert error["message"] == app.state.error_catalog.render(error["code"], language)
    assert error["code"] not in error["message"]


def test_terminal_admission_dynamic_projection_is_finite_and_in_inventory() -> None:
    assert {
        "session_turn_active",
        "session_transitioning",
        "terminal_dependency_recovering",
        "terminal_protocol_admission_rejected",
        "terminal_protocol_future_version",
        "terminal_protocol_malformed",
        "terminal_protocol_writer_not_ready",
    } == chat_routes._TERMINAL_ADMISSION_PUBLIC_CODES
    assert chat_routes._TERMINAL_ADMISSION_PUBLIC_CODES <= CHAT_SEND_ROUTE_CODES


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (
            TerminalProtocolMalformedError("bad row"),
            500,
            "terminal_protocol_malformed",
        ),
        (
            TerminalProtocolFutureVersionError("v2"),
            409,
            "terminal_protocol_future_version",
        ),
        (
            TerminalRuntimeAdmissionError("session_turn_active"),
            409,
            "session_turn_active",
        ),
        (
            TerminalRuntimeAdmissionError("session_recovery_locked"),
            409,
            "terminal_dependency_recovering",
        ),
        (
            TerminalRuntimeAdmissionError("terminal_steer_target_changed"),
            409,
            "session_turn_active",
        ),
    ],
)
def test_terminal_admission_failures_have_finite_public_contract(
    failure: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    error = chat_routes._terminal_admission_http_error(failure)
    assert error.status_code == expected_status
    assert error.detail == {"error": {"code": expected_code}}


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("zh-CN", "请输入消息或添加附件"),
        ("en-US", "Enter a message or add an attachment"),
    ],
)
def test_empty_message_response_is_localized(
    language: str,
    expected: str,
) -> None:
    client = TestClient(_app(), cookies={"NEXT_LOCALE": language})
    response = client.post(
        "/api/v1/chat/send",
        json={"workspace_id": "ws_test", "message": " "},
    )

    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "empty_message"
    assert expected in error["message"]
    assert error["message"] != error["code"]


def test_unsaved_bound_draft_rejects_chat_before_a_turn_starts() -> None:
    client = TestClient(_app(), cookies={"NEXT_LOCALE": "zh-CN"})
    response = client.post(
        "/api/v1/chat/send",
        json={
            "workspace_id": "ws_test",
            "message": "把用途写得更具体",
            "client_info": {
                "agent_draft_id": "sad_owned",
                "agent_draft_version": "3",
                "agent_draft_dirty": "1",
            },
        },
    )

    assert response.status_code == 409
    error = response.json()["detail"]["error"]
    assert error["code"] == "subagent_draft_unsaved_changes"
    assert error["admission"] == "not_started"
    assert "右侧有未保存修改" in error["message"]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("zh-CN", "10,000 个字符"),
        ("en-US", "10,000 characters"),
    ],
)
def test_message_length_response_uses_a_stable_localized_code(
    language: str,
    expected: str,
) -> None:
    client = TestClient(_app(), cookies={"NEXT_LOCALE": language})
    response = client.post(
        "/api/v1/chat/send",
        json={"workspace_id": "ws_test", "message": "x" * 10_001},
    )

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["code"] == "message_too_long"
    assert expected in error["message"]
    assert not isinstance(response.json()["detail"], list)


def test_entitlement_adapter_does_not_bypass_the_runtime_catalog() -> None:
    http_error = _denied_to_http(
        EntitlementDenied("over_grace", 402, "raw internal fallback"),
    )

    assert http_error.detail == {"error": {"code": "over_grace"}}


def test_rejection_chain_keeps_reason_codes_and_drops_everything_else():
    from bowhead_core.chat_reference_preparation import ReferenceStartPreparationError
    from bowhead_gateway.chat_reference_start import _rejection_chain

    try:
        try:
            try:
                raise KeyError("用户原文：2026 年各大类营业额")
            except KeyError:
                raise ValueError("exact_skill_grants_unavailable") from None
        except ValueError:
            raise ReferenceStartPreparationError("reference_start_preparation_unavailable") from None
    except ReferenceStartPreparationError as exc:
        chain = _rejection_chain(exc)
    assert chain == "ReferenceStartPreparationError:reference_start_preparation_unavailable <- ValueError:exact_skill_grants_unavailable <- KeyError"
