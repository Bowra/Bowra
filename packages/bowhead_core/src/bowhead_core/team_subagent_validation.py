"""Canonical validation rules shared by Subagent drafts and definitions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from bowhead_core.agent_model_policy import TEAM_SUBAGENT_ALLOWED_MODEL_ROLES
from bowhead_core.agents._generated.tool_renames import canonical_tool_name
from bowhead_core.agents.exceptions import (
    ManifestSecurityError,
    ManifestValidationError,
)
from bowhead_core.agents.manifest_security import lint_user_append
from bowhead_core.agents.registry import _validate_jsonschema
from bowhead_core.custom_subagent_store import (
    _NAME_RE,
    MAX_ITERATIONS_CAP,
)
from bowhead_core.skill_upload import NAME_PATTERN as _SKILL_NAME_RE
from bowhead_core.subagent_authoring_catalog import (
    APPROVAL_EXECUTOR_UNAVAILABLE,
    COMPILER_DERIVED_SKILL_TOOLS,
    SubagentAuthoringCatalogSnapshot,
)
from bowhead_core.subagent_prompt_limits import (
    MAX_PROMPT_CHARS_TEAM,
    MAX_PROMPT_TOKENS,
    system_prompt_token_overflow,
)

MAX_DESCRIPTION = 200
# Character limit is only a pre-tokenization check; the language-independent gate is MAX_PROMPT_TOKENS (see
# subagent_prompt_limits module docs: equal character counts differ sixfold in tokens between Chinese/English).
MAX_SYSTEM_PROMPT = MAX_PROMPT_CHARS_TEAM
MAX_TOOL_REFS = 60
MAX_SKILLS = 24
OUTPUT_SCHEMA_MAX_BYTES = 8192
OUTPUT_SCHEMA_MAX_DEPTH = 12
OUTPUT_SCHEMA_MAX_NODES = 250
OUTPUT_SCHEMA_DESCRIPTION = (
    "Optional JSON Schema constraints on the complete internal AnalysisPayload returned by subagent_return, "
    "not the final Chat text. Use null for free-text answers and put presentation requirements in system_prompt. "
    "A Chat payload uses delivery_surface='chat' and exec_summary; a closed object must permit its platform fields. "
    "Static validation does not prove business quality or all conditional schema compatibility. "
    "Runtime schema corrections have bounded retries and may degrade; evaluate the actual answer separately."
)
OUTPUT_SCHEMA_BANNED_KEYWORDS = (
    "$ref",
    "$dynamicRef",
    "$recursiveRef",
    "$dynamicAnchor",
    "$recursiveAnchor",
    "pattern",
    "format",
    "patternProperties",
    "uniqueItems",
)
_SCHEMA_MAP_KEYWORDS = (
    "properties",
    "$defs",
    "definitions",
    "dependentSchemas",
)


class TeamSubagentValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        tool_ref: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.tool_ref = tool_ref
        # Safe, actionable facts (for example which Skill or Tool blocks the dependency check).
        self.details = dict(details or {})


@dataclass(frozen=True)
class TeamSubagentDefinitionCandidate:
    name: str
    display_name: str
    description: str
    system_prompt: str
    tool_refs: tuple[str, ...]
    max_iterations: int
    skills: tuple[str, ...]
    model: str | None
    output_schema: Mapping[str, Any] | None
    audience_mode: str


def validate_name(name: str, *, reserved_names: set[str]) -> None:
    if not _NAME_RE.match(name):
        raise TeamSubagentValidationError(
            "subagent_name_invalid",
            "name must match ^[a-z][a-z0-9_-]{1,31}$",
            field="name",
        )
    if name in reserved_names:
        raise TeamSubagentValidationError(
            "subagent_name_reserved",
            f"name {name!r} is reserved (a built-in/system agent)",
            field="name",
        )


def validate_model(model: str | None) -> None:
    if model is not None and model not in TEAM_SUBAGENT_ALLOWED_MODEL_ROLES:
        raise TeamSubagentValidationError(
            "subagent_model_not_allowed",
            f"model {model!r} not allowed (choose one of: {sorted(TEAM_SUBAGENT_ALLOWED_MODEL_ROLES)})",
            field="model",
        )


def validate_skills(skills: tuple[str, ...] | list[str]) -> None:
    if len(skills) > MAX_SKILLS:
        raise TeamSubagentValidationError(
            "subagent_too_many_skills",
            f"too many skills (>{MAX_SKILLS})",
            field="skills",
        )
    for name in skills:
        if not _SKILL_NAME_RE.fullmatch(name):
            raise TeamSubagentValidationError(
                "subagent_skill_name_invalid",
                f"invalid skill name {name!r} (must be a skill slug)",
                field="skills",
            )


def validate_common(
    *,
    display_name: str = "",
    description: str,
    system_prompt: str,
    tool_refs: tuple[str, ...] | list[str],
    max_iterations: int,
    allow_empty_tools: bool = False,
) -> None:
    if len(display_name) > 40:
        raise TeamSubagentValidationError(
            "subagent_display_name_too_long",
            "display_name too long (>40)",
            field="display_name",
        )
    if len(description) > MAX_DESCRIPTION:
        raise TeamSubagentValidationError(
            "subagent_description_too_long",
            f"description too long (>{MAX_DESCRIPTION})",
            field="description",
        )
    if not system_prompt.strip():
        # Minimum length. Previously only the maximum was checked, making `system_prompt` optional throughout (optional FE type → gateway
        # `CreateBody.system_prompt: str = ""` → no empty check here). Production actually created instruction-free
        # agents: 3 of 6 team agents on 116 had empty prompts, including `pharma-competitive-research`,
        # dispatched by Lead for an entire turn (t_5d59b5d71fde, 2026-09-07). Its runtime
        # `profile.system_prompt` was empty (no skills or authoring pseudo-tools ⇒ empty prelude),
        # leaving only ws_meta/memory/language blocks, without role, boundaries or output contract, while Lead dispatched it
        # based on its carefully written description.
        #
        # Existing definitions are not locked out: status-only toggles follow another path (see routes/team_subagents.py's
        # `if body.model_fields_set:` comment); old definitions can still be disabled even if today's validator rejects them.
        raise TeamSubagentValidationError(
            "subagent_system_prompt_required",
            "system_prompt is required (an agent without instructions runs with none)",
            field="system_prompt",
        )
    if len(system_prompt) > MAX_SYSTEM_PROMPT:
        raise TeamSubagentValidationError(
            "subagent_system_prompt_too_long",
            f"system_prompt too long (>{MAX_SYSTEM_PROMPT})",
            field="system_prompt",
        )
    prompt_tokens = system_prompt_token_overflow(system_prompt)
    if prompt_tokens is not None:
        raise TeamSubagentValidationError(
            "subagent_system_prompt_too_many_tokens",
            f"system_prompt too long ({prompt_tokens} tokens, >{MAX_PROMPT_TOKENS})",
            field="system_prompt",
        )
    if not tool_refs and not allow_empty_tools:
        raise TeamSubagentValidationError(
            "subagent_tool_required",
            "select at least one tool (text-only creation is not enabled for this request)",
            field="tool_refs",
        )
    if len(tool_refs) > MAX_TOOL_REFS:
        raise TeamSubagentValidationError(
            "subagent_too_many_tools",
            f"too many tools (>{MAX_TOOL_REFS})",
            field="tool_refs",
        )
    if not (1 <= max_iterations <= MAX_ITERATIONS_CAP):
        raise TeamSubagentValidationError(
            "subagent_max_iterations_invalid",
            f"max_iterations must be in [1,{MAX_ITERATIONS_CAP}]",
            field="max_iterations",
        )
    for field_name, text in (
        ("description", description),
        ("system_prompt", system_prompt),
    ):
        try:
            lint_user_append(text)
        except ManifestSecurityError as exc:
            raise TeamSubagentValidationError(
                "subagent_prompt_security_rejected",
                f"{field_name} rejected by injection lint: {type(exc).__name__}",
                field=field_name,
            ) from exc


def validate_output_schema(schema: Mapping[str, Any] | None) -> None:
    if schema is None:
        return
    if not isinstance(schema, dict):
        raise TeamSubagentValidationError(
            "subagent_output_schema_invalid",
            "output_schema must be a JSON object",
            field="output_schema",
        )
    try:
        raw = json.dumps(schema, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise TeamSubagentValidationError(
            "subagent_output_schema_invalid",
            f"output_schema not JSON-serializable: {exc}",
            field="output_schema",
        ) from exc
    except RecursionError as exc:
        raise TeamSubagentValidationError(
            "subagent_output_schema_too_deep",
            "output_schema too deeply nested",
            field="output_schema",
        ) from exc
    if len(raw.encode("utf-8")) > OUTPUT_SCHEMA_MAX_BYTES:
        raise TeamSubagentValidationError(
            "subagent_output_schema_too_large",
            f"output_schema too large (>{OUTPUT_SCHEMA_MAX_BYTES} bytes)",
            field="output_schema",
        )

    stack: list[tuple[Any, int]] = [(schema, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        if depth > OUTPUT_SCHEMA_MAX_DEPTH:
            raise TeamSubagentValidationError(
                "subagent_output_schema_too_deep",
                f"output_schema too deeply nested (>{OUTPUT_SCHEMA_MAX_DEPTH})",
                field="output_schema",
            )
        nodes += 1
        if nodes > OUTPUT_SCHEMA_MAX_NODES:
            raise TeamSubagentValidationError(
                "subagent_output_schema_too_complex",
                f"output_schema too complex (>{OUTPUT_SCHEMA_MAX_NODES} nodes)",
                field="output_schema",
            )
        if isinstance(node, dict):
            for banned in OUTPUT_SCHEMA_BANNED_KEYWORDS:
                if banned in node:
                    raise TeamSubagentValidationError(
                        "subagent_output_schema_keyword_forbidden",
                        f"output_schema may not use {banned!r} (recursion / ReDoS / SSRF guard)",
                        field="output_schema",
                    )
            for key, value in node.items():
                if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
                    for child in value.values():
                        if isinstance(child, (dict, list)):
                            stack.append((child, depth + 1))
                elif isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
    try:
        _validate_jsonschema(dict(schema), "output_schema")
    except ManifestValidationError as exc:
        raise TeamSubagentValidationError(
            "subagent_output_schema_invalid",
            str(exc),
            field="output_schema",
        ) from exc
    from jsonschema import Draft202012Validator, SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, RecursionError) as exc:
        raise TeamSubagentValidationError(
            "subagent_output_schema_invalid",
            "output_schema must be a valid Draft 2020-12 JSON Schema",
            field="output_schema",
        ) from exc
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if "object" not in types:
        raise TeamSubagentValidationError(
            "subagent_output_schema_invalid", "output_schema must accept an internal AnalysisPayload object", field="output_schema"
        )
    if schema.get("additionalProperties") is False:
        allowed = {key for key, value in schema.get("properties", {}).items() if value is not False}
        if (
            not ("exec_summary" in allowed or "error" in allowed or {"title", "deliverables"} <= allowed)
            or not set(schema.get("required", [])) <= allowed
        ):
            raise TeamSubagentValidationError(
                "subagent_output_schema_invalid",
                "A closed output_schema must allow a platform payload: exec_summary, error, or title with deliverables, "
                "and every required property. Use null for free-text output.",
                field="output_schema",
            )


def validate_tool_refs(
    tool_refs: tuple[str, ...] | list[str],
    *,
    catalog: SubagentAuthoringCatalogSnapshot,
    require_selectable: bool,
    existing_tool_refs: tuple[str, ...] | list[str] = (),
) -> None:
    tools = catalog.tools_by_ref
    existing = {canonical_tool_name(ref) for ref in existing_tool_refs}
    for tool_ref in tool_refs:
        # C3 boundary: customers stored **pre-rename** names, while the catalog shows only current names (criterion 1d).
        # Without normalization, editing such custom agents would become invalid, forcing customers to change their own configuration.
        # Errors still return **the user's original submitted name**, matching frontend selector rows.
        # See docs/design/工具改名迁移.md for the specification.
        if canonical_tool_name(tool_ref) in COMPILER_DERIVED_SKILL_TOOLS:
            # Accepted and ignored: Skill grants decide the Skill wrapper Tools.
            continue
        tool = tools.get(canonical_tool_name(tool_ref))
        if tool is None or not tool.discoverable:
            raise TeamSubagentValidationError(
                "subagent_tool_no_longer_available",
                f"unknown/disallowed tool ref: {tool_ref!r}",
                field="tool_refs",
                tool_ref=tool_ref,
            )
        # Only the temporary approval-executor lock can retain a server-owned
        # binding. Unknown risk, missing tools and license locks still reject.
        selection = tool.for_selection()
        retained = tool.selectable and selection.locked_reason == APPROVAL_EXECUTOR_UNAVAILABLE and canonical_tool_name(tool_ref) in existing
        if require_selectable and not selection.selectable and not retained:
            raise TeamSubagentValidationError(
                "subagent_tool_not_selectable",
                f"tool ref is not selectable: {tool_ref!r}",
                field="tool_refs",
                tool_ref=tool_ref,
            )


def validate_candidate(
    candidate: TeamSubagentDefinitionCandidate,
    *,
    catalog: SubagentAuthoringCatalogSnapshot,
    reserved_names: set[str],
    operation: Literal["draft", "create", "edit", "enable"],
    allow_empty_tools: bool = False,
) -> None:
    validate_name(candidate.name, reserved_names=reserved_names)
    validate_common(
        display_name=candidate.display_name,
        description=candidate.description,
        system_prompt=candidate.system_prompt,
        tool_refs=candidate.tool_refs,
        max_iterations=candidate.max_iterations,
        allow_empty_tools=allow_empty_tools,
    )
    validate_model(candidate.model)
    validate_skills(candidate.skills)
    validate_output_schema(candidate.output_schema)
    validate_tool_refs(
        candidate.tool_refs,
        catalog=catalog,
        require_selectable=operation in {"create", "enable"},
    )
    if operation == "create" and candidate.audience_mode != "private":
        raise TeamSubagentValidationError(
            "subagent_create_must_be_private",
            "conversational Subagent creation must use private audience",
            field="audience_mode",
        )


__all__ = [
    "TeamSubagentDefinitionCandidate",
    "TeamSubagentValidationError",
    "validate_candidate",
    "validate_common",
    "validate_model",
    "validate_name",
    "validate_output_schema",
    "validate_skills",
    "validate_tool_refs",
]
