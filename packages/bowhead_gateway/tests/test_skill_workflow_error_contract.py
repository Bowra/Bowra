"""Static guards for the finite Skills/Workflow browser error boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from bowhead_core.published_diagnostics import SKILL_WORKFLOW_FAILURE_CODES

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_UPLOAD = _REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "skill_upload.py"
_GATEWAY_SRC = _REPO_ROOT / "packages" / "bowhead_gateway" / "src" / "bowhead_gateway"
_ROUTES = tuple(_GATEWAY_SRC / "routes" / name for name in ("skills.py", "team_skills.py", "workflow_solidify.py")) + (
    # Shared skill-bundle read surface: coded errors raised here (both routes only call it);
    # static guard must cover it, otherwise moving codes into a shared module bypasses this gate.
    _GATEWAY_SRC / "skill_bundle_files.py",
)


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _literal_code(call: ast.Call, *, position: int) -> str | None:
    if len(call.args) > position:
        value = call.args[position]
        return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None
    for keyword in call.keywords:
        if keyword.arg == "code":
            value = keyword.value
            return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None
    return None


def test_skill_upload_errors_always_have_registered_codes() -> None:
    calls = [node for node in ast.walk(_tree(_SKILL_UPLOAD)) if isinstance(node, ast.Call) and _call_name(node) == "SkillUploadError"]
    assert calls
    for call in calls:
        code = _literal_code(call, position=0)
        assert code is not None, f"{_SKILL_UPLOAD}:{call.lineno} SkillUploadError must use a literal code= and keep prose server-side"
        assert code in SKILL_WORKFLOW_FAILURE_CODES


def test_routes_do_not_raise_unprojected_http_exceptions() -> None:
    for path in _ROUTES:
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            assert _call_name(node.exc) != "HTTPException", f"{path}:{node.lineno} must use the finite Skills/Workflow error projector"


def test_literal_route_error_codes_are_registered() -> None:
    checked = 0
    for path in _ROUTES:
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in {"skill_workflow_http_error", "_coded_http"}:
                continue
            code = _literal_code(
                node,
                position=1 if name == "skill_workflow_http_error" else 2,
            )
            if code is None:
                continue
            checked += 1
            assert code in SKILL_WORKFLOW_FAILURE_CODES, f"{path}:{node.lineno} uses unpublished error code {code}"
    assert checked >= 40
