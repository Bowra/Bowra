"""Plain-language expected points on an Agent evaluation case.

A point is either checked by rules (`required_action`) or graded by the evaluation model
(`data_fact`, `judgment`). A point decides an attempt only when it is confirmed, its kind may
block, and — for model-graded kinds — it is calibrated. Everything else is shown, never voted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bowhead_core.evalkit.scorers import NoFabricationScorer

if TYPE_CHECKING:
    from bowhead_core.agent_eval_judge import EvalPointsGrading

PointKind = Literal["data_fact", "required_action", "judgment"]
PointSource = Literal["owner", "coach_task", "coach_live_answer", "coach_conversation", "coach_alignment"]
PointState = Literal["suggested", "confirmed"]

MAX_POINTS_PER_CASE = 12
MODEL_GRADED_KINDS = frozenset({"data_fact", "judgment"})
# Model-graded kinds join only after the dry run reaches the agreement threshold for that kind
# (technical design §5.6); record the result in the same commit that adds a kind here.
BLOCKING_POINT_KINDS: frozenset[str] = frozenset({"required_action"})
# Coach drafts, the Owner confirms (PRD R8–R11): a drafted point is always `suggested` and says
# where it came from, and a case gets few enough points that each one is read before it's confirmed.
MAX_DRAFTED_POINTS_PER_CASE = 6
COACH_POINT_SOURCES = frozenset({"coach_task", "coach_live_answer", "coach_conversation"})
COACH_POINT_DRAFTING_RULES = (
    "Optional expected_contract.points, at most 6 per case: "
    '{"id":"p_<4-16 lowercase hex>","kind":"data_fact|required_action|judgment","text":"plain words",'
    '"source":"coach_task|coach_live_answer|coach_conversation","state":"suggested"}, plus '
    '"action":{"tool":"<exact Tool name>"} or {"any_of":["<Tool>","<Tool>"]} on a required_action only. '
    "Write one observable thing per point that a reader can check in the answer or its Tool calls. "
    "Draft a required_action only when the Agent's instructions or the Owner say a specific Tool must be used; "
    "otherwise grade the outcome with a judgment or data_fact, since several paths can reach a good answer. "
    "Never copy a figure, name or date from a Live answer into a point unless the point is a data_fact; "
    "live data changes between runs. Points are suggestions until the Owner confirms them."
)


MAX_ACTION_ALTERNATIVES = 5


class _PointAction(BaseModel):
    """One Tool that must be called, or a few alternatives of which any one satisfies the point."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool: str | None = Field(default=None, min_length=1, max_length=200)
    any_of: list[str] | None = Field(default=None, min_length=2, max_length=MAX_ACTION_ALTERNATIVES)

    @model_validator(mode="after")
    def _one_form(self) -> _PointAction:
        if (self.tool is None) == (self.any_of is None):
            raise ValueError("A required action names either one Tool or any_of alternatives")
        if self.tool is not None and not self.tool.strip():
            raise ValueError("A required action names its Tool")
        if self.any_of is not None:
            names = [name.strip() for name in self.any_of]
            if any(not name or len(name) > 200 for name in names) or len(set(names)) != len(names):
                raise ValueError("Alternative Tools must be distinct, non-blank names")
        return self

    def tools(self) -> frozenset[str]:
        return frozenset([self.tool] if self.tool is not None else self.any_of or [])


