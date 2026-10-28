"""Typed Configuration Agent adapters backed by the original Team settings writers."""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any, get_type_hints

from bowhead_core.config_copilot import POLICY_GENERATED, FieldIssue, FormContext, FormRegistration, LiteApplyError, MaterializeResult, register_form
from bowhead_core.configuration_contracts import team_configuration_enabled
from fastapi import Response
from pydantic import ConfigDict, Field, create_model

from bowhead_core import agent_run_limits as limits
from bowhead_core import execution_limits
from bowhead_gateway.routes import team_settings

RunLimitsDraft = create_model(
    "RunLimitsDraft",
    __config__=ConfigDict(extra="forbid", strict=True),
    **{
        item.name: (
            get_type_hints(limits.AgentRunLimits)[item.name],
            Field(
                default=item.default,
                description=item.metadata["description"],
                ge=item.metadata["minimum"],
                le=item.metadata["maximum"],
            ),
        )
        for item in fields(limits.AgentRunLimits)
    },
)


async def run_limits_read(ctx: FormContext) -> dict[str, Any]:
    team_settings._require_run_limits_feature()
    raw = await ctx.deps["team_settings_store"].get(ctx.team_id, limits.SETTINGS_KEY)
    values = asdict(limits.parse_stored(raw))
    defaults = asdict(limits.DEFAULT_LIMITS)
    return {
        "revision": team_settings._run_limits_version(raw),
        "fields": {key: {"value": value, "unset": raw is None, "default": defaults[key]} for key, value in values.items()},
    }


async def run_limits_materialize(ctx: FormContext, patch: dict[str, Any]) -> MaterializeResult:
    current = await run_limits_read(ctx)
    limits.validate({**{key: info["value"] for key, info in current["fields"].items()}, **patch})
    return MaterializeResult(normalized=patch)


async def run_limits_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str):
    if op != "apply":
        raise LiteApplyError("undo_not_supported", "Review a new execution-limit change to restore earlier values.")
    current = await run_limits_read(ctx)
    revisions = {row.get("baseline_revision") for row in rows}
    if len(revisions) != 1 or current["revision"] not in revisions:
        raise LiteApplyError("configuration_baseline_changed", "Execution limits changed. Read again before proposing.")
    request, user = ctx.deps["request"], ctx.deps["actor"]
    args = {"request": request, "user": user, "user_store": ctx.deps["user_store"], "team_store": ctx.deps["team_settings_store"]}
    if any(row.get("reset_all") for row in rows):
        if not all(row.get("operation") == "reset" and row.get("reset_all") for row in rows):
            raise LiteApplyError("apply_validation_failed", "Invalid execution-limit reset.")
        if any(row["to"] != current["fields"][row["field"]]["default"] for row in rows):
            raise LiteApplyError("configuration_baseline_changed", "Execution-limit defaults changed. Read again before proposing.")
        await team_settings.delete_agent_run_limits(**args, expected_version=current["revision"])
    else:
        values = {key: info["value"] for key, info in current["fields"].items()}
        for row in rows:
            if row.get("field") not in values or row.get("operation", "set") != "set":
                raise LiteApplyError("apply_validation_failed", "Invalid execution-limit field.")
            values[row["field"]] = row["to"]
        await team_settings.put_agent_run_limits(team_settings.AgentRunLimitsBody(**values, expected_version=current["revision"]), **args)
    # The original writer already records the domain audit.
    return []


async def execution_limits_read(ctx: FormContext) -> dict[str, Any]:
    raw = await ctx.deps["team_settings_store"].get(ctx.team_id, execution_limits.SETTINGS_KEY)
    view = team_settings._execution_limits_view(raw)
    return {
        "revision": view["version"],
        "fields": {
            item.name: {"value": view[item.name], "unset": view["is_default"], "default": view["defaults"][item.name]}
            for item in fields(execution_limits.ExecutionLimits)
        },
    }


async def execution_limits_materialize(ctx: FormContext, patch: dict[str, Any]) -> MaterializeResult:
    current = await execution_limits_read(ctx)
    try:
        execution_limits.validate({**{key: info["value"] for key, info in current["fields"].items()}, **patch})
    except execution_limits.ExecutionLimitsInvalid as exc:
        return MaterializeResult(normalized={}, issues=[FieldIssue("_root", "invalid_execution_limits", str(exc))])
    return MaterializeResult(normalized=patch)


