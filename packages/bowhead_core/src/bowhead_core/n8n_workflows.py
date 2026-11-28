"""Approved n8n workflows as derived team tools (``FEATURE_N8N_WORKFLOW_TOOLS``).

A team MCP source that points at n8n's instance-level MCP server stays an ordinary
``kind="mcp"`` source. An owner or administrator approves single workflows; each approval
becomes one derived tool, ``teammcp.<source>.n8n_wf_<workflowId>``, whose handler runs the
published version and waits for the result (``n8n_workflow_run``). The raw n8n tools keep
their own selection. See ``docs/sprints/n8n-workflow-integration/tech-design.md`` D1 and D3.

Approvals are stored per source in ``team_settings`` under ``n8n_workflows:<source_id>`` as
``{workflow_id: record}``. Only the approval routes write them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from bowhead_core._db import open_pooled_read_ctx, open_shared_ctx
from bowhead_core.openapi_tool_source import TeamToolEntry, op_tool_name
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.team_settings_store import TeamSettingsStore

logger = logging.getLogger(__name__)

KEY_PREFIX: Final = "n8n_workflows:"
N8N_WORKFLOWS_SKILL: Final = "n8n-workflows"
N8N_WORKFLOWS_TEMPLATE: Final = "n8n_workflows"
MAX_APPROVED_PER_SOURCE: Final = 64
MAX_SCHEMA_BYTES: Final = 16 * 1024
MAX_DESCRIPTION_CHARS: Final = 1000
MAX_NAME_CHARS: Final = 120
CATEGORIES: Final = ("read", "external_write", "destructive")
N8N_MCP_PATH: Final = "/mcp-server/http"
N8N_RAW_TOOLS: Final = ("search_workflows", "get_workflow_details", "execute_workflow", "get_workflow_execution")
WEBHOOK_TRIGGER: Final = "n8n-nodes-base.webhook"
SCHEDULE_TRIGGER: Final = "n8n-nodes-base.scheduleTrigger"
SUPPORTED_TRIGGERS: Final = (WEBHOOK_TRIGGER, SCHEDULE_TRIGGER)
_TOOL_PREFIX: Final = "n8n_wf_"
_WORKFLOW_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Static scan of n8n expressions for webhook body fields: $json.body.month, $json.body?.month,
# $json.body["month"], $('Webhook').item.json.body.month. Matches code, not natural language.
_BODY_FIELD = re.compile(r"""\.body\??\.\s*([A-Za-z_][A-Za-z0-9_]{0,63})|\.body\??\.?\[\s*["']([^"'\]\\]{1,64})["']\s*\]""")
_MAX_DRAFT_FIELDS: Final = 32
_HINTS: Final = {"read": (True, False), "external_write": (False, False), "destructive": (False, True)}


@dataclass(frozen=True)
class NotApprovable:
    """Why a workflow cannot be approved: a stable ``code`` for the interface, an English ``message``."""

    code: str
    message: str
    detail: str = ""


def n8n_workflow_tools_enabled() -> bool:
    """Read on every call so a deployment change applies to the next Turn."""
    return os.environ.get("FEATURE_N8N_WORKFLOW_TOOLS", "0").strip().lower() in {"1", "true", "yes"}


def is_n8n_source(kind: str, base_url: str, tools_schema: Iterable[Mapping[str, Any]] | None) -> bool:
    """Whether a team source is n8n's instance-level MCP server (URL path and cached tools)."""
    if kind != "mcp" or not base_url:
        return False
    if urlsplit(base_url).path.rstrip("/") != N8N_MCP_PATH:
        return False
    names = {str(tool.get("name") or "") for tool in tools_schema or [] if isinstance(tool, Mapping)}
    return all(name in names for name in N8N_RAW_TOOLS)