class PointOrigin(BaseModel):
    """Where a point came from: the Owner's own words (who, when), or a member's thumbs-down.

    A member's comment is theirs: a point from one records the kind only, never the comment or the
    member, so nothing in an immutable suite revision names them (alignment loop §6).
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["owner_words", "member_feedback"] = "owner_words"
    words: str | None = Field(default=None, min_length=1, max_length=500)
    by: str | None = Field(default=None, min_length=1, max_length=200)
    at: str = Field(min_length=1, max_length=40)
    loop_id: str | None = Field(default=None, pattern=r"^aeal_[A-Za-z0-9_-]{1,80}$")

    @model_validator(mode="after")
    def _words_match_kind(self) -> PointOrigin:
        if self.kind == "owner_words" and (self.words is None or self.by is None):
            raise ValueError("A point from the Owner's words records the words and who said them")
        if self.kind == "member_feedback" and (self.words is not None or self.by is not None):
            raise ValueError("A point from a member's thumbs-down records no comment and no member")
        return self


class ExpectedPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^p_[0-9a-f]{4,16}$")
    kind: PointKind
    text: str = Field(min_length=1, max_length=300)
    source: PointSource
    state: PointState
    action: _PointAction | None = None
    # Optional so points saved before the alignment loop keep their stored shape and case hash.
    origin: PointOrigin | None = None
    # The text hash at which the Owner chose "Keep my wording" for Coach's point review.
    review_dismissed_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _action_matches_kind(self) -> ExpectedPoint:
        if not self.text.strip():
            raise ValueError("Point text must not be blank")
        if self.kind == "required_action":
            if self.action is None:
                raise ValueError("A required action names its Tool")
        elif self.action is not None:
            raise ValueError("Only a required action names a Tool")
        return self


def suite_revision_grades_points_sql(suite_revision_column: str) -> str:
    """SQL predicate: the suite revision has a case with a model-graded point to send to the judge."""

    return (
        "EXISTS(SELECT 1 FROM agent_eval_case_revision point_case,"
        " json_each(point_case.expected_contract_json,'$.points') point"
        f" WHERE point_case.suite_revision_id={suite_revision_column}"
        " AND json_extract(point.value,'$.kind') IN ('data_fact','judgment'))"
    )


def validate_points(points: Sequence[ExpectedPoint]) -> None:
    if len(points) > MAX_POINTS_PER_CASE:
        raise ValueError("A case has at most 12 points")
    ids = [point.id for point in points]
    if len(ids) != len(set(ids)):
        raise ValueError("Point ids must be unique within a case")


def is_held_out(expected: Mapping[str, Any] | None) -> bool:
    """A held-out case is hidden from Coach's reads except as a pass count (alignment loop A6)."""

    return (expected or {}).get("held_out") is True


def validate_drafted_points(expected: Mapping[str, Any] | None) -> None:
    """Reject Coach-drafted points that claim the Owner's confirmation or exceed the draft budget.

    Coach can't mark a case held out either: the platform assigns it and only the Owner changes it.
    """

    if expected is not None and "held_out" in expected:
        raise ValueError("Only the platform or the Owner marks a case held out")
    points = case_points(expected)
    if len(points) > MAX_DRAFTED_POINTS_PER_CASE:
        raise ValueError("Coach drafts at most 6 points per case")
    for point in points:
        if point.get("state") != "suggested" or point.get("source") not in COACH_POINT_SOURCES:
            raise ValueError("A drafted point is suggested by Coach")


def drafted_points(
    raw: object,
    *,
    source: PointSource,
    allowed_tools: Iterable[str],
    id_factory: Callable[[], str],
) -> list[dict[str, Any]]:
    """Normalize model-drafted points into valid suggested points; anything malformed is dropped.

    A required action must name a Tool the Agent can call, otherwise it could never be met.
    """

    if not isinstance(raw, list):
        return []
    tools = frozenset(allowed_tools)
    points: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    for item in raw:
        if len(points) >= MAX_DRAFTED_POINTS_PER_CASE:
            break
        if not isinstance(item, Mapping):
            continue
        kind, text, tool = item.get("kind"), item.get("text"), item.get("tool")
        if kind not in ("data_fact", "required_action", "judgment") or not isinstance(text, str):
            continue
        text = " ".join(text.split())[:300]
        if not text or text.casefold() in seen_texts:
            continue
        value: dict[str, Any] = {"id": id_factory(), "kind": kind, "text": text, "source": source, "state": "suggested"}
        if kind == "required_action":
            action = _drafted_action(tool, item.get("tools"), tools)
            if action is None:
                continue
            value["action"] = action
        try:
            ExpectedPoint.model_validate(value)
        except ValueError:
            continue
        seen_texts.add(text.casefold())
        points.append(value)
    return points


