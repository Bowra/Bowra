"""Static and helper guards for Clarifications error publication."""

from __future__ import annotations

import ast
from pathlib import Path

from bowhead_core.published_diagnostics import (
    RULE_CLARIFICATION_FAILURE_CODES,
)
from bowhead_gateway.rule_clarification_errors import (
    normalize_rule_clarification_access_code,
    rule_clarification_http_error,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROUTES = (_REPO_ROOT / "packages" / "bowhead_gateway" / "src" / "bowhead_gateway" / "routes" / "clarifications.py",)


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_routes_do_not_construct_raw_http_exceptions() -> None:
    for path in _ROUTES:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "fastapi":
                assert all(alias.name != "HTTPException" for alias in node.names)
            if isinstance(node, ast.Call):
                assert _call_name(node) != "HTTPException"


def test_literal_route_codes_are_registered() -> None:
    checked = 0
    names_and_positions = {"rule_clarification_http_error": 1}
    for path in _ROUTES:
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in names_and_positions:
                continue
            position = names_and_positions[name]
            if len(node.args) <= position:
                continue
            candidate = node.args[position]
            if not isinstance(candidate, ast.Constant) or not isinstance(
                candidate.value,
                str,
            ):
                continue
            checked += 1
            assert candidate.value in RULE_CLARIFICATION_FAILURE_CODES
    assert checked >= 25


def test_http_and_semantic_helpers_publish_finite_diagnostics() -> None:
    exc = rule_clarification_http_error(
        422,
        "clarification_input_rejected",
    )
    assert exc.detail["code"] == "clarification_input_rejected"
    assert exc.detail["diagnostics"]["diagnostic_code"] == ("rule_clarification_failure")


def test_unknown_or_dynamic_access_reasons_fail_closed() -> None:
    exc = rule_clarification_http_error(
        500,
        "Traceback: password=raw-secret",
    )
    assert exc.detail["code"] == "clarification_operation_failed"
    assert "raw-secret" not in str(exc.detail)
    assert (
        normalize_rule_clarification_access_code(
            "ws_access_denied:not_a_member",
        )
        == "ws_access_denied"
    )
    assert (
        normalize_rule_clarification_access_code(
            "Access denied for /srv/private; password=raw-secret",
        )
        == "clarification_access_denied"
    )


def test_resume_preflight_http_diagnostics_preserve_the_original_code() -> None:
    exc = rule_clarification_http_error(
        409, "clarification_resume_unavailable", nested_error=True, clarification_stage="clarification_resume_preflight"
    )
    assert exc.detail["error"]["code"] == "clarification_resume_unavailable"
    assert exc.detail["error"]["diagnostics"]["safe_detail"] == "phase=clarification_resume_preflight; failure_class=state"
    assert exc.detail["error"]["diagnostics"]["retryable"] is False