def n8n_instance_origin(base_url: str) -> str:
    """The n8n editor origin for execution links (the MCP URL without its path)."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def valid_workflow_id(workflow_id: object) -> bool:
    return isinstance(workflow_id, str) and bool(_WORKFLOW_ID.match(workflow_id))


def n8n_tool_operation(workflow_id: str) -> str:
    return f"{_TOOL_PREFIX}{workflow_id}"


def n8n_tool_ref(source_id: str, workflow_id: str) -> str:
    return op_tool_name(source_id, n8n_tool_operation(workflow_id))


def workflow_id_from_operation(operation_id: str) -> str | None:
    if not operation_id.startswith(_TOOL_PREFIX):
        return None
    workflow_id = operation_id[len(_TOOL_PREFIX) :]
    return workflow_id if valid_workflow_id(workflow_id) else None


@dataclass(frozen=True)
class N8nWorkflowApproval:
    """One administrator-approved workflow. ``digest`` binds every field a run relies on."""

    workflow_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    category: str
    trigger_node: str
    trigger_type: str
    approved_version_id: str
    approved_by: str = ""
    approved_at: str = ""

    @property
    def digest(self) -> str:
        return approval_digest(
            workflow_id=self.workflow_id,
            description=self.description,
            input_schema=self.input_schema,
            category=self.category,
            trigger_node=self.trigger_node,
            trigger_type=self.trigger_type,
            approved_version_id=self.approved_version_id,
        )

    @property
    def takes_body(self) -> bool:
        return self.trigger_type == WEBHOOK_TRIGGER

    def to_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "category": self.category,
            "trigger": {"node": self.trigger_node, "type": self.trigger_type},
            "approved_version_id": self.approved_version_id,
            "digest": self.digest,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


def approval_digest(
    *,
    workflow_id: str,
    description: str,
    input_schema: Mapping[str, Any],
    category: str,
    trigger_node: str,
    trigger_type: str,
    approved_version_id: str,
) -> str:
    blob = json.dumps(
        {
            "workflow_id": workflow_id,
            "description": description,
            "input_schema": input_schema,
            "category": category,
            "trigger": [trigger_node, trigger_type],
            "approved_version_id": approved_version_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def schema_problem(schema: object, *, takes_body: bool) -> NotApprovable | None:
    """Why ``schema`` cannot be a derived tool's input schema, or None.

    A webhook workflow's schema describes the request ``body``; a schedule workflow takes none.
    """
    if not isinstance(schema, dict):
        return NotApprovable("schema_invalid", "The input schema must be a JSON object.")
    try:
        size = len(json.dumps(schema, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return NotApprovable("schema_invalid", "The input schema must be plain JSON.")
    if size > MAX_SCHEMA_BYTES:
        return NotApprovable("schema_too_large", f"The input schema is larger than {MAX_SCHEMA_BYTES // 1024} KiB.")
    if schema.get("type") != "object":
        return NotApprovable("schema_not_object", 'The input schema must have "type": "object".')
    if not takes_body and schema.get("properties"):
        return NotApprovable(
            "schedule_takes_no_input", "A workflow started by a schedule trigger takes no input; leave the schema without properties."
        )
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001 — any schema error is reported, never raised
        message = getattr(exc, "message", "") or type(exc).__name__
        return NotApprovable("schema_invalid", "The input schema is not valid JSON Schema.", str(message)[:200])
    return None


def empty_input_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def draft_input_schema(nodes: Iterable[Mapping[str, Any]], *, takes_body: bool) -> dict[str, Any]:
    """A draft schema from the body fields the published nodes read. Every field is optional and untyped."""
    if not takes_body:
        return empty_input_schema()
    fields: list[str] = []
    for node in nodes:
        parameters = node.get("parameters") if isinstance(node, Mapping) else None
        if parameters is None:
            continue
        try:
            text = json.dumps(parameters, ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        for match in _BODY_FIELD.finditer(text):
            name = match.group(1) or match.group(2)
            if name and name not in fields:
                fields.append(name)
            if len(fields) >= _MAX_DRAFT_FIELDS:
                break
    return {"type": "object", "properties": {name: {} for name in fields}, "additionalProperties": True}


def published_nodes(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The published graph's nodes from ``get_workflow_details(detailLevel="full")``'s workflow."""
    active = workflow.get("activeVersion")
    if isinstance(active, Mapping) and active.get("sameAsDraft") is False:
        nodes = active.get("nodes")
    else:
        nodes = workflow.get("nodes")
    return [node for node in nodes or [] if isinstance(node, Mapping)]


def published_trigger(nodes: Iterable[Mapping[str, Any]]) -> tuple[str, str] | NotApprovable:
    """``(node name, node type)`` of the single runnable trigger, or why there is none."""
    enabled = [node for node in nodes if node.get("disabled") is not True]
    supported = [node for node in enabled if node.get("type") in SUPPORTED_TRIGGERS]
    if not supported:
        return NotApprovable(
            "no_supported_trigger", "The published version has no webhook or schedule trigger. Chat, form and other triggers are not supported yet."
        )
    if len(supported) > 1:
        return NotApprovable("several_triggers", "The published version has more than one webhook or schedule trigger; keep one.")
    node = supported[0]
    name = node.get("name")
    if not isinstance(name, str) or not name:
        return NotApprovable("no_supported_trigger", "The trigger node has no name.")
    return name, str(node.get("type"))


