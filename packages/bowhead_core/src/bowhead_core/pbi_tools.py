"""Power BI Service Principal tool handlers.

The agent uses the current team's administrator-managed semantic model and DAX templates.
The handler sends read-only DAX through the configured Service Principal; no end-user credential
or Power BI token is exposed to the agent or browser.

Registered per connection by ``builtin_powerbi_tool``. ``ctx.pbi`` is read via
``getattr`` so tests and narrow callers can use minimal contexts.
"""

from __future__ import annotations

import re
from typing import Any

from bowhead_core.published_diagnostics import (
    POWERBI_QUERY_FAILURE_CODES,
    publish_powerbi_query_failure,
)


def _handle(ctx: Any) -> Any | None:
    """The usable or finite-unavailable per-turn PBI handle, or None when unbound."""
    return getattr(ctx, "pbi", None)


def _failure(code: str, *, truncated: bool = False) -> dict[str, Any]:
    safe_code = code if code in POWERBI_QUERY_FAILURE_CODES else "pbi_query_failed"
    payload: dict[str, Any] = {
        "ok": False,
        "error_kind": "pbi",
        "error_code": safe_code,
        # Compatibility with generic result-failure detectors. This is always
        # a finite code, never provider prose.
        "error": safe_code,
    }
    if truncated:
        payload["truncated"] = True
    published = publish_powerbi_query_failure(safe_code)
    if published is not None:
        payload["diagnostics"] = published.to_wire()
    return payload


def _unavailable_code(handle: Any | None) -> str | None:
    if handle is None:
        return "pbi_connection_unavailable"
    code = getattr(handle, "error_code", None)
    if isinstance(code, str) and code in POWERBI_QUERY_FAILURE_CODES:
        return code
    return None


async def _pbi_execute_dax(args: dict[str, Any], ctx: Any) -> Any:
    h = _handle(ctx)
    unavailable = _unavailable_code(h)
    if unavailable is not None:
        return _failure(unavailable)
    dax = (args or {}).get("dax")
    if not isinstance(dax, str) or not dax.strip():
        return _failure("pbi_query_input_required")
    res = await h.client.execute_dax(dax)
    if res.error_code:
        # Surface truncation / engine errors instead of computing on partial data.
        return _failure(res.error_code, truncated=res.truncated)
    return {
        "columns": res.columns,
        "rows": res.rows,
        "row_count": res.row_count,
        "truncated": res.truncated,
        "empty": res.row_count == 0,
    }


async def _pbi_fetch_rows(args: dict[str, Any], ctx: Any) -> Any:
    # Same execution path; separate tool name so the agent's intent (pull rows for local
    # attribution/compute) is explicit and the result can be routed to an artifact later.
    return await _pbi_execute_dax(args, ctx)


async def _pbi_list_measures(args: dict[str, Any], ctx: Any) -> Any:
    h = _handle(ctx)
    unavailable = _unavailable_code(h)
    if unavailable is not None:
        return _failure(unavailable)
    # executeQueries can't do INFO/DMV → catalog is seeded manually at onboarding or via the admin
    # Scanner API (TODO: wire the catalog source). Until then, surface what's on the handle.
    catalog = getattr(h, "catalog", None) or {}
    measures = catalog.get("measures")
    if measures is None:
        return _failure("pbi_catalog_unavailable")
    return {"measures": measures}


async def _pbi_list_templates(args: dict[str, Any], ctx: Any) -> Any:
    """Expose admin-managed DAX templates for the agent to parameterize per request."""
    h = _handle(ctx)
    unavailable = _unavailable_code(h)
    if unavailable is not None:
        return _failure(unavailable)
    templates = (getattr(h, "catalog", None) or {}).get("templates")
    if not isinstance(templates, list):
        return _failure("pbi_catalog_unavailable")
    return {"templates": templates}


_TEMPLATE_PARAMETER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def _dax_literal(value: Any) -> str | None:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace('"', '""') + '"'
    return None


async def _pbi_execute_template(args: dict[str, Any], ctx: Any) -> Any:
    """Fill named ``{{parameter}}`` DAX template placeholders with scalar literals."""
    h = _handle(ctx)
    unavailable = _unavailable_code(h)
    if unavailable is not None:
        return _failure(unavailable)
    template_id = (args or {}).get("template_id")
    parameters = (args or {}).get("parameters")
    templates = (getattr(h, "catalog", None) or {}).get("templates")
    if not isinstance(template_id, str) or not isinstance(parameters, dict) or not isinstance(templates, list):
        return _failure("pbi_query_input_required")
    template = next((item for item in templates if isinstance(item, dict) and item.get("id") == template_id), None)
    dax = template.get("dax_template") if isinstance(template, dict) else None
    if not isinstance(dax, str):
        return _failure("pbi_query_input_required")
    names = set(_TEMPLATE_PARAMETER.findall(dax))
    if set(parameters) != names:
        return _failure("pbi_query_input_required")
    values = {name: _dax_literal(parameters[name]) for name in names}
    if any(value is None for value in values.values()):
        return _failure("pbi_query_input_required")
    query = _TEMPLATE_PARAMETER.sub(lambda match: values[match.group(1)] or "", dax)
    return await _pbi_execute_dax({"dax": query}, ctx)
