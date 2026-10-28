from __future__ import annotations

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway" / "routes"


def _functions(path: Path) -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node for node in ast.walk(tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))]


def _called_names(function: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_canvas_section_egress_cannot_bypass_viewer_safe_projector() -> None:
    exempt_canonical_builders = {"_canvas_detail", "_viewer_safe_projector"}
    failures: list[str] = []
    for function in _functions(ROUTES / "canvas.py"):
        calls = _called_names(function)
        if function.name in exempt_canonical_builders:
            continue
        if "to_report_section" in calls and not ({"project", "_project_canvas_detail"} & calls):
            failures.append(f"{function.name}: to_report_section without source projection")
        if "CanvasDetailDTO" in calls and "_project_canvas_detail" not in calls:
            failures.append(f"{function.name}: CanvasDetailDTO without source projection")
        if "CanvasRevisionDTO" in calls and "_project_revision_payload" not in calls:
            failures.append(f"{function.name}: revision payload without source projection")
    assert failures == []


def test_turn_event_user_egress_cannot_bypass_viewer_safe_event_projector() -> None:
    turns = {function.name: _called_names(function) for function in _functions(ROUTES / "turns.py")}
    event_dto_routes = {name: calls for name, calls in turns.items() if "TurnEventDTO" in calls}
    assert event_dto_routes
    assert all("project_event_payloads_for_viewer" in calls for calls in event_dto_routes.values())

    replay_routes = {name: calls for name, calls in turns.items() if "EventSourceResponse" in calls and "list_events" in calls}
    assert replay_routes
    assert all("project_event_payloads_for_viewer" in calls for calls in replay_routes.values())

    chat = {function.name: _called_names(function) for function in _functions(ROUTES / "chat.py")}
    stream_calls = chat["stream"]
    assert "project_event_payloads_for_viewer" in stream_calls
    assert "project_live_event_payload_for_viewer" in stream_calls