def runnable_trigger(workflow_id: str, workflow: Mapping[str, Any]) -> tuple[str, str] | NotApprovable:
    """The published trigger Bowhead would run, or why this workflow cannot be run from Bowhead."""
    if str(workflow.get("id") or "") != workflow_id:
        return NotApprovable("different_workflow", "n8n returned a different workflow.")
    if workflow.get("isArchived") is True:
        return NotApprovable("archived", "The workflow is archived in n8n.")
    version = workflow.get("activeVersionId")
    if workflow.get("active") is not True or not isinstance(version, str) or not version:
        return NotApprovable("not_published", "The workflow has no published version in n8n. Publish it first.")
    if workflow.get("canExecute") is not True:
        return NotApprovable("cannot_execute", "The n8n account this source signed in with cannot execute this workflow.")
    return published_trigger(published_nodes(workflow))


def build_approval(
    *,
    workflow_id: str,
    workflow: Mapping[str, Any],
    description: str,
    input_schema: dict[str, Any],
    category: str,
    actor_user_id: str,
    now: str | None = None,
) -> N8nWorkflowApproval | NotApprovable:
    """An approval from fresh ``get_workflow_details(full)`` output, or why it cannot be approved."""
    if category not in CATEGORIES:
        return NotApprovable("category_invalid", "Choose read, external write or destructive.")
    trigger = runnable_trigger(workflow_id, workflow)
    if isinstance(trigger, NotApprovable):
        return trigger
    node_name, node_type = trigger
    version = str(workflow.get("activeVersionId"))
    problem = schema_problem(input_schema, takes_body=node_type == WEBHOOK_TRIGGER)
    if problem:
        return problem
    text = (description or "").strip()[:MAX_DESCRIPTION_CHARS]
    if not text:
        return NotApprovable("description_missing", "Add a description that tells the model what the workflow does and returns.")
    name = str(workflow.get("name") or workflow_id).strip()[:MAX_NAME_CHARS] or workflow_id
    return N8nWorkflowApproval(
        workflow_id=workflow_id,
        name=name,
        description=text,
        input_schema=input_schema,
        category=category,
        trigger_node=node_name,
        trigger_type=node_type,
        approved_version_id=version,
        approved_by=actor_user_id,
        approved_at=now or datetime.now(tz=UTC).isoformat(),
    )


def approval_from_record(workflow_id: str, record: object) -> N8nWorkflowApproval | None:
    """A stored record, or None when it is malformed or its digest does not match its fields."""
    if not valid_workflow_id(workflow_id) or not isinstance(record, Mapping):
        return None
    trigger = record.get("trigger")
    schema = record.get("input_schema")
    if not isinstance(trigger, Mapping) or not isinstance(schema, dict):
        return None
    fields = {
        "name": record.get("name"),
        "description": record.get("description"),
        "category": record.get("category"),
        "trigger_node": trigger.get("node"),
        "trigger_type": trigger.get("type"),
        "approved_version_id": record.get("approved_version_id"),
    }
    if not all(isinstance(value, str) and value for value in fields.values()):
        return None
    if fields["category"] not in CATEGORIES or fields["trigger_type"] not in SUPPORTED_TRIGGERS:
        return None
    approval = N8nWorkflowApproval(
        workflow_id=workflow_id,
        input_schema=schema,
        approved_by=str(record.get("approved_by") or ""),
        approved_at=str(record.get("approved_at") or ""),
        **fields,  # type: ignore[arg-type]
    )
    if record.get("digest") != approval.digest:
        return None
    return approval


def n8n_workflow_entry(source_id: str, approval: N8nWorkflowApproval) -> TeamToolEntry:
    """The derived tool for one approval. Its hints mirror the approved category."""
    read_only, destructive = _HINTS[approval.category]
    operation = n8n_tool_operation(approval.workflow_id)
    return TeamToolEntry(
        source_id=source_id,
        advertised_name=op_tool_name(source_id, operation),
        dispatch_name=operation,
        description=f"Runs the published n8n workflow '{approval.name}' and waits for its result. {approval.description}",
        input_schema=approval.input_schema,
        destructive=approval.category != "read",
        kind="mcp",
        op=None,
        read_only_hint=read_only,
        destructive_hint=destructive,
        admin_category=approval.category,
        n8n_workflow=approval,
    )


