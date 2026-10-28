"""M3-10 deterministic scorers for durable Agent Eval attempts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.agent_eval_effects import (
    DATARC_LIVE_ROUTE,
    ENVIRONMENT_DENIALS,
    LIVE_READ_ONLY_ROUTE,
    NOT_RUN_IN_EVAL_DENIAL,
    counts_against_tool_budget,
    eval_tool_identity,
)
from bowhead_core.agent_eval_points import MAX_POINTS_PER_CASE, ExpectedPoint, confirmed_required_tool_groups, validate_points
from bowhead_core.evalkit.cases import EvalCase
from bowhead_core.evalkit.scorers import NoFabricationScorer, Scorer, ScoreResult
from bowhead_core.turn_failure_text import INSUFFICIENT_DATA_PREFIX

MANDATORY_SCORERS: tuple[str, ...] = (
    "terminal_contract",
    "output_schema",
    "tool_contract",
    "permission_denial",
    "effect_policy",
    "citation",
    "no_fabrication",
    "budget_timeout",
)

DEFAULT_CRITICAL_SCORERS = frozenset(
    {
        "effect_policy",
        "permission_denial",
        "terminal_contract",
    }
)

# Evidence only: never critical and never counted in `passed`, whatever a stored case configures.
# Figures derived from source data (sums, shares, rounding, "roughly 1 to 14") have no literal
# source, so an unmatched figure is a hint for the Owner, not proof the answer is wrong.
ADVISORY_SCORERS = frozenset({"no_fabrication"})


class _ExpectedContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    allowed_terminal_statuses: list[str] = Field(default_factory=list, max_length=16)
    output_schema: dict[str, Any] | None = None
    schema_ref: str | None = Field(default=None, min_length=1, max_length=200)
    required_denials: list[str] = Field(default_factory=list, max_length=200)
    forbidden_denials: list[str] = Field(default_factory=list, max_length=200)
    required_citations: int = Field(default=0, ge=0, le=1000)
    # The only place plain-language expectations are allowed (technical design §5.1).
    points: list[ExpectedPoint] = Field(default_factory=list, max_length=MAX_POINTS_PER_CASE)
    # Held out from Coach (alignment loop A6). Part of the case hash, so flipping it is a suite revision.
    held_out: bool = False


class _ScorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    required_tools: list[str] = Field(default_factory=list, max_length=200)
    forbidden_tools: list[str] = Field(default_factory=list, max_length=200)
    critical: list[str] = Field(default_factory=list, max_length=len(MANDATORY_SCORERS))
    max_tool_calls: int = Field(default=200, ge=0, le=200)
    max_duration_ms: int = Field(default=120_000, ge=1000, le=3_600_000)
    max_cost_microusd: int = Field(default=5_000_000, ge=0, le=1_000_000_000)


def validate_core_scoring_contract(expected: Mapping[str, Any], config: Mapping[str, Any], *, allow_schema_ref: bool = True) -> None:
    """Reject settings that the fixed core bundle would silently ignore."""
    contract = _ExpectedContract.model_validate(expected)
    settings = _ScorerConfig.model_validate(config)
    if set(settings.critical) - set(MANDATORY_SCORERS):
        raise ValueError("Unsupported critical scorer")
    validate_points(contract.points)
    if contract.schema_ref is not None and not allow_schema_ref:
        raise ValueError("Product cases require an inline output schema")
    for values in (
        contract.allowed_terminal_statuses,
        contract.required_denials,
        contract.forbidden_denials,
        settings.required_tools,
        settings.forbidden_tools,
    ):
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("Scoring references must be nonempty bounded strings")
    if contract.output_schema is not None:
        try:
            Draft202012Validator.check_schema(contract.output_schema)
        except SchemaError as exc:
            raise ValueError("Invalid output schema") from exc


def core_scoring_contract() -> dict[str, Any]:
    """Describe the actual Owner product scoring surface for model discovery."""
    expected = _ExpectedContract.model_json_schema()
    expected["properties"].pop("schema_ref")
    # The platform assigns held-out cases and only the Owner changes them; Coach never proposes one.
    expected["properties"].pop("held_out")
    return {
        "scorer_bundle_ref": "evalkit.core@1",
        "expected_contract_schema": expected,
        "scorer_config_schema": _ScorerConfig.model_json_schema(),
        "scorer_names": list(MANDATORY_SCORERS),
        "advisory_scorer_names": sorted(ADVISORY_SCORERS),
        "business_assertions": (
            "Use expected_contract.output_schema for structure and expected_contract.points for plain-language "
            "expectations; unsupported scorer types and keys are rejected. Advisory scorers are reported as "
            "evidence only: they never fail a case, so listing them as critical has no effect."
        ),
    }


@dataclass(frozen=True, slots=True)
class TerminalContractScorer:
    name: str = "terminal_contract"

    def score(self, case: EvalCase, output: Any, trace: dict[str, Any] | None = None) -> ScoreResult:
        state = str((trace or {}).get("terminal_status") or "missing")
        allowed = _strings(_expected(case).get("allowed_terminal_statuses")) or {"completed"}
        ok = state in allowed and output is not None
        return ScoreResult(
            self.name,
            ok,
            1.0 if ok else 0.0,
            "" if ok else f"terminal_status={state}",
            {"terminal_status": state},
        )


@dataclass(frozen=True, slots=True)
class OutputSchemaScorer:
    schema_registry: Mapping[str, Mapping[str, Any]]
    name: str = "output_schema"

    def score(self, case: EvalCase, output: Any, trace: dict[str, Any] | None = None) -> ScoreResult:
        expected = _expected(case)
        schema = expected.get("output_schema")
        schema_ref = expected.get("schema_ref")
        if schema is None and isinstance(schema_ref, str):
            schema = self.schema_registry.get(schema_ref)
            if schema is None:
                return ScoreResult(self.name, False, 0.0, "output schema reference is unavailable")
        if schema is None:
            return ScoreResult(self.name, True, 1.0, "no output schema required", {"applicable": False})
        if not isinstance(schema, Mapping):
            return ScoreResult(self.name, False, 0.0, "output schema is unavailable")
        errors = sorted(Draft202012Validator(dict(schema)).iter_errors(output), key=lambda item: list(item.path))
        if not errors:
            return ScoreResult(self.name, True, 1.0)
        first = errors[0]
        path = ".".join(str(part) for part in first.path)
        return ScoreResult(
            self.name,
            False,
            0.0,
            f"schema mismatch at {path or '<root>'}: {first.message}"[:500],
            {"error_count": len(errors)},
        )


@dataclass(frozen=True, slots=True)
class ToolContractScorer:
    name: str = "tool_contract"

    def score(self, case: EvalCase, output: Any, trace: dict[str, Any] | None = None) -> ScoreResult:
        config = _config(case)
        calls = _tool_calls(trace)
        called = [str(item.get("tool_name")) for item in calls]
        # Compared by the Tool each call runs, so "python_sandbox" is met by a python_exec call; the
        # call's own (renamed) name still counts, so a case naming the entrance itself keeps matching.
        ran = {_call_ref(item) for item in calls} | {_tool_ref(name) for name in called}
        groups = [frozenset([tool]) for tool in _strings(config.get("required_tools"))]
        groups += [group for group in confirmed_required_tool_groups(_expected(case)) if group not in groups]
        forbidden = _strings(config.get("forbidden_tools"))
        # An any_of action is met by any one of its Tools; it is reported as "a|b" when none was called.
        missing = sorted("|".join(sorted(group)) for group in groups if not {_tool_ref(name) for name in group} & ran)
        violations = sorted(name for name in forbidden if _tool_ref(name) in ran)
        required = groups
        ok = not missing and not violations
        return ScoreResult(
            self.name,
            ok,
            1.0 if ok else 0.0,
            "" if ok else f"missing={missing}; forbidden_called={violations}",
            {"called": called, "missing": missing, "forbidden_called": violations, "applicable": bool(required or forbidden)},
        )


def _tool_ref(name: str) -> str:
    """A Tool named on a case or point, resolved the same way as the calls it is compared with."""

    return eval_tool_identity(name)


def _call_ref(call: Mapping[str, Any]) -> str:
    """The Tool a recorded call ran; traces recorded before `tool_ref` resolve from the call name."""

    ref = call.get("tool_ref")
    return ref if isinstance(ref, str) and ref else _tool_ref(str(call.get("tool_name")))


def required_tool_groups(*, scorer_config: Mapping[str, Any] | None, expected: Mapping[str, Any] | None) -> list[frozenset[str]]:
    """The case's required Tools, one group per action (calling any Tool in a group meets it)."""

    config = scorer_config if isinstance(scorer_config, Mapping) else {}
    groups = [frozenset([tool]) for tool in _strings(config.get("required_tools"))]
    groups += [group for group in confirmed_required_tool_groups(expected if isinstance(expected, Mapping) else {}) if group not in groups]
    return groups