def _drafted_action(tool: object, alternatives: object, allowed: frozenset[str]) -> dict[str, Any] | None:
    """A drafted required action keeps only Tools the Agent can call, otherwise it could never be met."""

    if isinstance(tool, str):
        return {"tool": tool} if tool in allowed else None
    if not isinstance(alternatives, list):
        return None
    names = list(dict.fromkeys(name for name in alternatives if isinstance(name, str) and name in allowed))[:MAX_ACTION_ALTERNATIVES]
    if len(names) == 1:
        return {"tool": names[0]}
    return {"any_of": names} if names else None


def case_points(expected: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return a stored case's points; cases saved before points existed have none."""

    value = (expected or {}).get("points")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping) and isinstance(item.get("id"), str)]  # type: ignore[misc]


def has_model_graded_points(expected: Mapping[str, Any] | None) -> bool:
    return any(point.get("kind") in MODEL_GRADED_KINDS for point in case_points(expected))


def confirmed_required_tool_groups(expected: Mapping[str, Any] | None) -> list[frozenset[str]]:
    """One group per confirmed required action; calling any Tool in a group meets it.

    A suggested action is shown, never checked.
    """

    groups: list[frozenset[str]] = []
    for point in case_points(expected):
        action = point.get("action")
        if point.get("kind") != "required_action" or point.get("state") != "confirmed" or not isinstance(action, Mapping):
            continue
        tool, alternatives = action.get("tool"), action.get("any_of")
        if isinstance(tool, str) and tool:
            groups.append(frozenset([tool]))
        elif isinstance(alternatives, list):
            names = frozenset(name for name in alternatives if isinstance(name, str) and name)
            if names:
                groups.append(names)
    return groups


def is_blocking_point(point: Mapping[str, Any], *, calibrated_point_ids: Iterable[str] = ()) -> bool:
    """Whether a model-graded point can decide the attempt (technical design §5.3)."""

    if point.get("state") != "confirmed" or point.get("kind") not in BLOCKING_POINT_KINDS:
        return False
    if point.get("kind") in MODEL_GRADED_KINDS:
        return str(point.get("id")) in set(calibrated_point_ids)
    return True