async def execution_limits_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str):
    if op != "apply":
        raise LiteApplyError("undo_not_supported", "Review a new sandbox or HTML limit change to restore earlier values.")
    current = await execution_limits_read(ctx)
    if not rows or any(row.get("baseline_revision") != current["revision"] for row in rows):
        raise LiteApplyError("configuration_baseline_changed", "Sandbox or HTML limits changed. Read again before proposing.")
    values = {key: info["value"] for key, info in current["fields"].items()}
    if any(row.get("reset_all") for row in rows):
        if not all(row.get("operation") == "reset" and row.get("reset_all") for row in rows):
            raise LiteApplyError("apply_validation_failed", "Invalid execution-limit reset.")
        if any(row["to"] != current["fields"][row["field"]]["default"] for row in rows):
            raise LiteApplyError("configuration_baseline_changed", "Deployment defaults changed. Read again before proposing.")
        values = None
    else:
        for row in rows:
            if row.get("field") not in values or row.get("operation", "set") != "set":
                raise LiteApplyError("apply_validation_failed", "Invalid execution-limit field.")
            values[row["field"]] = row["to"]
    await team_settings.put_execution_limits(
        team_settings.ExecutionLimitsBody(limits=values, expected_version=current["revision"]),
        request=ctx.deps["request"],
        response=Response(),
        user=ctx.deps["actor"],
        team_store=ctx.deps["team_settings_store"],
        user_store=ctx.deps["user_store"],
    )
    return []


async def domain_context(ctx: FormContext) -> str:
    return "Use the current authorized JSON Schema and guidance. Preserve unrequested values and request explicit user review."


def team_adapter(with_deps, handler):
    async def guarded(ctx, *args):
        store = ctx.deps.get("user_store")
        if not team_configuration_enabled() or not ctx.team_id or store is None:
            raise LiteApplyError("form_unavailable", "Team configuration assistance is unavailable.")
        actor = await store.get_user(ctx.user_id)
        if (
            actor is None
            or actor.status != "active"
            or not await store.user_has_active_team_membership(ctx.user_id, ctx.team_id)
            or await store.get_team_role(ctx.user_id, ctx.team_id) not in {"owner", "team_admin"}
        ):
            raise LiteApplyError("permission_denied", "A current Team owner or administrator is required.")
        return await handler(ctx, *args)

    return with_deps(guarded)


def register_team_configuration_forms(with_deps) -> None:
    register_form(
        FormRegistration(
            form_id="team_run_limits",
            schema=RunLimitsDraft,
            guidance="Change only requested Team execution limits. Null step_limit means unlimited; null model_call_time_limit_s means automatic. Use reset_all to remove the Team override and restore product defaults. Changes affect subsequent turns.",
            field_policy=dict.fromkeys(RunLimitsDraft.model_fields, POLICY_GENERATED),
            context_provider=team_adapter(with_deps, domain_context),
            materializer=team_adapter(with_deps, run_limits_materialize),
            risk_tier=1,
            read_accessor=team_adapter(with_deps, run_limits_read),
            lite_applier=team_adapter(with_deps, run_limits_apply),
            read_scope="team_admin",
            write_scope="team_admin",
            write_descriptor="PUT/DELETE /api/v1/team-settings/agent-run-limits",
            typed_patch=True,
            reset_all=True,
            lite_revision_field="revision",
        )
    )

    view = team_settings._execution_limits_view(None)
    schema = create_model(
        "ExecutionLimitsDraft",
        __config__=ConfigDict(extra="forbid", strict=True),
        **{
            item.name: (
                int,
                Field(
                    default=view["defaults"][item.name],
                    description=item.metadata["description"],
                    ge=view["ranges"][item.name]["min"],
                    le=view["ranges"][item.name]["max"],
                ),
            )
            for item in fields(execution_limits.ExecutionLimits)
        },
    )
    register_form(
        FormRegistration(
            form_id="team_execution_limits",
            schema=schema,
            guidance="Preserve unrequested sandbox and HTML limits. Durations are seconds and HTML size is bytes. The default sandbox duration cannot exceed the Team maximum or the deployment ceiling. reset_all restores deployment defaults for this group, independently of agent loop limits. Changes affect new tasks.",
            field_policy=dict.fromkeys(schema.model_fields, POLICY_GENERATED),
            context_provider=team_adapter(with_deps, domain_context),
            materializer=team_adapter(with_deps, execution_limits_materialize),
            read_accessor=team_adapter(with_deps, execution_limits_read),
            lite_applier=team_adapter(with_deps, execution_limits_apply),
            risk_tier=1,
            read_scope="team_admin",
            write_scope="team_admin",
            typed_patch=True,
            reset_all=True,
            lite_revision_field="revision",
            write_descriptor="PUT /api/v1/team-settings/execution-limits",
        )
    )