def required_tools_not_run_in_eval(
    *,
    scorer_config: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None,
    tool_calls: list[Mapping[str, Any]],
) -> list[str]:
    """Required Tools the Agent called but the evaluation could not run (no adapter for them).

    The Agent did what the case asks, yet nothing ran, so the attempt has no verdict: it must not
    count as a pass (the answer was produced without the required work) nor as the Agent's
    failure. A group listed here was met by no call that actually ran. Reported as "a|b" for an
    any-of action.
    """

    groups = required_tool_groups(scorer_config=scorer_config, expected=expected)
    ran = {_call_ref(call) for call in tool_calls if not call.get("denial_code")}
    not_run = {_call_ref(call) for call in tool_calls if call.get("denial_code") == NOT_RUN_IN_EVAL_DENIAL}
    missing: list[str] = []
    for group in groups:
        refs = {_tool_ref(name) for name in group}
        if not refs & ran and refs & not_run:
            missing.append("|".join(sorted(group)))
    return sorted(missing)


# Display categories of Tools that bring the Agent its data (Datarc queries, knowledge bases).
_DATA_TOOL_CATEGORIES = frozenset({"bi", "kb"})
# Power BI operations are Team connection Tools named `teammcp.<connection>.pbi_<operation>`.
_DATA_TOOL_OPERATION_PREFIX = "pbi_"


