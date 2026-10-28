"""Network-free metadata snapshots for Composer team Tool references.

The snapshot deliberately separates a local source/operation contract from a
connection subject.  Existing team Tool rows do not persist an authenticated
account subject, so only anonymous connections can currently produce an exact
reference.  Encrypted credentials and credential hashes never enter this
index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Literal

import aiosqlite
import anyio
from bowhead_skill.readonly_entry import ReadOnlyEntryError, validate_parameter_schema

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_references import (
    McpPromptResourceRef,
    McpSourceResourceRef,
    McpToolResourceRef,
)
from bowhead_core.openapi_tool_source import TeamToolEntry, mcp_entry, openapi_entry, parse_openapi_ops

logger = logging.getLogger(__name__)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_SOURCES = 512
_MAX_OPERATIONS_PER_SOURCE = 2000
_MAX_CONTRACT_BYTES = 2 * 1024 * 1024


def _hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _label(value: object, *, fallback: str, limit: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(_CONTROL_RE.sub(" ", text).split())
    return (text or fallback)[:limit]


@dataclass(frozen=True, slots=True)
class ReferenceMcpRemoteVersion:
    status: Literal["unknown"] = "unknown"
    value: None = None
    evidence: None = None


@dataclass(frozen=True, slots=True)
class ReferenceMcpOperationMetadata:
    id: str
    source_id: str
    label: str
    summary: str
    source_revision: str
    parameter_schema_json: str | None
    reviewed_read_contract: bool
    resource: McpToolResourceRef | None
    remote_version: ReferenceMcpRemoteVersion = field(default_factory=ReferenceMcpRemoteVersion)

    def parameter_schema(self) -> dict | None:
        return json.loads(self.parameter_schema_json) if self.parameter_schema_json is not None else None


@dataclass(frozen=True, slots=True)
class ReferenceMcpPromptMetadata:
    id: str
    source_id: str
    prompt_name: str
    label: str
    summary: str
    source_revision: str
    parameter_schema_json: str | None
    resource: McpPromptResourceRef | None
    remote_version: ReferenceMcpRemoteVersion = field(
        default_factory=ReferenceMcpRemoteVersion,
    )

    def parameter_schema(self) -> dict | None:
        return json.loads(self.parameter_schema_json) if self.parameter_schema_json is not None else None


@dataclass(frozen=True, slots=True)
class ReferenceMcpSourceMetadata:
    team_id: str
    source_id: str
    label: str
    summary: str
    visibility: str
    created_by: str | None
    state_version: str
    resource: McpSourceResourceRef | None
    operations: tuple[ReferenceMcpOperationMetadata, ...]
    prompts: tuple[ReferenceMcpPromptMetadata, ...] = ()
    remote_version: ReferenceMcpRemoteVersion = field(default_factory=ReferenceMcpRemoteVersion)


@dataclass(frozen=True, slots=True)
class ReferenceMcpMetadataIndex:
    db_path: Path
    items: tuple[ReferenceMcpSourceMetadata, ...] = ()
    ready: bool = False
    incomplete_teams: frozenset[str] = frozenset()
    prompt_cache_ready: bool = False
    _by_team: Mapping[str, tuple[ReferenceMcpSourceMetadata, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        grouped: dict[str, list[ReferenceMcpSourceMetadata]] = {}
        identities: set[tuple[str, str]] = set()
        for item in self.items:
            identity = (item.team_id, item.source_id)
            if identity in identities:
                raise ValueError("ambiguous MCP reference metadata")
            identities.add(identity)
            grouped.setdefault(item.team_id, []).append(item)
        object.__setattr__(
            self,
            "_by_team",
            MappingProxyType({team_id: tuple(values) for team_id, values in grouped.items()}),
        )

    def for_team(self, team_id: str) -> tuple[ReferenceMcpSourceMetadata, ...]:
        return self._by_team.get(team_id, ())


def _source_resource(
    *,
    team_id: str,
    source_id: str,
    kind: str,
    base_url: str,
    auth_type: str,
    entries: tuple[TeamToolEntry, ...],
) -> tuple[McpSourceResourceRef | None, str, str | None]:
    contracts = [
        {
            "advertised_name": entry.advertised_name,
            "dispatch_name": entry.dispatch_name,
            "input_schema": entry.input_schema,
            "kind": entry.kind,
            "method": entry.op.method.upper() if entry.op is not None else None,
            "path": entry.op.path if entry.op is not None else None,
        }
        for entry in entries
    ]
    local_revision = _hash(
        {
            "schema": "reference-mcp-source-v1",
            "source_id": source_id,
            "kind": kind,
            "base_url": base_url,
            "operations": contracts,
        }
    )
    if auth_type != "none":
        return None, local_revision, None
    # Anonymous access has no rotating bearer material.  Its stable external
    # subject is the registered route itself; account-bearing connections need
    # a future registry field and must not derive identity from auth_ref.
    subject = _hash(
        {
            "schema": "reference-mcp-anonymous-subject-v1",
            "team_id": team_id,
            "source_id": source_id,
            "base_url": base_url,
        }
    )
    return (
        McpSourceResourceRef(
            kind="mcp_source",
            source_id=source_id,
            local_contract_revision=local_revision,
            connection_binding_id=source_id,
            connection_subject_fingerprint=subject,
        ),
        local_revision,
        subject,
    )


def _operation(
    entry: TeamToolEntry,
    *,
    source_resource: McpSourceResourceRef | None,
    local_revision: str,
    source_revision: str,
) -> ReferenceMcpOperationMetadata:
    reviewed_read = entry.op is not None and entry.op.method.upper() in {"GET", "HEAD", "OPTIONS"}
    try:
        parameter_schema = validate_parameter_schema(entry.input_schema)
    except ReadOnlyEntryError:
        parameter_schema = None
    operation_revision = _hash(
        {
            "schema": "reference-mcp-operation-v1",
            "source_id": entry.source_id,
            "advertised_name": entry.advertised_name,
            "dispatch_name": entry.dispatch_name,
            "input_schema": entry.input_schema,
            "kind": entry.kind,
            "method": entry.op.method.upper() if entry.op is not None else None,
            "path": entry.op.path if entry.op is not None else None,
            "effect_contract": "read" if reviewed_read else "unreviewed",
        }
    )
    resource = (
        McpToolResourceRef(
            kind="mcp_tool",
            source_id=entry.source_id,
            local_contract_revision=local_revision,
            connection_binding_id=source_resource.connection_binding_id,
            connection_subject_fingerprint=source_resource.connection_subject_fingerprint,
            operation_name=entry.advertised_name,
            operation_contract_revision=operation_revision,
        )
        if source_resource is not None and parameter_schema is not None
        else None
    )
    return ReferenceMcpOperationMetadata(
        id=entry.advertised_name,
        source_id=entry.source_id,
        label=_label(entry.dispatch_name, fallback="Operation", limit=120),
        summary=_label(entry.description, fallback=entry.dispatch_name, limit=1024),
        source_revision=source_revision,
        parameter_schema_json=(
            json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if parameter_schema is not None else None
        ),
        reviewed_read_contract=reviewed_read,
        resource=resource,
    )


def _prompt(
    value: object,
    *,
    source_id: str,
    source_resource: McpSourceResourceRef | None,
    local_revision: str,
    source_revision: str,
) -> ReferenceMcpPromptMetadata:
    if not isinstance(value, dict) or set(value) != {
        "name",
        "title",
        "description",
        "arguments",
    }:
        raise ValueError("invalid MCP Prompt cache")
    name, title = value["name"], value["title"]
    description, arguments = value["description"], value["arguments"]
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 512
        or not isinstance(title, str)
        or len(title) > 256
        or not isinstance(description, str)
        or len(description) > 4096
        or not isinstance(arguments, list)
        or len(arguments) > 128
    ):
        raise ValueError("invalid MCP Prompt cache")
    properties: dict[str, object] = {}
    required: list[str] = []
    for argument in arguments:
        if not isinstance(argument, dict) or set(argument) != {
            "name",
            "description",
            "required",
        }:
            raise ValueError("invalid MCP Prompt argument cache")
        argument_name = argument["name"]
        argument_description = argument["description"]
        is_required = argument["required"]
        if (
            not isinstance(argument_name, str)
            or not argument_name
            or len(argument_name) > 256
            or argument_name in properties
            or not isinstance(argument_description, str)
            or len(argument_description) > 2048
            or type(is_required) is not bool
        ):
            raise ValueError("invalid MCP Prompt argument cache")
        properties[argument_name] = {
            "type": "string",
            **({"description": argument_description} if argument_description else {}),
        }
        if is_required:
            required.append(argument_name)
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    try:
        parameter_schema = validate_parameter_schema(schema)
    except ReadOnlyEntryError:
        parameter_schema = None
    prompt_revision = _hash(
        {
            "schema": "reference-mcp-prompt-v1",
            "source_id": source_id,
            "name": name,
            "arguments": arguments,
            "effect_contract": "expand",
        }
    )
    resource = (
        McpPromptResourceRef(
            kind="mcp_prompt",
            source_id=source_resource.source_id,
            local_contract_revision=local_revision,
            connection_binding_id=source_resource.connection_binding_id,
            connection_subject_fingerprint=(source_resource.connection_subject_fingerprint),
            prompt_name=name,
            prompt_contract_revision=prompt_revision,
        )
        if source_resource is not None and parameter_schema is not None
        else None
    )
    identity = "mcpprompt." + source_id + "." + hashlib.sha256(name.encode()).hexdigest()[:24]
    return ReferenceMcpPromptMetadata(
        id=identity,
        source_id=source_id,
        prompt_name=name,
        label=_label(title, fallback=name, limit=256),
        summary=_label(description, fallback=name, limit=1024),
        source_revision=source_revision,
        parameter_schema_json=(
            json.dumps(
                parameter_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if parameter_schema is not None
            else None
        ),
        resource=resource,
    )


def _entries(row: aiosqlite.Row) -> tuple[TeamToolEntry, ...]:
    selected = {str(value) for value in json.loads(str(row["selected_op_ids_json"] or "[]"))}
    if row["kind"] == "openapi":
        raw_contract = str(row["openapi_spec_json"] or "{}")
        if len(raw_contract.encode("utf-8", "replace")) > _MAX_CONTRACT_BYTES:
            raise ValueError("OpenAPI contract limit exceeded")
        spec = json.loads(raw_contract)
        values = [openapi_entry(str(row["id"]), op) for op in parse_openapi_ops(spec) if not selected or op.operation_id in selected]
    elif row["kind"] == "mcp":
        raw_contract = str(row["tools_schema_json"] or "[]")
        if len(raw_contract.encode("utf-8", "replace")) > _MAX_CONTRACT_BYTES:
            raise ValueError("MCP tools cache limit exceeded")
        tools = json.loads(raw_contract)
        if not isinstance(tools, list):
            raise ValueError("invalid MCP tools cache")
        values = [
            mcp_entry(str(row["id"]), value)
            for value in tools
            if isinstance(value, dict) and value.get("name") and (not selected or str(value["name"]) in selected)
        ]
    else:
        raise ValueError("unsupported team Tool source kind")
    if len(values) > _MAX_OPERATIONS_PER_SOURCE:
        raise ValueError("MCP source operation limit exceeded")
    return tuple(sorted(values, key=lambda value: value.advertised_name))


async def build_reference_mcp_metadata(db_path: str | Path) -> ReferenceMcpMetadataIndex:
    items: list[ReferenceMcpSourceMetadata] = []
    incomplete: set[str] = set()
    prompt_cache_ready = False
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        async with db.execute("PRAGMA table_info(team_tool_source)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        prompt_cache_ready = "prompts_schema_json" in columns
        prompt_projection = "prompts_schema_json" if prompt_cache_ready else "'[]' AS prompts_schema_json"
        async with db.execute(
            f"""
            SELECT id,team_id,name,kind,base_url,openapi_spec_json,selected_op_ids_json,
                   auth_type,tools_schema_json,{prompt_projection},
                   visibility,created_by,updated_at
            FROM team_tool_source
            WHERE status='enabled'
            ORDER BY team_id,id
            LIMIT ?
            """,
            (_MAX_SOURCES + 1,),
        ) as cursor:
            rows = await cursor.fetchall()
    if len(rows) > _MAX_SOURCES:
        incomplete.update(str(row["team_id"]) for row in rows[_MAX_SOURCES:])
        rows = rows[:_MAX_SOURCES]
    for row in rows:
        team_id = str(row["team_id"])
        try:
            entries = await anyio.to_thread.run_sync(_entries, row)
            source_resource, local_revision, _subject = _source_resource(
                team_id=team_id,
                source_id=str(row["id"]),
                kind=str(row["kind"]),
                base_url=str(row["base_url"]),
                auth_type=str(row["auth_type"]),
                entries=entries,
            )
            operations = tuple(
                _operation(
                    entry,
                    source_resource=source_resource,
                    local_revision=local_revision,
                    source_revision=str(row["updated_at"]),
                )
                for entry in entries
            )
            raw_prompts = str(row["prompts_schema_json"] or "[]")
            if len(raw_prompts.encode("utf-8", "replace")) > _MAX_CONTRACT_BYTES:
                raise ValueError("MCP Prompt cache limit exceeded")
            prompt_values = json.loads(raw_prompts)
            if not isinstance(prompt_values, list):
                raise ValueError("invalid MCP Prompt cache")
            if row["kind"] != "mcp" and prompt_values:
                raise ValueError("MCP Prompt cache belongs to a non-MCP source")
            prompts = tuple(
                _prompt(
                    value,
                    source_id=str(row["id"]),
                    source_resource=source_resource,
                    local_revision=local_revision,
                    source_revision=str(row["updated_at"]),
                )
                for value in prompt_values
            )
            if (
                len(prompts) > _MAX_OPERATIONS_PER_SOURCE
                or len({prompt.id for prompt in prompts}) != len(prompts)
                or len({prompt.prompt_name for prompt in prompts}) != len(prompts)
            ):
                raise ValueError("ambiguous MCP Prompt metadata")
            items.append(
                ReferenceMcpSourceMetadata(
                    team_id=team_id,
                    source_id=str(row["id"]),
                    label=_label(row["name"], fallback="Team connection", limit=256),
                    summary="MCP connection" if row["kind"] == "mcp" else "OpenAPI connection",
                    visibility=str(row["visibility"]),
                    created_by=str(row["created_by"]) if row["created_by"] is not None else None,
                    state_version=str(row["updated_at"]),
                    resource=source_resource,
                    operations=operations,
                    prompts=prompts,
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            incomplete.add(team_id)
    return ReferenceMcpMetadataIndex(
        Path(db_path),
        tuple(items),
        True,
        frozenset(incomplete),
        prompt_cache_ready,
    )


class ReferenceMcpMetadataCache:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._index = ReferenceMcpMetadataIndex(self.db_path)
        self._expires_at = 0.0
        self._lock = anyio.Lock()

    def current(self) -> ReferenceMcpMetadataIndex:
        return self._index if monotonic() < self._expires_at else ReferenceMcpMetadataIndex(self.db_path)

    async def refresh(self) -> None:
        async with self._lock:
            index = await build_reference_mcp_metadata(self.db_path)
            self._index, self._expires_at = index, monotonic() + 30

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                logger.warning("reference_mcp_metadata_refresh_failed error=%s", type(exc).__name__)
            await anyio.sleep(15)


__all__ = [
    "ReferenceMcpMetadataCache",
    "ReferenceMcpMetadataIndex",
    "ReferenceMcpOperationMetadata",
    "ReferenceMcpPromptMetadata",
    "ReferenceMcpSourceMetadata",
    "build_reference_mcp_metadata",
]
