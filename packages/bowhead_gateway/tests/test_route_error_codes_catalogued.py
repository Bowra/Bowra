"""Every literal error code raised by the scanned route modules has catalog copy.

Codes missing from config/i18n/errors reach the browser without a message (the
116 rename rejection showed only a raw code). The scan is scoped to the listed
route modules; other modules still carry uncatalogued protocol codes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import bowhead_core.team_plugin_store as team_plugin_store
import bowhead_gateway.routes.canvas as canvas_routes
import bowhead_gateway.routes.sessions as sessions_routes
import bowhead_gateway.routes.team_builtin_tools as team_builtin_tool_routes
import bowhead_gateway.routes.team_plugins as team_plugin_routes
import bowhead_gateway.routes.team_tool_sources as team_tool_source_routes
import pytest
from bowhead_core.error_catalog import SUPPORTED_LANGUAGES, default_error_catalog_dir, load_error_catalog


def _literal_codes(module_path: Path) -> set[str]:
    """Codes passed as `_coded_error(status, "code")` or `{"error": {"code": "code"}}`."""
    codes: set[str] = set()
    for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "_coded_error" and len(node.args) >= 2:
                argument = node.args[1]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    codes.add(argument.value)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if not (isinstance(key, ast.Constant) and key.value == "error" and isinstance(value, ast.Dict)):
                    continue
                for inner_key, inner_value in zip(value.keys, value.values, strict=True):
                    if isinstance(inner_key, ast.Constant) and inner_key.value == "code" and isinstance(inner_value, ast.Constant):
                        codes.add(str(inner_value.value))
    return codes


@pytest.mark.parametrize(
    "module",
    [sessions_routes, canvas_routes, team_plugin_routes, team_builtin_tool_routes, team_tool_source_routes],
    ids=["sessions", "canvas", "team_plugins", "team_builtin_tools", "team_tool_sources"],
)
def test_route_error_codes_have_catalog_copy_in_every_language(module) -> None:
    codes = _literal_codes(Path(module.__file__))
    assert codes, "scanner found no codes; the guard would pass vacuously"
    catalog = load_error_catalog(default_error_catalog_dir())
    missing = {language: sorted(code for code in codes if not catalog.has(code, language)) for language in SUPPORTED_LANGUAGES}
    assert all(not values for values in missing.values()), missing


def test_plugin_store_error_codes_have_catalog_copy_in_every_language() -> None:
    """TeamPluginError codes reach plugin management and Agent publication as coded errors."""
    tree = ast.parse(Path(team_plugin_store.__file__).read_text(encoding="utf-8"))
    codes = {
        str(node.args[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "TeamPluginError" and node.args and isinstance(node.args[0], ast.Constant)
    }
    # Raised only for a caller that forgot its transaction; never reaches a user.
    codes.discard("capability_transaction_required")
    assert {"capability_skill_conflict", "capability_unavailable", "capability_empty"} <= codes
    catalog = load_error_catalog(default_error_catalog_dir())
    missing = {language: sorted(code for code in codes if not catalog.has(code, language)) for language in SUPPORTED_LANGUAGES}
    assert all(not values for values in missing.values()), missing


def test_scanner_sees_the_rename_and_cursor_codes() -> None:
    codes = _literal_codes(Path(sessions_routes.__file__)) | _literal_codes(Path(canvas_routes.__file__))
    assert {"session_title_empty", "session_title_too_long", "session_title_invalid", "invalid_session_cursor", "invalid_report_cursor"} <= codes