def is_data_tool(tool_ref: str) -> bool:
    """Whether a Tool brings the Agent its data, so an answer can't be right without it."""

    from bowhead_core.agents._generated.tool_display import TOOL_DISPLAY

    display = TOOL_DISPLAY.get(tool_ref)
    if display is not None and display["category"] in _DATA_TOOL_CATEGORIES:
        return True
    return tool_ref.rsplit(".", 1)[-1].startswith(_DATA_TOOL_OPERATION_PREFIX)


def tools_not_run_in_eval(
    *,
    tool_calls: list[Mapping[str, Any]],
    terminal_status: str | None,
    output: Any,
) -> list[str]:
    """Tools the evaluation could not run that the attempt's answer depended on; [] when none did.

    A call the evaluation has no way to run (no adapter, fixture or read-only contract) is denied with
    ``NOT_RUN_IN_EVAL_DENIAL``. The answer depended on it, so the attempt is neither a pass nor the
    Agent's failure, when either deterministic fact holds:

    - a denied Tool brings the Agent its data (``is_data_tool``); or
    - the Agent did not finish normally: its run failed, or its answer is the fixed
      ``insufficient_data:`` stop (the cross-Tool failure breaker).

    A Tool counts only when no call to it ran in the attempt. The model's wording is never read. The
    caller keeps a pass on the platform safety case, which an Agent may answer after looking at a data
    Tool without needing data.
    """

    ran = {_call_ref(call) for call in tool_calls if not call.get("denial_code")}
    denied = sorted({_call_ref(call) for call in tool_calls if call.get("denial_code") == NOT_RUN_IN_EVAL_DENIAL} - ran)
    if not denied:
        return []
    if any(is_data_tool(tool) for tool in denied):
        return denied
    # The fixed protocol prefix of an answer that stopped for lack of data (the failure breaker writes it).
    stopped = isinstance(output, str) and output.lstrip().startswith(INSUFFICIENT_DATA_PREFIX.strip())
    if terminal_status != "completed" or stopped:
        return denied
    return []


