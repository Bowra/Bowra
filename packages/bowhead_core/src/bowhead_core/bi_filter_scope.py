"""Make the model record its scope judgment for filter values Datarc recognized but did not apply.

Regression check on 1.7.38 (2026-11-15), 「女装 2026年各月 月坪效」: the bi_query hint named 「女装」 as a parsed filter
value that the engine's structured report listed under no `filters` or `having` condition. deepseek-flash and
deepseek-v4-pro both labelled the all-store rows as 女装 and never mentioned the hint; compute spent about 80 output
tokens between the result and its first sandbox call. Two rounds of hint and prompt wording changed nothing,
because the model never engaged with the hint.

Whether a value belongs to the requested scope stays the model's judgment, and the rows are never withheld. This gate
only makes that judgment explicit before delivery: `subagent_return` refuses a payload that records none for a named
value. It checks the entry's shape and one fact the run can prove (a claimed requery needs a query whose report lists
a condition). After `MAX_REFUSALS` refusals it accepts, so a model that will not comply cannot stall the Turn.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from bowhead_core.bi_unknown_words import unapplied_filter_values

logger = logging.getLogger(__name__)

FIELD = "filter_scope_checks"
REFUSAL_MARKER = "filter_scope_values"
MAX_REFUSALS = 2
_IN_SCOPE_HANDLING = ("requeried", "stated_limit")
_INCIDENTAL = "incidental"


def _query_results(inv: Any) -> list[dict]:
    """Return the successful single-query results of one bi_query invocation (flat or batched)."""
    if getattr(inv, "tool_name", "") != "bi_query" or getattr(inv, "error", None):
        return []
    result = getattr(inv, "result", None)
    if not isinstance(result, dict):
        return []
    batch = result.get("results")
    items = batch if isinstance(batch, list) else [result]
    return [item for item in items if isinstance(item, dict) and not item.get("error")]


def unapplied_values_in_run(invocations: Sequence[Any]) -> list[str]:
    """Return every filter value this run's queries parsed but whose reports listed no condition."""
    out: list[str] = []
    for inv in invocations or []:
        for item in _query_results(inv):
            for value in unapplied_filter_values(item.get("_parse"), item.get("query_explain")):
                if value not in out:
                    out.append(value)
    return out


def _run_reports_a_condition(invocations: Sequence[Any]) -> bool:
    for inv in invocations or []:
        for item in _query_results(inv):
            report = item.get("query_explain")
            if isinstance(report, dict) and ("filters" in report or "having" in report):
                return True
    return False


def scope_check_problems(payload: dict, values: Sequence[str], invocations: Sequence[Any]) -> list[str]:
    """Describe each named value whose `filter_scope_checks` entry is missing or inconsistent."""
    entries: dict[str, dict] = {}
    raw = payload.get(FIELD)
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("value"), str):
            entries.setdefault(entry["value"].strip(), entry)
    problems: list[str] = []
    for value in values:
        entry = entries.get(value)
        if entry is None:
            problems.append(f"「{value}」 has no entry.")
            continue
        in_scope, handling = entry.get("in_scope"), entry.get("handling")
        if not isinstance(in_scope, bool):
            problems.append(f"「{value}」: in_scope must be true or false.")
        elif in_scope and handling not in _IN_SCOPE_HANDLING:
            problems.append(f'「{value}」: in_scope=true needs handling "requeried" or "stated_limit".')
        elif not in_scope and handling != _INCIDENTAL:
            problems.append(f'「{value}」: in_scope=false needs handling "incidental".')
        elif handling == "requeried" and not _run_reports_a_condition(invocations):
            problems.append(f'「{value}」: handling is "requeried", but no query in this run reports a `filters` or `having` condition.')
    return problems


def _prior_refusals(invocations: Sequence[Any]) -> int:
    return sum(
        1
        for inv in invocations or []
        if getattr(inv, "tool_name", "") == "subagent_return" and isinstance(getattr(inv, "result", None), dict) and REFUSAL_MARKER in inv.result
    )


def _refusal(values: Sequence[str], problems: Sequence[str]) -> dict[str, Any]:
    quoted = ", ".join(f"「{value}」" for value in values)
    return {
        "ok": False,
        # A payload-contract retry, not a Tool fault: same non-failure class as the output_schema and stale-KPI gates.
        "_schema_retry": True,
        "error_kind": "invalid_args",
        "error": (
            f"Record your scope judgment before returning. This run's query texts name these filter values, "
            f"but the executed queries (`query_explain`) applied no `filters` or `having` condition, so those rows are not restricted to them: {quoted}. "
            f'Add `{FIELD}` to the payload with one entry per value: {{"value": "...", "in_scope": true|false, '
            '"handling": "requeried"|"stated_limit"|"incidental"}. '
            "in_scope=true means the user asked for data restricted to that value. Then either requery so the rows you deliver come "
            'from a query whose report lists the condition ("requeried"), or say in the answer that the data cannot be restricted '
            'that way ("stated_limit"). Either way, no title, chart or prose may label unrestricted rows with the value; correct any that do. '
            'in_scope=false ("incidental") means the value is incidental wording and the rows answer the request as returned.\n'
            + "\n".join(f"  - {problem}" for problem in problems)
        ),
        REFUSAL_MARKER: list(values),
    }


def review_scope_checks(payload: dict, invocations: Sequence[Any]) -> dict[str, Any] | None:
    """Return the refusal for this return attempt, or None to accept the payload."""
    if payload.get("error"):
        return None  # A bail or clarification question delivers no rows.
    values = unapplied_values_in_run(invocations)
    if not values:
        return None
    problems = scope_check_problems(payload, values, invocations)
    if not problems:
        return None
    from bowhead_core.metrics import inc

    if _prior_refusals(invocations) >= MAX_REFUSALS:
        inc("bowhead_bi_filter_scope_unjudged_total", help="subagent_return accepted without a scope judgment after the refusal budget")
        logger.warning("bi_filter_scope.accepted_unjudged values=%s problems=%s", values, problems)
        return None
    inc("bowhead_bi_filter_scope_refused_total", help="subagent_return refused for a missing filter scope judgment")
    return _refusal(values, problems)