def model_graded_points(expected: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [point for point in case_points(expected) if point.get("kind") in MODEL_GRADED_KINDS]


def graded_point_rows(
    points: Sequence[Mapping[str, Any]],
    grading: EvalPointsGrading | None,
    *,
    data_read: bool,
    authorized_numbers: Iterable[float],
    calibrated_point_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Turn one judge call into per-point evidence; data facts are decided by rules, not the model."""

    calibrated = frozenset(calibrated_point_ids)
    authorized = tuple(authorized_numbers)
    unavailable = (grading.failure_code if grading is not None else None) or "agent_eval_judge_unavailable"
    rows: list[dict[str, Any]] = []
    for point in points:
        kind = str(point.get("kind"))
        row: dict[str, Any] = {
            "id": str(point.get("id")),
            "kind": kind,
            "state": str(point.get("state")),
            "blocking": is_blocking_point(point, calibrated_point_ids=calibrated),
        }
        if kind == "data_fact" and not data_read:
            # No data was read, so there is nothing to compare a stated figure with (PRD R2).
            rows.append({**row, "applicable": False, "met": None, "quote": None, "reason": "no_data_read"})
            continue
        verdict = grading.verdicts.get(row["id"]) if grading is not None and grading.status == "graded" else None
        if verdict is None:
            rows.append({**row, "applicable": True, "met": None, "quote": None, "reason": unavailable})
            continue
        met, reason = verdict.met, verdict.reason
        if kind == "data_fact" and met is True and verdict.value is not None:
            # The rule can only withdraw a "met": a judge's "not met" stands even when the stated
            # figure appears in the Tool results, because the answer may state it for something else.
            matched = NoFabricationScorer().stated_value_matches(verdict.value, authorized)
            met = matched
            reason = "value_has_no_figure" if matched is None else (None if matched else "value_not_in_tool_results")
        graded = {**row, "applicable": True, "met": met, "quote": verdict.quote, "reason": reason}
        if kind == "data_fact":
            graded["value"] = verdict.value
        rows.append(graded)
    return rows


def points_evidence(
    rows: Sequence[Mapping[str, Any]],
    grading: EvalPointsGrading | None,
    *,
    model_fingerprint: str | None,
) -> dict[str, Any]:
    """The `judge_json` document for v2 grading (technical design §5.2)."""

    from bowhead_core.agent_eval_judge import POINTS_PROMPT_VERSION

    evidence = dict(grading.evidence) if grading is not None else {}
    document: dict[str, Any] = {
        **{key: evidence[key] for key in ("model", "observed_route_fingerprint", "provider", "tokens_input", "tokens_output") if key in evidence},
        "cost_microusd": grading.cost_microusd if grading is not None else 0,
        "duration_ms": grading.duration_ms if grading is not None else 0,
        "model_fingerprint": model_fingerprint,
        "points": [dict(row) for row in rows],
        "prompt_version": POINTS_PROMPT_VERSION,
        "schema_version": 2,
    }
    failure_code = grading.failure_code if grading is not None else "agent_eval_judge_unavailable"
    if failure_code is not None:
        document["failure_code"] = failure_code
    document["points_passed"] = points_passed(rows)
    # A blocking point the model couldn't grade doesn't block; the result says so (PRD R17).
    not_graded = sorted(str(row["id"]) for row in rows if _counts(row) and row.get("met") is None)
    if not_graded:
        document["blocking_not_graded"] = not_graded
    return document


def _counts(row: Mapping[str, Any]) -> bool:
    return bool(row.get("blocking")) and row.get("applicable", True) is not False


def points_passed(graded: Sequence[Mapping[str, Any]]) -> bool:
    """The points verdict alone: no blocking point that applies was graded unmet."""

    return not any(_counts(row) and row.get("met") is False for row in graded)


def settle_points(
    *,
    rule_passed: bool,
    graded: Sequence[Mapping[str, Any]],
) -> Literal["passed", "failed"]:
    """Combine the rule verdict with blocking points.

    `graded` rows carry `blocking`, `applicable` and `met` (true, false or None for not graded).
    Only a blocking point graded unmet fails the attempt. A blocking point the model couldn't
    grade never blocks: the rules decide, and the evidence records it as not graded (PRD R17).
    """

    return "passed" if rule_passed and points_passed(graded) else "failed"


def unmet_blocking_point_ids(judge: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Read the blocking points an attempt failed on from its stored judge evidence."""

    points = (judge or {}).get("points")
    if not isinstance(points, list):
        return ()
    return tuple(
        sorted(
            str(row["id"])
            for row in points
            if isinstance(row, Mapping)
            and isinstance(row.get("id"), str)
            and row.get("blocking") is True
            and row.get("applicable", True) is not False
            and row.get("met") is False
        )
    )


__all__ = [
    "BLOCKING_POINT_KINDS",
    "COACH_POINT_DRAFTING_RULES",
    "COACH_POINT_SOURCES",
    "MAX_DRAFTED_POINTS_PER_CASE",
    "MAX_POINTS_PER_CASE",
    "MODEL_GRADED_KINDS",
    "ExpectedPoint",
    "PointOrigin",
    "case_points",
    "confirmed_required_tool_groups",
    "drafted_points",
    "has_model_graded_points",
    "graded_point_rows",
    "is_held_out",
    "is_blocking_point",
    "model_graded_points",
    "points_evidence",
    "points_passed",
    "settle_points",
    "suite_revision_grades_points_sql",
    "unmet_blocking_point_ids",
    "validate_drafted_points",
    "validate_points",
]