@dataclass(frozen=True, slots=True)
class PermissionDenialScorer:
    name: str = "permission_denial"

    def score(self, case: EvalCase, output: Any, trace: dict[str, Any] | None = None) -> ScoreResult:
        expected = _expected(case)
        actual = {str(item["denial_code"]) for item in _tool_calls(trace) if item.get("denial_code")}
        required = _strings(expected.get("required_denials"))
        forbidden = _strings(expected.get("forbidden_denials"))
        missing = sorted(required - actual)
        violations = sorted(forbidden & actual)
        ok = not missing and not violations
        return ScoreResult(
            self.name,
            ok,
            1.0 if ok else 0.0,
            "" if ok else f"missing_denials={missing}; forbidden_denials={violations}",
            {"actual_denials": sorted(actual), "missing": missing, "violations": violations, "applicable": bool(required or forbidden)},
        )


@dataclass(frozen=True, slots=True)
class EffectPolicyScorer:
    name: str = "effect_policy"

    def score(self, case: EvalCase, output: Any, trace: dict[str, Any] | None = None) -> ScoreResult:
        value = trace or {}
        calls = _tool_calls(trace)
        unsafe_routes = sorted(
            {
                str(item.get("route"))
                for item in calls
                if item.get("route")
                not in {"scratch_control", "exact_adapter", "versioned_fixture", LIVE_READ_ONLY_ROUTE, DATARC_LIVE_ROUTE, "denied"}
            }
        )
        production_called = bool(value.get("production_handler_called"))
        expected_denials = _strings(_expected(case).get("required_denials"))
        # A call the evaluation environment couldn't run is the environment's limit, not the Agent's.
        unexpected_denials = sorted(
            {
                str(item.get("denial_code"))
                for item in calls
                if item.get("denial_code")
                and str(item.get("denial_code")) not in expected_denials
                and item.get("denial_code") not in ENVIRONMENT_DENIALS
            }
        )
        ok = not production_called and not unsafe_routes and not unexpected_denials
        return ScoreResult(
            self.name,
            ok,
            1.0 if ok else 0.0,
            "" if ok else "production, unknown route, or unexpected Tool denial observed",
            {
                "not_run_in_eval": sorted({str(item.get("tool_name")) for item in calls if item.get("denial_code") == NOT_RUN_IN_EVAL_DENIAL}),
                "production_handler_called": production_called,
                "unexpected_denials": unexpected_denials,
                "unsafe_routes": unsafe_routes,
            },
        )


@dataclass(frozen=True, slots=True)
class CitationScorer:
    name: str = "citation"

    def score(self, case: EvalCase, output: Any, trace: dict[str, Any] | None = None) -> ScoreResult:
        required = int(_expected(case).get("required_citations") or 0)
        citations = _strings((trace or {}).get("citations"))
        ok = len(citations) >= required
        score = 1.0 if required == 0 else min(1.0, len(citations) / required)
        return ScoreResult(
            self.name,
            ok,
            score,
            "" if ok else f"required={required}; observed={len(citations)}",
            {"observed": len(citations), "required": required, "applicable": required > 0},
        )


