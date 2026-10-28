"""Ordered per-call authorization policies for ``ToolRegistry.execute``.

Each policy holds one guard that used to be inline in ``ToolRegistry.execute``
and keeps its exact decision, denial shape, metric, audit record and log line.
``TOOL_CALL_POLICIES`` fixes the order. ``ToolRegistry.admit`` runs the
``BEFORE_PRESENTATION`` stage before ``SubagentRunner._execute_tool`` emits the
step card; ``ToolRegistry.execute`` runs the ``BEFORE_DISPATCH`` stage. See docs/design/tool-call-policy-chain-tech-design.md.

Helpers that live in ``tool_registry`` are read from that module at call time,
so tests that patch them there keep working.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec
    from bowhead_core.types import ToolInvocation

logger = logging.getLogger("bowhead_core.tool_registry")


class PolicyStage(StrEnum):
    BEFORE_PRESENTATION = "before_presentation"
    BEFORE_DISPATCH = "before_dispatch"


@dataclass(frozen=True)
class ToolCallRequest:
    # BEFORE_DISPATCH: canonical name and unwrapped arguments, except raw arguments before the
    # spec lookup. BEFORE_PRESENTATION: the call exactly as the model sent it.
    name: str
    arguments: dict[str, Any]
    # None only for policies that run before the spec lookup.
    spec: ToolSpec | None
    # BEFORE_PRESENTATION only: the registry serving this call, the model's tool-call id
    # and the caller's event bus.
    registry: Any | None = None
    call_id: str | None = None
    bus: Any | None = None


@dataclass(frozen=True)
class PolicyDenial:
    """Stops the chain. Written to the invocation exactly as the guard did inline."""

    result: Any = None
    error: str | None = None


class ToolCallPolicy(Protocol):
    name: str
    stage: PolicyStage
    # Runs before argument unwrap and the spec lookup, so it can deny an unregistered name.
    before_spec_lookup: bool

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | ToolInvocation | None:
        """None allows; a PolicyDenial or a complete ToolInvocation stops the chain."""


class SkillScopePolicy:
    """Published Skill scope.

    Registered in both stages from one implementation: ``admit`` denies before the
    step card (and records the rejection), and ``execute`` repeats the pure check
    so a direct registry caller cannot reach a Skill handler outside its scope.
    """

    name = "skill_scope"
    before_spec_lookup = False

    def __init__(self, stage: PolicyStage) -> None:
        self.stage = stage

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | None:
        from bowhead_skill.runtime_scope import skill_operation_denial

        if denial := skill_operation_denial(ctx, call.name, call.arguments):
            return PolicyDenial(result=denial, error=f"ToolPermissionError: {denial['error_kind']}")
        return None

    def on_denial(self, raw_name: str, raw_arguments: dict[str, Any], denial: PolicyDenial, ctx: ToolExecutionContext) -> None:
        """Record the rejection as retained ordinary-Lead material, keyed by the call as the model sent it."""
        from bowhead_core.ordinary_lead_material import note_known_rejection

        note_known_rejection(ctx, raw_name, raw_arguments, denial.result, error=denial.error)


class ApprovalPolicy:
    """Write Tools that need admin approval are queued instead of run.

    The gate returns its own invocation (``approval_pending`` / ``approval_unsupported``);
    a gate failure becomes ``approval_gate_failed`` without running the Tool. Contexts
    without an ``approval_gate`` (approved executions, tests, non-gateway callers) skip it.
    """

    name = "approval"
    stage = PolicyStage.BEFORE_PRESENTATION
    before_spec_lookup = False

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> ToolInvocation | None:
        import asyncio

        from bowhead_core.events import raise_if_terminal_bus_failed
        from bowhead_core.scrub import scrub
        from bowhead_core.types import ToolInvocation, utc_now_iso

        approval_gate = getattr(ctx, "approval_gate", None)
        if approval_gate is None:
            return None
        try:
            gated_inv = await approval_gate.maybe_gate(
                registry=call.registry,
                tool_name=call.name,
                arguments=call.arguments,
                ctx=ctx,
                tool_call_id=call.call_id,
                bus=call.bus,
            )
            raise_if_terminal_bus_failed(call.bus)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise_if_terminal_bus_failed(call.bus)
            logger.warning(
                "approval_gate.failed tool=%s turn=%s err=%s",
                call.name,
                ctx.turn_id,
                exc,
            )
            gated_inv = ToolInvocation(
                tool_name=call.name,
                arguments={},
                error=scrub(f"approval_gate_failed: {type(exc).__name__}: {exc}"),
                started_at=utc_now_iso(),
                ended_at=utc_now_iso(),
            )
        return gated_inv


class ReferenceSkillContextPolicy:
    name = "reference_skill_context"
    stage = PolicyStage.BEFORE_DISPATCH
    before_spec_lookup = True

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | None:
        from bowhead_skill.runtime_scope import reference_skill_context_denial

        if denial := await reference_skill_context_denial(ctx, call.name):
            return PolicyDenial(result=denial, error="ToolPermissionError: reference_skill_unavailable")
        return None


class DocumentLatchPolicy:
    name = "document_latch"
    stage = PolicyStage.BEFORE_DISPATCH
    before_spec_lookup = False

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | None:
        if ctx.document_turn_state is not None and (denial := ctx.document_turn_state.denial(call.name, call.arguments)) is not None:
            if denial["document_terminal"]:
                from bowhead_core.documents.turn_policy import terminal_message

                denial["message"] = terminal_message(ctx, accepted=False)
            return PolicyDenial(result=denial)
        return None


class RoleAclPolicy:
    """M158 paradigm v2: role-based tool whitelist; ctx.role=None skips enforcement."""

    name = "role_acl"
    stage = PolicyStage.BEFORE_DISPATCH
    before_spec_lookup = False

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | None:
        from bowhead_core.tool_registry import _opened_to_role

        assert call.spec is not None
        if ctx.role is None or ctx.role in call.spec.allowed_for_roles or _opened_to_role(call.name, ctx.role):
            return None
        allowed_list = sorted(call.spec.allowed_for_roles)
        err_msg = (
            f"role={ctx.role!r} cannot call tool={call.name!r}, allowed_roles={allowed_list}. hint: use 'subagent_spawn' to delegate to analyst."
        )
        logger.warning(
            "tool_registry.role_denied tool=%s role=%s allowed=%s",
            call.name,
            ctx.role,
            allowed_list,
        )
        # M158-15 phase 3 metric: rejected lead BI tool calls (expected 0: Lead obeys)
        try:
            from bowhead_core import metrics as _m

            _m.inc(
                "bowhead_lead_bi_attempts_total",
                labels={"role": ctx.role or "none", "tool": call.name[:32]},
                help="paradigm v2: Lead role 调 BI tool 被拒次数 (期望 0)",
            )
        except Exception:  # noqa: S110
            pass
        return PolicyDenial(error=f"ToolPermissionError: {err_msg}")


class ReferenceSkillOperationPolicy:
    name = "reference_skill_operation"
    stage = PolicyStage.BEFORE_DISPATCH
    before_spec_lookup = False

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | None:
        from bowhead_skill.runtime_scope import reference_skill_operation_denial

        if denial := await reference_skill_operation_denial(ctx, call.name, call.arguments):
            return PolicyDenial(result=denial, error="ToolPermissionError: reference_skill_unavailable")
        return None


class ResourceGrantsPolicy:
    name = "resource_grants"
    stage = PolicyStage.BEFORE_DISPATCH
    before_spec_lookup = False

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | None:
        from bowhead_core.tool_registry import _resource_grant_targets_for_tool

        name, arguments = call.name, call.arguments
        grant_targets = _resource_grant_targets_for_tool(name, arguments)
        access_store = getattr(ctx, "access_control_store", None)
        access_ctx = getattr(ctx, "access_context", None)
        if grant_targets and access_store is not None and not access_ctx:
            try:
                from bowhead_core.access_control import resource_grants_enforce_enabled

                if resource_grants_enforce_enabled():
                    return PolicyDenial(error=f"ToolPermissionError: resource grant denied tool={name!r} reason=missing_access_context")
            except Exception as exc:
                logger.warning(
                    "resource_grant.tool_missing_context_check_failed tool=%s err=%s",
                    name,
                    exc,
                )
        if grant_targets and access_store is not None and access_ctx:
            enforce_grants = False
            try:
                from bowhead_core.access_control import resource_grants_enforce_enabled

                enforce_grants = resource_grants_enforce_enabled()
                from bowhead_core.grant_evaluation import GrantVerdict, iter_grant_verdicts

                denied: list[GrantVerdict] = []
                # skill-team-governance P1a: skill keys follow actual resolved origin (team origin →
                # team_skill+id key, aligned with content-sharing, audit #11). Save tools always write personal packages;
                # a same-named team package must not redirect authorization checks to the team package.
                async for verdict in iter_grant_verdicts(
                    ctx,
                    access_store,
                    access_ctx,
                    grant_targets,
                    rekey_skill_targets=name != "skill_save_generated",
                ):
                    if verdict.allowed:
                        continue
                    if verdict.owner_exempt:
                        # Narrow owner exemption (tech-design P1a): only not_in_allowlist lets the author through;
                        # creator_only named sharing creates an allowlist that would reject an author lacking a grant; explicit deny still applies to authors.
                        logger.info(
                            "resource_grant.team_skill_owner_exempt tool=%s resource=%s",
                            name,
                            verdict.resource_id,
                        )
                        continue
                    denied.append(verdict)
                    logger.info(
                        "resource_grant.tool_would_deny tool=%s resource=%s:%s permission=%s reason=%s enforce=%s",
                        name,
                        verdict.resource_kind,
                        verdict.resource_id,
                        verdict.permission,
                        verdict.reason,
                        enforce_grants,
                    )
                    await access_store.record_runtime_denied(
                        access_ctx,
                        resource_kind=verdict.resource_kind,
                        resource_id=verdict.resource_id,
                        permission=verdict.permission,
                        reason=str(verdict.reason or "denied"),
                        target_kind="tool",
                        target_id=name,
                        payload={"tool_name": name, "enforce": enforce_grants},
                    )
                if denied and enforce_grants:
                    first = denied[0]
                    return PolicyDenial(
                        error=(
                            "ToolPermissionError: resource grant denied "
                            f"tool={name!r} resource={first.resource_kind}:{first.resource_id} "
                            f"permission={first.permission} reason={first.reason}"
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "resource_grant.tool_check_failed tool=%s err=%s",
                    name,
                    exc,
                )
                if enforce_grants:
                    return PolicyDenial(error=f"ToolPermissionError: resource grant check failed tool={name!r} reason=access_check_error")
        return None


class DatarcCapabilityPolicy:
    """A turn without verified Datarc access must not execute its BI tools.

    Reads the field directly (like the role gate reads ctx.role) rather than calling
    ctx.has_datarc(), so the minimal ctx stubs used in tests work too.
    """

    name = "datarc_capability"
    stage = PolicyStage.BEFORE_DISPATCH
    before_spec_lookup = False

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | None:
        from bowhead_core import tool_registry as _tr

        reason = getattr(ctx, "datarc_absence_reason", "")
        if not (
            _tr._tool_requires_datarc(call.name) and (getattr(ctx, "datarc", None) is None or reason in {"connection_rejected", "member_unavailable"})
        ):
            return None
        err_msg = (
            f"tool={call.name!r} needs a BI data source (datarc), but this workspace has none "
            f"connected. hint: answer from the knowledge base (kb_search / "
            f"kb_query_records), or tell the user to add a connection in "
            f"Settings → Data Connections."
        )
        if reason == "connection_rejected":
            err_msg = (
                f"tool={call.name!r}: Datarc rejected the team's connection credentials (HTTP 401/403). "
                "Ask a team admin to check whether the Datarc key expired or was revoked and update "
                "it in Team settings → Tool Sources. This affects only Datarc; continue unrelated work."
            )
        elif reason == "member_unavailable":
            err_msg = (
                f"tool={call.name!r}: the caller is not a member of the selected Datarc workspace. "
                "Ask a Datarc admin to add the caller to that workspace. Continue unrelated work."
            )
        elif reason == "transient":
            err_msg = (
                f"tool={call.name!r}: Datarc authorization could not be verified because its connection "
                "or member lookup failed. Do NOT claim the caller lacks permission. Retry the Datarc "
                "operation later; continue unrelated work."
            )
        elif reason == "no_identity":
            err_msg = (
                f"tool={call.name!r}: a verified identity is required for Datarc access. "
                "Check the caller's verified sign-in email and Datarc account. Continue unrelated work."
            )
        logger.info("tool_registry.datarc_unavailable tool=%s", call.name)
        return PolicyDenial(error=f"DatarcUnavailableError: {err_msg}")


class ConnectorBindingPolicy:
    """S1 (datarc-as-connector): a bound client whose workspace maps to no connector project.

    Ctx access and wording are table-driven via ConnectorContextBinding; datarc values remain
    byte-identical (golden-string pins). A binding miss (pbi/None) skips the policy.
    """

    name = "connector_binding"
    stage = PolicyStage.BEFORE_DISPATCH
    before_spec_lookup = False

    async def check(self, call: ToolCallRequest, ctx: ToolExecutionContext) -> PolicyDenial | None:
        from bowhead_core import tool_registry as _tr

        name = call.name
        _ctx_binding = _tr.CONNECTOR_CONTEXT_BINDINGS.get(_tr.connector_for_tool(name) or "")
        if _ctx_binding is None:
            return None
        _resolve_proj = getattr(ctx, _ctx_binding.resolve_attr, None)
        if not (callable(_resolve_proj) and not _resolve_proj()):
            return None
        # Batch 2 (PRD §3.3b): per-user turns carry an accessible-set snapshot (>1 = user has
        # choices but has not selected) → return a shape-2 clarification sentinel rather than an error;
        # Lead loop's is_clarification_sentinel recognizes this shape and server-emits
        # `clarification_required` (does not rely on the LLM asking proactively; avoids qwen act-first).
        # Empty snapshot (legacy mode / absence-matrix cases) → retain the original bare unavailable error.
        _accessible = tuple(getattr(ctx, _ctx_binding.accessible_attr, ()) or ())
        if len(_accessible) > 1:
            sentinel = {
                "ok": False,
                "error_kind": "needs_clarification",
                "clarification": {
                    "kind": "tool_context_select",
                    "tool": _ctx_binding.connector,
                    "prompt": (f"你在{_ctx_binding.zh_display_name}有 {len(_accessible)} 个可访问的工作区，这次查询用哪个？"),
                    "options": [
                        {"value": str(w.get("id") or ""), "label": str(w.get("name") or w.get("id") or "")}
                        for w in _accessible
                        if isinstance(w, dict) and w.get("id")
                    ],
                    "remember_option": True,
                },
                "rows": [],
            }
            logger.info(
                "tool_registry.%s_workspace_select_sentinel tool=%s n=%d",
                _ctx_binding.connector,
                name,
                len(_accessible),
            )
            try:
                from bowhead_core import metrics as _m

                _m.inc("bowhead_tool_context_sentinel_total", help="批2 §4.4: 工作区选择 sentinel 弹卡次数")
            except Exception:  # noqa: S110
                pass
            return PolicyDenial(result=sentinel)
        # bi-absent-turn R3.2: route N<=1 bare errors by absence reason (LLM-facing English).
        # no_identity / zero_workspaces = user permission issue; explicitly tell the LLM not to blame
        # space configuration (previously always "workspace isn't linked", making Lead's closing
        # wording direct users to configure a data source already configured by the admin); transient = datarc
        # instability; never describe it as missing permission (which creates erroneous tickets). "" / others = original wording and bytes.
        _absence = str(getattr(ctx, "datarc_absence_reason", "") or "")
        if _absence == "no_identity":
            err_msg = (
                f"tool={name!r} needs a {_ctx_binding.connector} workspace, but the CALLER has no "
                f"{_ctx_binding.connector} account. This is a USER-permission issue, NOT a workspace "
                f"configuration issue — do NOT suggest configuring or connecting a data source. "
                f"Tell the user to ask an admin to create their {_ctx_binding.display_name} account "
                f"first. Meanwhile, answer from the knowledge base (kb_search / kb_query_records) "
                f"if possible."
            )
        elif _absence == "zero_workspaces":
            err_msg = (
                f"tool={name!r} needs a {_ctx_binding.connector} workspace, but the caller's "
                f"{_ctx_binding.connector} account has NO workspace grants. This is a "
                f"USER-permission issue, NOT a workspace configuration issue — do NOT suggest "
                f"configuring or connecting a data source. Tell the user to ask an admin to grant "
                f"them access to the relevant {_ctx_binding.display_name} workspace. Meanwhile, "
                f"answer from the knowledge base (kb_search / kb_query_records) if possible."
            )
        elif _absence == "transient":
            err_msg = (
                f"tool={name!r} needs a {_ctx_binding.connector} workspace, but the caller's "
                f"{_ctx_binding.connector} authorization could not be read right now (transient "
                f"upstream failure). Do NOT claim the user lacks permission. Suggest retrying "
                f"later, or answer from the knowledge base (kb_search / kb_query_records)."
            )
        else:
            err_msg = (
                f"tool={name!r} needs a {_ctx_binding.connector} project, but this workspace isn't linked to "
                f"one. hint: answer from the knowledge base (kb_search / kb_query_records), "
                f"or select a {_ctx_binding.connector} project for this workspace."
            )
        logger.info(
            "tool_registry.%s_no_project tool=%s absence=%s",
            _ctx_binding.connector,
            name,
            _absence or "-",
        )
        return PolicyDenial(error=f"{_ctx_binding.unavailable_label}: {err_msg}")


TOOL_CALL_POLICIES: tuple[ToolCallPolicy, ...] = (
    SkillScopePolicy(PolicyStage.BEFORE_PRESENTATION),
    ApprovalPolicy(),
    ReferenceSkillContextPolicy(),
    DocumentLatchPolicy(),
    RoleAclPolicy(),
    SkillScopePolicy(PolicyStage.BEFORE_DISPATCH),
    ReferenceSkillOperationPolicy(),
    ResourceGrantsPolicy(),
    DatarcCapabilityPolicy(),
    ConnectorBindingPolicy(),
)
