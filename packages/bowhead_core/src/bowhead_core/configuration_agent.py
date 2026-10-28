"""Independent system settings execution with caller-bound domain Tools."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from bowhead_router.types import ToolSchema

from bowhead_core.config_copilot import POLICY_GENERATED, FormRegistration, LiteApplyError, get_form
from bowhead_core.configuration_contracts import DOMAINS, domain_enabled
from bowhead_core.configuration_settings_target import validate_settings_target
from bowhead_core.events import make_event
from bowhead_core.language_context import german_ai_output_enabled
from bowhead_core.space_instructions import MAX_CONTENT_CHARS
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_NONE, TOOL_EFFECT_READ_ONLY
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

AGENT_NAME = "configuration-agent"
TOOL_SUBMIT = "configuration_submit"
FORM_SKILLS = {name: domain.skill for name, domain in DOMAINS.items()}
SUPPORTED_FORMS: Final = frozenset(FORM_SKILLS)
# Forms bound to the current Space: they need a Space in the target and their proposals stay on it.
SPACE_FORMS: Final = frozenset({"workspace_i18n", "workspace_metadata", "space_instructions"})
_EVENT_CANVAS_OPEN: Final = "configuration.canvas_open"
_BINDING_ATTR = "_configuration_agent_binding"
AccessResolver = Callable[[ToolExecutionContext, str, bool], Awaitable[dict[str, Any]]]


def configuration_form_fields(form_id: str) -> frozenset[str]:
    """Use the admitted domain's registered field policy, never the whole settings store."""
    reg = get_form(form_id) if form_id in SUPPORTED_FORMS else None
    if reg is None:
        return frozenset()
    return frozenset(name for name, policy in reg.field_policy.items() if policy == POLICY_GENERATED and name not in reg.secret_value_fields)