def _core(db_path: str | Path) -> str:
    return str(domain_database_path(db_path, "core"))


def _decode(value: object) -> dict[str, Any]:
    try:
        data = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def approvals_from_records(records: Mapping[str, Any]) -> dict[str, N8nWorkflowApproval]:
    approvals: dict[str, N8nWorkflowApproval] = {}
    for workflow_id, record in records.items():
        approval = approval_from_record(str(workflow_id), record)
        if approval is not None:
            approvals[approval.workflow_id] = approval
    return approvals


async def load_n8n_approvals(db_path: str | Path, team_id: str) -> dict[str, dict[str, N8nWorkflowApproval]]:
    """``{source_id: {workflow_id: approval}}``. A read failure yields none (fail closed)."""
    try:
        async with (
            open_pooled_read_ctx(_core(db_path)) as db,
            db.execute(
                "SELECT key, value FROM team_settings WHERE team_id=? AND key LIKE ?",
                (team_id, f"{KEY_PREFIX}%"),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 — approvals only add tools; losing them is safe
        logger.warning("n8n_workflows.load_failed team=%s err=%s", team_id, type(exc).__name__)
        return {}
    return {str(key)[len(KEY_PREFIX) :]: approvals_from_records(_decode(value)) for key, value in rows}


async def load_source_records(db_path: str | Path, team_id: str, source_id: str) -> dict[str, Any]:
    """The raw stored records of one source, for the approval dialog."""
    async with (
        open_pooled_read_ctx(_core(db_path)) as db,
        db.execute(
            "SELECT value FROM team_settings WHERE team_id=? AND key=?",
            (team_id, f"{KEY_PREFIX}{source_id}"),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    return _decode(row[0]) if row else {}


class TooManyApprovals(Exception):
    """The source already holds ``MAX_APPROVED_PER_SOURCE`` approvals."""


async def save_n8n_approval(
    db_path: str | Path,
    *,
    team_id: str,
    source_id: str,
    workflow_id: str,
    approval: N8nWorkflowApproval | None,
    actor_user_id: str,
) -> dict[str, Any] | None:
    """Store (or, with ``approval=None``, remove) one approval and bump the source revision.

    Returns the source's stored records, or None when the source no longer exists in this Team.
    Raises ``TooManyApprovals`` before writing when a new approval would exceed the limit.
    """
    now = datetime.now(tz=UTC).isoformat()
    key = f"{KEY_PREFIX}{source_id}"
    async with open_shared_ctx(_core(db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("SELECT 1 FROM team_tool_source WHERE team_id=? AND id=?", (team_id, source_id)) as cursor:
                exists = await cursor.fetchone()
            if not exists:
                await db.rollback()
                return None
            records = _decode(await TeamSettingsStore.get_with_connection(db, team_id, key) or "{}")
            if approval is None:
                records.pop(workflow_id, None)
            else:
                if workflow_id not in records and len(records) >= MAX_APPROVED_PER_SOURCE:
                    await db.rollback()
                    raise TooManyApprovals
                records[workflow_id] = approval.to_record()
            if records:
                await TeamSettingsStore.set_with_connection(
                    db, team_id, key, json.dumps(records, ensure_ascii=False, sort_keys=True), actor_user_id, updated_at=now
                )
            else:
                await db.execute("DELETE FROM team_settings WHERE team_id=? AND key=?", (team_id, key))
            # The source revision is each tool's dependency revision, so dependent Agents see the change.
            await db.execute("UPDATE team_tool_source SET updated_at=? WHERE team_id=? AND id=?", (now, team_id, source_id))
            await db.commit()
            return records
        except TooManyApprovals:
            raise
        except BaseException:
            await db.rollback()
            raise


async def current_approval(db_path: str | Path, team_id: str, source_id: str, workflow_id: str) -> N8nWorkflowApproval | None:
    """The approval as stored now, for the execution gate. Raises on read failure (the gate fails closed)."""
    return approvals_from_records(await load_source_records(db_path, team_id, source_id)).get(workflow_id)