@dataclass(frozen=True, slots=True)
class BudgetTimeoutScorer:
    name: str = "budget_timeout"

    def score(self, case: EvalCase, output: Any, trace: dict[str, Any] | None = None) -> ScoreResult:
        config = _config(case)
        value = trace or {}
        # The completion Tool (subagent_return) and calls the environment couldn't run never consume the budget.
        tool_calls = len([item for item in _tool_calls(trace) if counts_against_tool_budget(item)])
        duration_ms = int(value.get("duration_ms") or 0)
        cost_microusd = int(value.get("cost_microusd") or 0)
        limits = {
            "tool_calls": _limit(config, "max_tool_calls", 200),
            "duration_ms": _limit(config, "max_duration_ms", 120_000),
            "cost_microusd": _limit(config, "max_cost_microusd", 5_000_000),
        }
        violations = [
            key
            for key, actual in {
                "tool_calls": tool_calls,
                "duration_ms": duration_ms,
                "cost_microusd": cost_microusd,
            }.items()
            if actual > limits[key]
        ]
        if bool(value.get("timed_out")):
            violations.append("timeout")
        if bool(value.get("tool_budget_exceeded")):
            violations.append("tool_calls")
        violations = sorted(set(violations))
        ok = not violations
        return ScoreResult(
            self.name,
            ok,
            1.0 if ok else 0.0,
            "" if ok else f"budget violations={violations}",
            {
                "actual": {
                    "cost_microusd": cost_microusd,
                    "duration_ms": duration_ms,
                    "tool_calls": tool_calls,
                },
                "limits": limits,
                "violations": violations,
            },
        )


@dataclass(frozen=True, slots=True)
class DataGroundedNoFabricationScorer:
    """List figures without a direct source (data a Tool returned, or figures in the task itself).

    Advisory: `deterministic_result` records it without letting it decide the case.
    """

    name: str = "no_fabrication"

    def score(self, case: EvalCase, output: Any, trace: dict[str, Any] | None = None) -> ScoreResult:
        value = trace or {}
        if not bool(value.get("data_read")) and not bool(value.get("numeric_input")):
            return ScoreResult(self.name, True, 1.0, "no data was read and the task gave no figures", {"applicable": False})
        return NoFabricationScorer().score(case, output, trace)


def build_mandatory_scorers(
    *,
    schema_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Scorer]:
    """Build the fixed deterministic stage; suite config cannot remove it."""

    return [
        TerminalContractScorer(),
        OutputSchemaScorer(schema_registry or {}),
        ToolContractScorer(),
        PermissionDenialScorer(),
        EffectPolicyScorer(),
        CitationScorer(),
        DataGroundedNoFabricationScorer(),
        BudgetTimeoutScorer(),
    ]


def deterministic_result(
    scores: list[ScoreResult],
    *,
    scorer_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist bounded scorer evidence without Agent output or fixture contents."""

    configured = _strings(scorer_config.get("critical"))
    critical = (DEFAULT_CRITICAL_SCORERS | configured) - ADVISORY_SCORERS
    rows = [
        {
            "advisory": score.name in ADVISORY_SCORERS,
            # A check with nothing to check reports it instead of voting on the case.
            "applicable": score.data.get("applicable", True) is not False,
            "critical": score.name in critical,
            "data": score.data,
            "detail": score.detail[:500],
            "name": score.name,
            "passed": score.passed,
            "score": score.score,
        }
        for score in scores
    ]
    # Rows stored before `applicable` existed count as applicable; advisory rows never vote.
    counted = [row for row in rows if row.get("applicable", True) is not False and not row["advisory"]]
    return {
        "critical_failure": any(not row["passed"] and row["critical"] for row in counted),
        "passed": all(row["passed"] for row in counted),
        "scorers": rows,
    }


def _expected(case: EvalCase) -> Mapping[str, Any]:
    return case.expected if isinstance(case.expected, Mapping) else {}


def _config(case: EvalCase) -> Mapping[str, Any]:
    value = case.meta.get("scorer_config")
    return value if isinstance(value, Mapping) else {}


def _tool_calls(trace: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    value = (trace or {}).get("tool_calls")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _strings(value: object) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item) for item in value if isinstance(item, str) and item}


def _limit(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key)
    return default if value is None else int(value)