def _read_schema(reg: FormRegistration, fields: dict[str, Any], choices: dict[str, list[Any]]) -> dict[str, Any]:
    """Project trusted field documentation onto this authorized read, including only reachable definitions."""
    source = reg.schema.model_json_schema()
    properties = {name: copy.deepcopy(prop) for name, prop in source.get("properties", {}).items() if name in fields}
    for name, values in choices.items():
        if name not in properties:
            continue
        prop = properties[name]
        for branch in [prop, *prop.get("anyOf", [])]:
            if "enum" in branch:
                branch["enum"] = [value for value in branch["enum"] if value is None or value in values]
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    required = [name for name in source.get("required", []) if name in properties]
    if required:
        schema["required"] = required
    definitions = source.get("$defs", {})
    selected: dict[str, Any] = {}

    def include_references(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in selected and name in definitions:
                    selected[name] = copy.deepcopy(definitions[name])
                    include_references(selected[name])
            for value in node.values():
                include_references(value)
        elif isinstance(node, list):
            for value in node:
                include_references(value)

    include_references(properties)
    if selected:
        schema["$defs"] = selected
    return schema


def _field_constraints(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep the existing compact constraint receipt sourced from the same JSON Schema."""
    keys = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern", "minItems", "maxItems"}
    constraints = {}
    for name, prop in schema["properties"].items():
        limits = {key: value for branch in [prop, *prop.get("anyOf", [])] for key, value in branch.items() if key in keys}
        if limits:
            constraints[name] = limits
    return constraints


def configuration_agent_in_roster(ctx: object) -> bool:
    """Whether Lead may name and delegate to the specialist in this Turn.

    The actor-scoped roster frozen on the Turn context is the only source of
    Agent names (WP M3-12). An unknown roster counts as absent, so copy and
    refusals never advertise an Agent that cannot be spawned.
    """
    peers = getattr(ctx, "_actor_scoped_prompt_peers", None)
    return isinstance(peers, tuple) and any(getattr(peer, "name", None) == AGENT_NAME for peer in peers)


@dataclass
class ConfigurationAgentBinding:
    form_id: str
    target: tuple[str, ...]
    intent_quote: str
    baselines: dict[str, dict[str, Any]] = field(default_factory=dict)
    proposals: set[str] = field(default_factory=set)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    message: str = ""
    read_skills: set[str] = field(default_factory=set)


def form_plugin_disabled(ctx: object, form_id: str) -> bool:
    """Whether the settings Plugin behind ``form_id`` is turned off for this caller (team or personal).

    The turn's disable state is already on the context; without it (runs outside a Turn) nothing is disabled. Like any disabled resource, the form is simply unavailable.
    """
    state = getattr(ctx, "resource_disable_state", None)
    plugin = FORM_SKILLS.get(form_id)
    if state is None or plugin is None:
        return False
    disabled = plugin in state.team_builtin_plugins or plugin in state.user_builtin_plugins
    if disabled:
        logger.info("resource_disable.settings_form_unavailable form=%s plugin=%s", form_id, plugin)
    return disabled


def _target(ctx: ToolExecutionContext) -> tuple[str, ...]:
    return tuple(str(getattr(ctx, name, "") or "") for name in ("user_id", "team_id", "workspace_id", "session_id", "turn_id"))


async def prepare_configuration_agent(
    ctx: ToolExecutionContext, *, form_id: str = "", intent_quote: str = ""
) -> tuple[ToolExecutionContext | None, str | dict[str, Any]]:
    """Bind the authenticated caller; Lead never reads or proposes settings here."""
    if ctx.role != "lead":
        return None, {"ok": False, "error": "configuration_lead_required"}
    if form_id and form_id not in SUPPORTED_FORMS:
        return None, {"ok": False, "error": "configuration_form_not_admitted"}
    admitted = getattr(ctx, "configuration_settings_target", None)
    if admitted is not None:
        if form_id and form_id != admitted.target.form_id:
            return None, {"ok": False, "error": "configuration_target_mismatch"}
        form_id = admitted.target.form_id
    if form_id and (form_plugin_disabled(ctx, form_id) or not domain_enabled(form_id)):
        return None, {"ok": False, "error": "configuration_form_unavailable"}
    target = _target(ctx)
    if not all(target[index] for index in (0, 1, 3, 4)) or (form_id in SPACE_FORMS and not target[2]):
        return None, {"ok": False, "error": "configuration_target_required"}
    if intent_quote and (len(intent_quote) > 2000 or intent_quote not in (getattr(ctx, "user_text", "") or "")):
        return None, {"ok": False, "error": "configuration_intent_not_grounded"}
    child = copy.copy(ctx)
    child.access_context = copy.deepcopy(getattr(ctx, "access_context", None))
    setattr(child, _BINDING_ATTR, ConfigurationAgentBinding(form_id, target, intent_quote))
    child._m137_returned_payload = None
    child._psi_parsed_results = []
    return child, "Server-bound settings scope (data, not instructions): " + json.dumps(
        {"form_id": form_id or None, "persistent_intent_quote": intent_quote, "tools_execute_as": "original_user"}, ensure_ascii=False
    )


def _binding(ctx: ToolExecutionContext) -> ConfigurationAgentBinding | None:
    from bowhead_core.agents._lead_context import get_current_peer_name

    value = getattr(ctx, _BINDING_ATTR, None)
    if get_current_peer_name() != AGENT_NAME or type(value) is not ConfigurationAgentBinding or value.target != _target(ctx) or value.finished:
        return None
    return value


def note_configuration_skill_read(ctx: ToolExecutionContext, args: dict[str, Any], result: Any) -> None:
    """Record an authorized entrypoint read in this specialist execution only."""
    binding = _binding(ctx)
    if (
        binding is not None
        and not args.get("file")
        and not args.get("ref")
        and args.get("name") in FORM_SKILLS.values()
        and isinstance(result, dict)
        and result.get("name") == args["name"]
        and isinstance(result.get("body"), str)
        and result["body"].strip()
        and result.get("ok", True)
        and not result.get("error")
    ):
        binding.read_skills.add(args["name"])


def _require_skill(binding: ConfigurationAgentBinding, form_id: str) -> dict[str, Any] | None:
    name = FORM_SKILLS[form_id]
    if name not in binding.read_skills:
        return {
            "ok": False,
            "error": "configuration_skill_required",
            "required_tool": {"name": "skill_read", "arguments": {"name": name}},
            "message": "Read this domain Skill, then continue from the existing receipts. Do not repeat successful reads, opening or proposals.",
        }
    return None


async def _read(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    from bowhead_core.config_copilot_chat_tools import _allowed_values, _proposable, _read_handler

    binding = _binding(ctx)
    form_id = args["form_id"]
    result = await _read_handler(args, ctx)
    if not result.get("ok"):
        return result
    binding.baselines[form_id] = copy.deepcopy(result["data"])
    reg = get_form(form_id)
    editable = bool(ctx.config_lite_supported and _proposable(reg, ctx.access_context) and not ctx.access_context.get("configuration_readonly"))
    if form_id == "user_language" and not binding.intent_quote:
        editable = False
    if form_id == "space_instructions" and not ctx.access_context.get("space_instructions_editable"):
        # Members read the instructions; only Space admins and Team managers of an active Space change them.
        editable = False
    admitted_fields = configuration_form_fields(form_id)
    fields = {key: copy.deepcopy(value) for key, value in result["data"].get("fields", {}).items() if key in admitted_fields}
    if form_id == "space_instructions" and "content" in fields:
        # The sanitized read caps text at 2000 characters and escapes phrases; a full-text rewrite built on that would
        # cut or corrupt the instructions. The exact text is already in this agent's prompt as Space instructions, so
        # the specialist gets it unchanged. The baseline stays the sanitized read, compared like every other form.
        from bowhead_core.config_copilot_chat_tools import _form_context

        raw = await reg.read_accessor(_form_context(ctx))
        fields["content"] = {**fields["content"], "value": raw["fields"]["content"]["value"]}
    for value in fields.values():
        if type(value.get("unset")) is bool:
            value["value_source"] = "default" if value["unset"] else "saved"
    choices = {key: value for key, value in _allowed_values(reg).items() if key in fields}
    if form_id == "user_language" and "output_language" in choices and not german_ai_output_enabled():
        choices["output_language"] = [value for value in choices.get("output_language", []) if value != "de-DE"]
    schema = _read_schema(reg, fields, choices)
    actions = ["read"]
    if editable:
        actions += ["propose", "apply" if form_id == "user_language" else "review" if reg.review_page else "confirm"]
        if form_id in {"workspace_i18n", "workspace_metadata"}:
            actions.append("open")
    return {
        "ok": True,
        "form_id": form_id,
        "fields": fields,
        "schema": schema,
        "guidance": reg.guidance,
        "patch_semantics": (
            "Omit unchanged fields. Explicit null is a typed value and must satisfy the schema. Objects and arrays replace the supplied field; preserve unrequested nested values."
            if reg.typed_patch
            else "Omit unchanged fields. Null is ignored, not a reset. Empty text clears only fields whose guidance permits it."
        ),
        "reset": {
            "fields": sorted(reg.reset_fields),
            "all": reg.reset_all,
            "semantics": "Use reset or reset_all explicitly; never use null to request reset.",
        },
        "page": DOMAINS[form_id].page,
        "review_page": reg.review_page,
        **({"revision": result["data"][reg.lite_revision_field]} if reg.lite_revision_field else {}),
        **({"parameter_profiles": result["data"]["parameter_profiles"]} if "parameter_profiles" in result["data"] else {}),
        "field_constraints": _field_constraints(schema),
        "allowed_values": choices,
        "editable_fields": sorted(fields) if editable else [],
        "save_policy": "explicit_persistent_language_request" if form_id == "user_language" else "user_confirmation",
        "actions": actions,
        "required_skill": FORM_SKILLS[form_id],
        "target": {
            "user_id": ctx.user_id,
            "team_id": ctx.team_id,
            **({"workspace_id": ctx.workspace_id, "workspace_name": result["data"].get("workspace_name")} if form_id in SPACE_FORMS else {}),
        },
        **(
            {
                "revision": result["data"]["revision"],
            }
            if form_id == "workspace_metadata"
            else {
                "revision": result["data"]["revision"],
                "patch_contract": "content is the complete new text; keep every line the user did not ask to change exactly as read.",
                # An archived Space keeps its instructions read-only for everyone until it is restored.
                "space_archived": bool(ctx.access_context.get("space_archived")),
            }
            if form_id == "space_instructions"
            else {}
        ),
    }


async def _open(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    pages = {"workspace_i18n": "workspace_i18n", "workspace_metadata": "space_general"}
    if not ctx.config_lite_supported or args["form_id"] not in pages:
        return {"ok": False, "error": "configuration_editor_unavailable"}
    pointer = {"page": pages[args["form_id"]], "user_id": ctx.user_id, "team_id": ctx.team_id, "workspace_id": ctx.workspace_id}
    bus = getattr(ctx, "parent_bus", None)
    if bus is None or await bus.emit(make_event(_EVENT_CANVAS_OPEN, ctx.turn_id, {"turn_id": ctx.turn_id, "target": pointer})) is False:
        return {"ok": False, "error": "not_delivered"}
    return {
        "ok": True,
        "status": "open_requested",
        "target": pointer,
        "ui_status": "unacknowledged",
        "saved": False,
        "message": "The authorized Canvas open event was emitted. Browser rendering and review readiness are not acknowledged.",
    }


async def _propose(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    from bowhead_core.config_copilot_chat_tools import _allowed_values, _propose_handler

    binding = _binding(ctx)
    form_id, patch = args["form_id"], args.get("patch", {})
    if form_id not in binding.baselines:
        return {"ok": False, "error": "configuration_read_required"}
    readable_fields = set(binding.baselines[form_id].get("fields", {})) & configuration_form_fields(form_id)
    resets = args.get("reset", [])
    if not isinstance(resets, list) or any(not isinstance(key, str) for key in resets):
        return {"ok": False, "error": "invalid_configuration_reset"}
    if not isinstance(patch, dict) or (not patch and not resets and not args.get("reset_all")) or (set(patch) | set(resets)) - readable_fields:
        return {"ok": False, "error": "invalid_configuration_fields"}
    if form_id == "user_language" and not binding.intent_quote:
        return {"ok": False, "error": "persistent_intent_required"}
    choices = _allowed_values(get_form(form_id))
    if form_id == "user_language" and not german_ai_output_enabled():
        choices["output_language"] = [value for value in choices.get("output_language", []) if value != "de-DE"]
    if any(key in choices and value not in choices[key] for key, value in patch.items()):
        return {"ok": False, "error": "invalid_patch"}
    if form_id == "space_instructions":
        from bowhead_core.config_copilot_chat_tools import _echoes_sanitizer_marker

        if _echoes_sanitizer_marker(patch):
            return {"ok": False, "error": "invalid_patch", "message": "The text contains a display placeholder. Read again and copy the exact text."}
        if len(str(patch.get("content", "")).strip()) > MAX_CONTENT_CHARS:
            return {"ok": False, "error": "content_too_long", "max_chars": MAX_CONTENT_CHARS}
    if requirement := _require_skill(binding, form_id):
        return requirement
    receipt = await _propose_handler(
        args, ctx, expected_data=binding.baselines[form_id], prepare_only=True, emit_confirmation=form_id != "user_language"
    )
    receipt = {**receipt, "form_id": form_id, "saved": False}
    if receipt.get("ok"):
        binding.proposals.add(receipt["proposal_id"])
        receipt["status"] = "proposed"
    return receipt


async def _result(args: dict[str, Any], ctx: ToolExecutionContext, *, apply: bool = False) -> dict[str, Any]:
    from bowhead_core.config_copilot_chat_tools import _form_context, _proposal_writer_provider
    from bowhead_core.config_draft_store import KIND_LITE_PROPOSAL

    binding = _binding(ctx)
    store = _proposal_writer_provider() if _proposal_writer_provider else None
    if store is None:
        return {"ok": False, "error": "configuration_store_unavailable"}
    draft = await store.get_draft(args.get("proposal_id", ""), team_id=ctx.team_id, owner_user_id=ctx.user_id)
    if draft is None or draft.kind != KIND_LITE_PROPOSAL or draft.form_id != args["form_id"]:
        return {"ok": False, "error": "configuration_proposal_not_found"}
    if draft.form_id in SPACE_FORMS and draft.workspace_id != ctx.workspace_id:
        return {"ok": False, "error": "configuration_target_mismatch"}
    if apply:
        if draft.form_id != "user_language":
            return {"ok": False, "error": "user_confirmation_required", "proposal_id": draft.id, "status": draft.status}
        if not binding.intent_quote or draft.id not in binding.proposals:
            return {"ok": False, "error": "persistent_intent_required"}
        if requirement := _require_skill(binding, draft.form_id):
            return requirement
        if draft.status == "proposed":
            reg = get_form(draft.form_id)
            if reg is None or reg.lite_applier is None:
                return {"ok": False, "error": "configuration_adapter_unavailable"}
            try:
                await reg.lite_applier(_form_context(ctx), draft.payload["rows"], "apply")
            except LiteApplyError as exc:
                return {"ok": False, "error": exc.code, "saved": False}
            transition = await store.transition(
                draft.id, team_id=ctx.team_id, owner_user_id=ctx.user_id, to_status="applied", from_statuses=("proposed",)
            )
            if transition.outcome not in {"ok", "replay"}:
                return {"ok": False, "error": "configuration_receipt_conflict", "status": "unknown"}
            draft = await store.get_draft(draft.id, team_id=ctx.team_id, owner_user_id=ctx.user_id)
    receipt = {"ok": True, "proposal_id": draft.id, "form_id": draft.form_id, "status": draft.status, "saved": draft.status == "applied"}
    return receipt


async def _submit_handler(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    from bowhead_core.subagent_return_tool import CTX_PAYLOAD_ATTR, TERMINAL_FLAG

    binding = _binding(ctx)
    if binding is None:
        return {"ok": False, "error": "configuration_execution_not_admitted"}
    message = args.get("message")
    if set(args) != {"message"} or not isinstance(message, str) or not message.strip() or len(message) > 2000:
        return {
            "ok": False,
            "error": "invalid_configuration_submission",
            "expected_arguments": {"message": "A non-empty receipt-based explanation of at most 2000 characters."},
            "message": "Call configuration_submit with only message. Do not pass form_id, proposal_id, status, saved or receipts.",
        }
    forms = set(binding.baselines)
    for receipt in binding.receipts:
        if not receipt.get("ok"):
            continue
        if receipt.get("form_id") in FORM_SKILLS:
            forms.add(receipt["form_id"])
        page = receipt.get("target", {}).get("page")
        if page in {"workspace_i18n", "space_general"}:
            forms.add("workspace_metadata" if page == "space_general" else "workspace_i18n")
    for form_id in sorted(forms):
        if requirement := _require_skill(binding, form_id):
            return requirement
    binding.finished = True
    binding.message = message
    setattr(ctx, CTX_PAYLOAD_ATTR, {"delivery_surface": "chat", "exec_summary": message})
    return {"ok": True, TERMINAL_FLAG: True, "message": message, "receipts": binding.receipts}


def configuration_agent_result(child: ToolExecutionContext) -> dict[str, Any]:
    """Collect specialist receipts without a Lead settings operation."""
    binding = getattr(child, _BINDING_ATTR, None)
    if type(binding) is not ConfigurationAgentBinding:
        return {"ok": False, "error": "configuration_submission_missing"}
    receipts = copy.deepcopy(binding.receipts)
    if not binding.finished:
        return {"ok": False, "error": "configuration_submission_missing", "receipts": receipts}
    return {"ok": True, "message": binding.message, "receipts": receipts}


def _compact_receipt(receipt: dict[str, Any], form_id: str | None) -> dict[str, Any]:
    """Receipts reach Lead; long Space instructions text is replaced by its length (the specialist kept the text)."""
    if form_id != "space_instructions":
        return receipt
    content = receipt.get("fields", {}).get("content") if isinstance(receipt.get("fields"), dict) else None
    if isinstance(content, dict) and isinstance(content.get("value"), str):
        content["value"] = f"<{len(content['value'])} characters>"
    for row in receipt.get("rows") or []:
        for key in ("from", "to"):
            if isinstance(row, dict) and isinstance(row.get(key), str):
                row[key] = f"<{len(row[key])} characters>"
    return receipt


def register_configuration_agent_tools(registry: ToolRegistry, *, access_resolver: AccessResolver | None = None) -> None:

    def guarded(handler, *, write=False, record=False):
        async def execute(args, ctx):
            binding = _binding(ctx)
            form_id = args.get("form_id")
            if binding is None:
                return {"ok": False, "error": "configuration_execution_not_admitted"}
            if form_id not in SUPPORTED_FORMS or (binding.form_id and binding.form_id != form_id):
                return {"ok": False, "error": "configuration_form_not_admitted"}
            if form_plugin_disabled(ctx, form_id) or not domain_enabled(form_id):
                return {"ok": False, "error": "configuration_form_unavailable"}
            rejection = await validate_settings_target(ctx, form_id)
            if rejection is not None:
                return rejection
            if access_resolver is None:
                return {"ok": False, "error": "configuration_access_unavailable"}
            try:
                ctx.access_context = await access_resolver(ctx, form_id, write)
            except Exception:
                return {"ok": False, "error": "configuration_access_denied"}
            if write and ctx.access_context.get("configuration_readonly"):
                return {"ok": False, "error": "permission_denied"}
            try:
                return await handler(args, ctx)
            except Exception:
                return {"ok": False, "error": "configuration_operation_failed", "status": "unknown"}

        async def with_receipt(args, ctx):
            receipt = await execute(args, ctx)
            binding = _binding(ctx)
            if record and binding is not None:
                binding.receipts.append(_compact_receipt(copy.deepcopy(receipt), args.get("form_id")))
            return receipt

        return with_receipt

    async def discover(args, ctx):
        binding = _binding(ctx)
        if binding is None:
            return {"ok": False, "error": "configuration_execution_not_admitted"}
        entries = []
        for form_id, domain in DOMAINS.items():
            if (binding.form_id and binding.form_id != form_id) or not domain_enabled(form_id) or form_plugin_disabled(ctx, form_id):
                continue
            if get_form(form_id) is None:
                continue

            # Reuse the exact read admission without changing execution baselines or requiring a method read.
            async def describe(_args, scoped, form_id=form_id, domain=domain):
                from bowhead_core.config_copilot_chat_tools import _allowed_values, _proposable, _readable

                reg = get_form(form_id)
                if not _readable(reg, scoped.access_context)[0]:
                    return {"ok": False}
                fields = dict.fromkeys(configuration_form_fields(form_id))
                editable = (
                    _proposable(reg, scoped.access_context)
                    and not scoped.access_context.get("configuration_readonly")
                    and scoped.config_lite_supported
                )
                if form_id == "user_language" and not binding.intent_quote:
                    editable = False
                if form_id == "space_instructions" and not scoped.access_context.get("space_instructions_editable"):
                    editable = False
                actions = ["read"]
                if editable:
                    actions += ["propose", "apply" if form_id == "user_language" else "review" if reg.review_page else "confirm"]
                    if form_id in {"workspace_i18n", "workspace_metadata"}:
                        actions.append("open")
                return {
                    "ok": True,
                    "form_id": form_id,
                    "scope": domain.scope,
                    "page": domain.page,
                    "description": domain.description,
                    "required_skill": domain.skill,
                    "schema": _read_schema(reg, fields, _allowed_values(reg)),
                    "guidance": reg.guidance,
                    "review_page": reg.review_page,
                    "reset": {"fields": sorted(reg.reset_fields), "all": reg.reset_all},
                    "actions": actions,
                }

            result = await guarded(describe)({"form_id": form_id}, ctx)
            if result.get("ok"):
                entries.append(result)
        return {"ok": True, "forms": entries}

    form = {
        "type": "string",
        "enum": sorted(SUPPORTED_FORMS),
        "description": ("Use configuration_list to discover current caller-authorized domains and schemas. A selected original page fixes the form."),
    }
    specs = [
        (
            "configuration_list",
            "Discover available configuration domains, schemas, field descriptions, original pages and permissions for the actual caller.",
            {},
            [],
            discover,
        ),
        (
            "configuration_read",
            "Read current settings, registered JSON Schema with field descriptions and constraints, guidance, permitted values and caller permissions. "
            "Use this contract for fields and patches; values include default/saved provenance, not storage history. "
            "Do not read unrelated settings to handle an unsupported interface-language request or an unspecified language request.",
            {"form_id": form},
            ["form_id"],
            guarded(_read, record=True),
        ),
        (
            "configuration_open",
            "Request the original authorized settings editor in Canvas. Event delivery does not acknowledge browser rendering or save settings.",
            {"form_id": form},
            ["form_id"],
            guarded(_open, write=True, record=True),
        ),
        (
            "configuration_propose",
            "Persist a sparse suggestion for review against the previous read. The suggestion is saved; canonical settings remain unchanged until confirmation.",
            {
                "form_id": form,
                "patch": {"type": "object", "additionalProperties": True},
                "reset": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "reset_all": {"type": "boolean"},
            },
            ["form_id"],
            guarded(_propose, write=True, record=True),
        ),
        (
            "configuration_apply",
            "Save an explicitly requested persistent language preference. Other domains require the user's confirmation.",
            {"form_id": form, "proposal_id": {"type": "string"}},
            ["form_id", "proposal_id"],
            guarded(lambda args, ctx: _result(args, ctx, apply=True), write=True, record=True),
        ),
        (
            "configuration_result",
            "Read the caller-owned proposal's actual status. Proposed does not mean saved.",
            {"form_id": form, "proposal_id": {"type": "string"}},
            ["form_id", "proposal_id"],
            guarded(_result, record=True),
        ),
        (
            TOOL_SUBMIT,
            "Mandatory final delivery for every outcome, including read-only, errors and suggestions awaiting user confirmation. "
            "Call immediately when the requested work is done; plain assistant text is not delivery. "
            "Pass only message, grounded in actual Tool receipts. Label current values default or saved without inferring past choices. "
            "For a pending suggestion, say it is saved for review and settings have not changed. "
            "For a clear unsupported request, state the limit briefly without offering other settings or follow-up work. "
            "For unspecified language, ask which setting before reading or suggesting. This does not save settings or confirm a suggestion.",
            {"message": {"type": "string", "minLength": 1, "maxLength": 2000}},
            ["message"],
            _submit_handler,
        ),
    ]
    for name, description, properties, required, handler in specs:
        registry.register(
            ToolSpec(
                schema=ToolSchema(
                    name=name,
                    description=description,
                    input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
                ),
                handler=handler,
                allowed_for_roles=frozenset({"analyst"}),
                needs_approval=False,
                risk_level="low",
                effect_spec=TOOL_EFFECT_READ_ONLY
                if name in {"configuration_list", "configuration_read", "configuration_result"}
                else TOOL_EFFECT_NONE
                if name in {"configuration_open", TOOL_SUBMIT}
                else None,
            )
        )
