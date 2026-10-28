"""Read-only Composer metadata. Discovery never grants execution permission.

Projects hired Team Agents and indexed builtin/personal/team Skills under
current access. Other adapters report their missing contract independently. No package compiler,
model, connection discovery, execution reservation or catalog write runs here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_serializer, model_validator

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessContext, AccessControlStore, resource_grants_enforce_enabled
from bowhead_core.agent_capability_catalog import default_catalog
from bowhead_core.agent_execution_binding import team_agent_dispatchable_state_sql
from bowhead_core.agent_listing_safety import scan_agent_listing
from bowhead_core.catalog_localization import agent_listing_display_text
from bowhead_core.chat_reference_scope import ReferenceRequestScope, read_reference_access_context_in_connection, require_reference_request_scope
from bowhead_core.chat_references import (
    AgentResourceRef,
    CapabilityResourceRef,
    Identity,
    InputDocument,
    McpPromptResourceRef,
    McpSourceResourceRef,
    McpToolResourceRef,
    ReferenceNode,
    ResourceRef,
    SkillResourceRef,
    decode_reference_json,
)
from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadata, ReferenceReceiverMetadataIndex
from bowhead_core.reference_skill_compatibility import ReferenceSkillMetadata, reference_skill_modes
from bowhead_core.resource_access import builtin_capability_ids_for_skill
from bowhead_core.share_service import grants_for_viewer, is_visible
from bowhead_core.skill_reference_metadata import BuiltinSkillMetadataIndex
from bowhead_core.system_agent_marketplace import (
    SYSTEM_AGENT_LISTINGS,
    system_agent_localized_display_name,
    system_agent_localized_summary,
)
from bowhead_core.system_agent_reference_metadata import SystemAgentReferenceMetadataIndex, system_reference_runtime_modes
from bowhead_core.team_subagent_access import TeamSubagentAccessResolver
from bowhead_core.team_subagent_store import release_semantics_active_conn
from bowhead_core.uploaded_skill_reference_metadata import UploadedSkillMetadataIndex, uploaded_skill_metadata_rows

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolRegistry

EntryMode = Literal["ordinary", "steer", "plan", "resume"]
ResourceKind = Literal["agent", "skill", "capability", "mcp_source", "mcp_tool", "mcp_prompt"]
AuthorizationMode = Literal["baseline", "readonly_extension"]
Reason = Literal["unsupported_entry", "unsupported_binding", "resource_unavailable", "catalog_adapter_unavailable", "catalog_metadata_unavailable"]
PAGE_SIZE = 20


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ReferenceCatalogRequest(_Wire):
    workspace_id: Identity
    session_id: Identity | None = None
    trigger: Literal["@", "/"]
    query: Annotated[str, Field(max_length=128)] = ""
    cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    selected_refs: list[ResourceRef] = Field(default_factory=list, max_length=10)
    # Local parameter editing must resolve the saved identity, never a same-name
    # replacement or the first page of a search. This remains metadata discovery.
    exact_resource: SkillResourceRef | CapabilityResourceRef | McpToolResourceRef | McpPromptResourceRef | None = None
    entry_mode: EntryMode = "ordinary"
    submission_protocol: Literal["chat-references.v1"] = "chat-references.v1"

    @model_validator(mode="after")
    def exact_lookup(self):
        if self.exact_resource is not None and (self.trigger != "/" or self.query or self.cursor is not None):
            raise ValueError("reference_input_invalid")
        return self


class ReferenceEntrySupport(_Wire):
    submission_protocol: Literal["chat-references.v1"] = "chat-references.v1"
    entry_mode: EntryMode
    resource_kind: ResourceKind
    authorization_mode: AuthorizationMode
    adapter_id: Literal["unavailable", "initial-agent-skill", "initial-capability", "initial-mcp-read", "initial-mcp-prompt"] = "unavailable"
    adapter_version: Literal[0, 1] = 0
    supported: bool = False
    reason_code: Literal["unsupported_entry"] | None = "unsupported_entry"

    @model_validator(mode="after")
    def adapter_contract(self):
        if self.adapter_id == "unavailable":
            valid = self.adapter_version == 0 and not self.supported and self.reason_code == "unsupported_entry"
        elif self.adapter_id == "initial-agent-skill":
            valid = (
                self.adapter_version == 1
                and self.entry_mode in {"ordinary", "steer", "plan"}
                and self.resource_kind in {"agent", "skill"}
                and ((self.supported and self.reason_code is None) or (not self.supported and self.reason_code == "unsupported_entry"))
            )
        elif self.adapter_id == "initial-capability":
            valid = (
                self.adapter_version == 1
                and self.entry_mode in {"ordinary", "steer"}
                and self.resource_kind == "capability"
                and self.authorization_mode == "baseline"
                and ((self.supported and self.reason_code is None) or (not self.supported and self.reason_code == "unsupported_entry"))
            )
        elif self.adapter_id == "initial-mcp-prompt":
            valid = (
                self.adapter_version == 1
                and self.entry_mode in {"ordinary", "steer"}
                and self.resource_kind == "mcp_prompt"
                and self.authorization_mode == "baseline"
                and ((self.supported and self.reason_code is None) or (not self.supported and self.reason_code == "unsupported_entry"))
            )
        else:
            valid = (
                self.adapter_version == 1
                and self.entry_mode in {"ordinary", "steer"}
                and self.resource_kind in {"mcp_source", "mcp_tool"}
                and self.authorization_mode == "baseline"
                and ((self.supported and self.reason_code is None) or (not self.supported and self.reason_code == "unsupported_entry"))
            )
        if not valid or (self.authorization_mode == "readonly_extension" and self.resource_kind != "skill"):
            raise ValueError("reference_entry_support_invalid")
        return self


def reference_entry_support(entry_mode: EntryMode) -> list[ReferenceEntrySupport]:
    """Declare released Agent/Skill/Capability/MCP entry adapters.

    Deployment readiness is applied separately by the Gateway, so the feature
    flag, key material and durable worker must still be present before these
    released cells become selectable or writable for a request.
    """
    cells = [
        ReferenceEntrySupport(entry_mode=entry_mode, resource_kind=kind, authorization_mode=authorization)
        for kind in ("agent", "skill", "capability", "mcp_source", "mcp_tool", "mcp_prompt")
        for authorization in (("baseline", "readonly_extension") if kind == "skill" else ("baseline",))
    ]
    if entry_mode == "plan":
        return [
            cell.model_copy(update={"adapter_id": "initial-agent-skill", "adapter_version": 1, "supported": True, "reason_code": None})
            if cell.resource_kind in {"agent", "skill"}
            else cell
            for cell in cells
        ]
    if entry_mode not in {"ordinary", "steer"}:
        return cells
    return [
        cell.model_copy(
            update={
                "adapter_id": (
                    "initial-mcp-prompt"
                    if cell.resource_kind == "mcp_prompt"
                    else "initial-capability"
                    if cell.resource_kind == "capability"
                    else "initial-mcp-read"
                    if cell.resource_kind in {"mcp_source", "mcp_tool"}
                    else "initial-agent-skill"
                ),
                "adapter_version": 1,
                "supported": True,
                "reason_code": None,
            }
        )
        for cell in cells
    ]


def _validated_entry_support(entry_mode: EntryMode) -> list[ReferenceEntrySupport]:
    cells = [ReferenceEntrySupport.model_validate(cell.model_dump(mode="python")) for cell in reference_entry_support(entry_mode)]
    if (
        len(cells) != 7
        or len({(cell.resource_kind, cell.authorization_mode) for cell in cells}) != 7
        or any(cell.entry_mode != entry_mode for cell in cells)
    ):
        raise ValueError("reference_entry_support_invalid")
    return cells


def initial_reference_authoring_supported() -> bool:
    """The editor may mount only for a known released initial Agent cell."""
    try:
        return any(
            cell.resource_kind == "agent"
            and cell.authorization_mode == "baseline"
            and cell.supported
            and cell.adapter_id == "initial-agent-skill"
            and cell.adapter_version == 1
            for cell in _validated_entry_support("ordinary")
        )
    except (ValueError, TypeError, AttributeError):
        return False


def initial_reference_entry_supported(
    document: InputDocument,
    *,
    entry_mode: EntryMode = "ordinary",
    required_skill_modes: tuple[str, ...] | None = None,
) -> bool:
    references = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in references if isinstance(node.resource, AgentResourceRef)]
    skills = [node for node in references if isinstance(node.resource, SkillResourceRef)]
    capabilities = [node for node in references if isinstance(node.resource, CapabilityResourceRef)]
    mcp = [node for node in references if isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef)]
    prompts = [node for node in references if isinstance(node.resource, McpPromptResourceRef)]
    if entry_mode == "plan" and (len(agents) != 1 or agents[0].resource.source_kind != "team" or capabilities or mcp or prompts):
        return False
    if prompts:
        try:
            cells = _validated_entry_support(entry_mode)
            return bool(
                entry_mode in {"ordinary", "steer"}
                and len(agents) == 1
                and agents[0].resource.source_kind == "team"
                and 1 <= len(prompts) <= 3
                and len(references) == len(agents) + len(prompts)
                and required_skill_modes in (None, ())
                and all(
                    any(cell.resource_kind == kind and cell.supported and cell.adapter_id == adapter and cell.adapter_version == 1 for cell in cells)
                    for kind, adapter in (("agent", "initial-agent-skill"), ("mcp_prompt", "initial-mcp-prompt"))
                )
            )
        except (ValueError, TypeError, AttributeError):
            return False
    resource_selection = not mcp and (bool(skills) or bool(capabilities)) and len(skills) <= 3 and len(capabilities) <= 1
    mcp_selection = bool(mcp) and not skills and not capabilities
    agent_only = not capabilities and not skills and not mcp
    explicit_receiver = len(agents) == 1 and (agent_only or resource_selection or mcp_selection)
    automatic_receiver = entry_mode in {"ordinary", "steer"} and not agents and resource_selection
    if len(references) != len(agents) + len(skills) + len(capabilities) + len(mcp) or not (explicit_receiver or automatic_receiver):
        return False
    try:
        cells = _validated_entry_support(entry_mode)
        supported = {(cell.resource_kind, cell.authorization_mode): cell for cell in cells}

        def ready(kind, mode):
            cell = supported.get((kind, mode))
            return cell is not None and cell.supported and cell.adapter_id == "initial-agent-skill" and cell.adapter_version == 1

        if not ready("agent", "baseline"):
            return False
        if mcp:
            return required_skill_modes in (None, ()) and all(
                bool(
                    (cell := supported.get((node.resource.kind, "baseline"))) is not None
                    and cell.supported
                    and cell.adapter_id == "initial-mcp-read"
                    and cell.adapter_version == 1
                )
                for node in mcp
            )
        if capabilities:
            cell = supported.get(("capability", "baseline"))
            if not bool(cell is not None and cell.supported and cell.adapter_id == "initial-capability" and cell.adapter_version == 1):
                return False
        if not skills:
            return required_skill_modes in (None, ())
        return (
            any(ready("skill", mode) for mode in ("baseline", "readonly_extension"))
            if required_skill_modes is None
            else bool(required_skill_modes) and all(ready("skill", mode) for mode in required_skill_modes)
        )
    except (ValueError, TypeError, AttributeError):
        return False


class ReferenceCatalogItem(_Wire):
    kind: Literal["agent"] = "agent"
    source_kind: Literal["team", "system"] = "team"
    id: Identity
    label: Annotated[str, Field(min_length=1, max_length=256)]
    summary: Annotated[str, Field(max_length=1024)]
    # Missing exact metadata is a disabled discovery item, never a guessed ref.
    resource: AgentResourceRef | None
    availability: Literal["unavailable", "baseline"] = "unavailable"
    reason_code: Reason | None
    # The viewer owns this team Agent: the picker says "Created by you" instead of "Team Agent", which reads as shared.
    created_by_me: bool = False
    receiver_metadata: ReferenceReceiverMetadata | None = Field(default=None, exclude=True, repr=False)
    metadata_profile_name: str | None = Field(default=None, exclude=True, repr=False)

    @field_serializer("resource", when_used="json")
    def serialize_resource(self, resource: AgentResourceRef | None):
        return resource.model_dump(mode="json", exclude_none=True) if resource is not None else None

    @model_serializer(mode="wrap")
    def _omit_not_created_by_me(self, handler):
        # Additive to the v1 browser contract: sent only for the viewer's own Agents.
        data = handler(self)
        if isinstance(data, dict) and not data.get("created_by_me"):
            data.pop("created_by_me", None)
        return data

    @model_validator(mode="after")
    def exact_identity(self):
        if (self.availability == "baseline" and (self.resource is None or self.reason_code is not None)) or (
            self.availability == "unavailable" and self.reason_code is None
        ):
            raise ValueError("catalog_availability_invalid")
        if self.resource is not None and (self.resource.source_kind != self.source_kind or self.resource.id != self.id):
            raise ValueError("catalog_identity_invalid")
        if self.source_kind == "system" and not self.id.startswith("system:"):
            raise ValueError("catalog_identity_invalid")
        if self.resource is None and self.reason_code == "unsupported_entry":
            raise ValueError("catalog_identity_invalid")
        return self


class SkillCatalogItem(_Wire):
    kind: Literal["skill"] = "skill"
    source_kind: Literal["builtin", "user", "team"] = "builtin"
    id: Identity
    label: Annotated[str, Field(min_length=1, max_length=256)]
    summary: Annotated[str, Field(max_length=1024)]
    resource: SkillResourceRef | None
    read_only_parameter_schema: dict | None
    availability: Literal["unavailable", "baseline", "readonly_extension"] = "unavailable"
    reason_code: Reason | None
    metadata_name: str | None = Field(default=None, exclude=True, repr=False)
    metadata_entry_json: str | None = Field(default=None, exclude=True, repr=False)
    metadata_strict_access: bool = Field(default=False, exclude=True, repr=False)

    @model_validator(mode="after")
    def exact_identity(self):
        if (
            self.availability != "unavailable" and (self.resource is None or self.read_only_parameter_schema is None or self.reason_code is not None)
        ) or (self.availability == "unavailable" and self.reason_code is None):
            raise ValueError("catalog_availability_invalid")
        if self.resource is not None and (self.resource.source_kind != self.source_kind or self.resource.id != self.id):
            raise ValueError("catalog_identity_invalid")
        if self.source_kind == "builtin" and (self.resource is None or not self.id.startswith("builtin:")):
            raise ValueError("catalog_identity_invalid")
        if self.resource is None and (self.read_only_parameter_schema is not None or self.reason_code == "unsupported_entry"):
            raise ValueError("catalog_identity_invalid")
        return self


class CapabilityCatalogItem(_Wire):
    kind: Literal["capability"] = "capability"
    source_kind: Literal["platform"] = "platform"
    id: Identity
    label: Annotated[str, Field(min_length=1, max_length=256)]
    summary: Annotated[str, Field(max_length=1024)]
    resource: CapabilityResourceRef
    parameter_schema: dict
    availability: Literal["unavailable", "baseline"] = "unavailable"
    reason_code: Reason | None

    @model_validator(mode="after")
    def exact_identity(self):
        if (
            self.resource.id != self.id
            or (self.availability == "baseline" and self.reason_code is not None)
            or (self.availability == "unavailable" and self.reason_code is None)
        ):
            raise ValueError("catalog_identity_invalid")
        return self


class McpRemoteVersion(_Wire):
    status: Literal["unknown"] = "unknown"
    value: None = None
    evidence: None = None


class McpSourceCatalogItem(_Wire):
    kind: Literal["mcp_source"] = "mcp_source"
    source_kind: Literal["team"] = "team"
    id: Identity
    source_id: Identity
    label: Annotated[str, Field(min_length=1, max_length=256)]
    summary: Annotated[str, Field(max_length=1024)]
    resource: McpSourceResourceRef | None
    remote_version: McpRemoteVersion = Field(default_factory=McpRemoteVersion)
    availability: Literal["unavailable", "baseline"] = "unavailable"
    reason_code: Reason | None
    source_revision: str = Field(exclude=True, repr=False)
    reviewed_operation_names: tuple[str, ...] = Field(default=(), exclude=True, repr=False)

    @model_validator(mode="after")
    def exact_identity(self):
        if (
            self.id != self.source_id
            or (self.resource is not None and self.resource.source_id != self.source_id)
            or (self.availability == "baseline" and (self.resource is None or self.reason_code is not None))
            or (self.availability == "unavailable" and self.reason_code is None)
            or (self.resource is None and self.reason_code == "unsupported_entry")
            or not self.source_revision
            or len(set(self.reviewed_operation_names)) != len(self.reviewed_operation_names)
            or tuple(sorted(self.reviewed_operation_names)) != self.reviewed_operation_names
        ):
            raise ValueError("catalog_identity_invalid")
        return self


class McpToolCatalogItem(_Wire):
    kind: Literal["mcp_tool"] = "mcp_tool"
    source_kind: Literal["team"] = "team"
    id: Identity
    source_id: Identity
    label: Annotated[str, Field(min_length=1, max_length=256)]
    summary: Annotated[str, Field(max_length=1024)]
    resource: McpToolResourceRef | None
    parameter_schema: dict | None
    remote_version: McpRemoteVersion = Field(default_factory=McpRemoteVersion)
    availability: Literal["unavailable", "baseline"] = "unavailable"
    reason_code: Reason | None
    source_revision: str = Field(exclude=True, repr=False)
    reviewed_read_contract: bool = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def exact_identity(self):
        if (
            (self.resource is not None and self.resource.source_id != self.source_id)
            or (self.resource is not None and self.resource.operation_name != self.id)
            or (self.availability == "baseline" and (self.resource is None or self.parameter_schema is None or self.reason_code is not None))
            or (self.availability == "unavailable" and self.reason_code is None)
            or (self.resource is None and self.reason_code == "unsupported_entry")
            or (self.parameter_schema is None and self.reason_code == "unsupported_entry")
            or not self.source_revision
            or type(self.reviewed_read_contract) is not bool
        ):
            raise ValueError("catalog_identity_invalid")
        return self


class McpPromptCatalogItem(_Wire):
    kind: Literal["mcp_prompt"] = "mcp_prompt"
    source_kind: Literal["team"] = "team"
    id: Identity
    source_id: Identity
    prompt_name: Annotated[str, Field(min_length=1, max_length=512)]
    label: Annotated[str, Field(min_length=1, max_length=256)]
    summary: Annotated[str, Field(max_length=1024)]
    resource: McpPromptResourceRef | None
    parameter_schema: dict | None
    remote_version: McpRemoteVersion = Field(default_factory=McpRemoteVersion)
    availability: Literal["unavailable", "baseline"] = "unavailable"
    reason_code: Reason | None
    source_revision: str = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def exact_identity(self):
        if (
            (self.resource is not None and self.resource.source_id != self.source_id)
            or (self.resource is not None and self.resource.prompt_name != self.prompt_name)
            or (self.availability == "baseline" and (self.resource is None or self.parameter_schema is None or self.reason_code is not None))
            or (self.availability == "unavailable" and self.reason_code is None)
            or (self.resource is None and self.reason_code == "unsupported_entry")
            or (self.parameter_schema is None and self.reason_code == "unsupported_entry")
            or not self.source_revision
        ):
            raise ValueError("catalog_identity_invalid")
        return self


CatalogItem = Annotated[
    ReferenceCatalogItem | SkillCatalogItem | CapabilityCatalogItem | McpSourceCatalogItem | McpToolCatalogItem | McpPromptCatalogItem,
    Field(discriminator="kind"),
]
SkillAccessFactory = Callable[[], Awaitable[tuple[AccessControlStore, AccessContext]]]


class ReferenceCatalogGroup(_Wire):
    id: Literal["team_agents", "system_agents", "skills", "capabilities", "mcp"]
    items: list[CatalogItem] = Field(default_factory=list)
    error: Reason | None = None


class ReferenceCatalogResponse(_Wire):
    catalog_generation: str
    input_schema_versions: list[int] = Field(default_factory=lambda: [1])
    entry_support: list[ReferenceEntrySupport]
    groups: list[ReferenceCatalogGroup]
    next_cursor: str | None = None


class ReferenceCatalogError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def _require_scope(db: aiosqlite.Connection, *, team_id: str, user_id: str, query: ReferenceCatalogRequest) -> int:
    try:
        scope = ReferenceRequestScope(team_id=team_id, user_id=user_id, workspace_id=query.workspace_id, session_id=query.session_id)
        return await require_reference_request_scope(db, scope)
    except ValueError as exc:
        raise ReferenceCatalogError("reference_scope_unavailable") from exc


async def _team_agents(
    db: aiosqlite.Connection, *, db_path: Path, team_id: str, user_id: str, locale: str, receiver_index: ReferenceReceiverMetadataIndex | None = None
) -> ReferenceCatalogGroup:
    group = ReferenceCatalogGroup(id="team_agents")
    if not await release_semantics_active_conn(db):
        return group.model_copy(update={"error": "unsupported_binding"})
    decisions = await TeamSubagentAccessResolver(db_path).resolve_many_in_connection(db, team_id=team_id, actor_user_id=user_id)
    # Use the runtime's durable state predicate, then canonical access and the
    # current hire cycle. Only finite listing metadata is selected, not bodies.
    async with db.execute(
        f"""
        SELECT a.id,a.name,a.live_revision_id,a.live_release_id,
               assignment.id AS assignment_id,
               r.listing_json,behavior.revision_id AS behavior_revision_id,safety.status AS safety_status,
               r.id AS matched_revision_id
        FROM team_subagent a
        JOIN agent_assignment assignment ON assignment.team_id=a.team_id AND assignment.agent_id=a.id
          AND assignment.user_id=? AND assignment.agent_source_kind='team'
          AND assignment.personal_state='active' AND assignment.access_state='granted'
        LEFT JOIN team_subagent_release release ON release.team_id=a.team_id AND release.subagent_id=a.id
          AND release.id=a.live_release_id AND release.revision_id=a.live_revision_id
        LEFT JOIN team_subagent_revision r ON r.team_id=a.team_id AND r.subagent_id=a.id AND r.id=release.revision_id
          AND r.capability_fingerprint=release.capability_fingerprint AND r.dependency_fingerprint=release.dependency_fingerprint
        LEFT JOIN team_subagent_revision_behavior behavior ON behavior.team_id=a.team_id AND behavior.subagent_id=a.id AND behavior.revision_id=r.id
        LEFT JOIN agent_safety_report safety ON safety.team_id=a.team_id AND safety.agent_id=a.id
          AND safety.id=release.safety_report_id AND safety.subject_kind='revision' AND safety.subject_ref=r.id
          AND safety.dependency_fingerprint=r.dependency_fingerprint
        WHERE a.team_id=? AND {team_agent_dispatchable_state_sql("a")}
        ORDER BY a.id
        """,  # noqa: S608 -- shared fixed SQL state predicate, never request input
        (user_id, team_id),
    ) as cursor:
        rows = await cursor.fetchall()
    items = []
    error = None
    for row in rows:
        decision = decisions.get(row["id"])
        if decision is None or not decision.resolution.catalog_visible:
            continue
        try:
            listing = decode_reference_json(row["listing_json"]) if row["listing_json"] is not None else None
            if listing is None:
                # No current immutable listing: do not expose mutable draft text.
                error = "catalog_metadata_unavailable"
                continue
            display_name, summary = agent_listing_display_text(listing, routing_name=str(row["name"]), requested_locale=locale)
            if scan_agent_listing(name=row["name"], display_name=display_name, description=summary).blocked:
                continue
            exact = bool(row["matched_revision_id"] and row["behavior_revision_id"] and row["safety_status"] not in (None, "blocked", "unknown"))
            resource = (
                AgentResourceRef(
                    kind="agent",
                    source_kind="team",
                    id=row["id"],
                    version=row["live_revision_id"],
                    assignment_id=row["assignment_id"],
                    release_id=row["live_release_id"],
                )
                if exact
                else None
            )
            items.append(
                ReferenceCatalogItem(
                    id=row["id"],
                    label=display_name,
                    summary=summary[:1024],
                    resource=resource,
                    created_by_me=bool(getattr(decision.facts, "is_owner", False)),
                    metadata_profile_name=row["name"],
                    receiver_metadata=receiver_index.find(team_id, resource)
                    if resource is not None and receiver_index is not None and receiver_index.db_path == db_path
                    else None,
                    reason_code="resource_unavailable"
                    if not decision.resolution.can_consume
                    else "unsupported_entry"
                    if exact
                    else "unsupported_binding",
                )
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            error = "catalog_metadata_unavailable"
    return group.model_copy(update={"items": items, "error": error})


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _page_offset(cursor: str | None, *, generation: str, ids: list[str]) -> int:
    if cursor is None:
        return 0
    try:
        payload = decode_reference_json(base64.b64decode(cursor, altchars=b"-_", validate=True), max_bytes=1024)
        if not isinstance(payload, dict) or set(payload) != {"generation", "last"} or payload["generation"] != generation:
            raise ValueError("stale cursor")
        return ids.index(payload["last"]) + 1
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ReferenceCatalogError("reference_catalog_cursor_invalid") from exc


async def _builtin_skills(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    user_id: str,
    query: ReferenceCatalogRequest,
    epoch: int,
    index: BuiltinSkillMetadataIndex | None,
    access_factory: SkillAccessFactory | None,
) -> ReferenceCatalogGroup:
    if index is None:
        return ReferenceCatalogGroup(id="skills", error="catalog_adapter_unavailable")
    try:
        if access_factory is None:
            raise ValueError("catalog_metadata_unavailable")
        access_store, context = await access_factory()
        if (context.team_id, context.user_id, context.workspace_id, context.policy_epoch) != (team_id, user_id, query.workspace_id, epoch):
            raise ValueError("catalog_metadata_unavailable")
        async with db.execute("SELECT name,enabled FROM skill_instances WHERE user_id=? AND source_type='builtin'", (user_id,)) as cursor:
            preferences = {row["name"]: bool(row["enabled"]) for row in await cursor.fetchall()}
        items = []
        for item in index.items:
            targets = [("skill", item.name), *(("builtin_capability", cid) for cid in builtin_capability_ids_for_skill(item.name))]
            visible = True
            strict_access = True
            for kind, resource_id in targets:
                decision = await access_store.explain_decision(context, kind, resource_id, "use", connection=db)
                strict_access &= bool(decision.get("allowed"))
                if not decision.get("allowed") and resource_grants_enforce_enabled():
                    visible = False
                    break
            if visible:
                items.append(
                    SkillCatalogItem(
                        id=item.id,
                        label=item.label,
                        summary=item.summary,
                        resource=SkillResourceRef(kind="skill", source_kind="builtin", id=item.id, version=item.revision),
                        read_only_parameter_schema=item.parameter_schema(),
                        metadata_name=item.name,
                        metadata_entry_json=item.read_only_entry_json,
                        metadata_strict_access=strict_access,
                        reason_code="unsupported_entry" if preferences.get(item.name, True) else "resource_unavailable",
                    )
                )
        # Grant reads use the canonical store's connections. Reject a policy
        # update observed across those reads instead of publishing a mixed page.
        if await access_store.get_policy_epoch(team_id) != epoch:
            raise ValueError("catalog_metadata_unavailable")
        return ReferenceCatalogGroup(id="skills", items=items, error="catalog_metadata_unavailable" if index.incomplete else None)
    except Exception:
        # No fallback to an unfiltered catalog, and no raw schema/path/account
        # details in an HTTP error. Other resource groups retain their own state.
        return ReferenceCatalogGroup(id="skills", error="catalog_metadata_unavailable")


async def _uploaded_skills(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
    team_id: str,
    user_id: str,
    query: ReferenceCatalogRequest,
    epoch: int,
    index: UploadedSkillMetadataIndex | None,
    access_factory: SkillAccessFactory | None,
) -> ReferenceCatalogGroup:
    try:
        rows = await uploaded_skill_metadata_rows(db, user_id=user_id, team_id=team_id)
        if not rows:
            return ReferenceCatalogGroup(id="skills")
        if access_factory is None:
            raise ValueError("catalog_metadata_unavailable")
        access_store, requested = await access_factory()
        if Path(access_store.db_path) != db_path or (requested.team_id, requested.user_id, requested.workspace_id, requested.policy_epoch) != (
            team_id,
            user_id,
            query.workspace_id,
            epoch,
        ):
            raise ValueError("catalog_metadata_unavailable")
        scope = ReferenceRequestScope(team_id=team_id, user_id=user_id, workspace_id=query.workspace_id, session_id=query.session_id)
        context = await read_reference_access_context_in_connection(db, scope=scope, access_store=access_store)
        shared = await grants_for_viewer(
            access_store, team_id=team_id, user_id=user_id, workspace_id=query.workspace_id, kind="team_skill", connection=db
        )
        async with db.execute("SELECT name,source_type,enabled FROM skill_instances WHERE user_id=?", (user_id,)) as cursor:
            cursor.row_factory = aiosqlite.Row
            preferences = {row["name"]: dict(row) for row in await cursor.fetchall()}
        items = []
        incomplete = False
        for kind, row in rows:
            if kind == "team" and not is_visible(
                owner_id=row["created_by"], visibility=row["visibility"], resource_id=row["id"], viewer_id=user_id, granted_ids=shared
            ):
                continue
            targets = [("team_skill", row["id"])] if kind == "team" else [("skill", row["name"])]
            targets.extend(("builtin_capability", cid) for cid in builtin_capability_ids_for_skill(row["name"]))
            visible = True
            strict_access = True
            for target_kind, identity in targets:
                decision = await access_store.explain_decision(context, target_kind, identity, "use", connection=db)
                owner_exempt = target_kind == "team_skill" and row["created_by"] == user_id and decision.get("reason") == "not_in_allowlist"
                strict_access &= bool(decision.get("allowed")) or owner_exempt
                if not decision.get("allowed") and not owner_exempt and resource_grants_enforce_enabled():
                    visible = False
                    break
            if not visible:
                continue
            if kind == "user":
                active = bool(row["enabled"]) and row["import_status"] != "pending"
            else:
                preference = preferences.get("team:" + row["id"])
                if preference is None:
                    legacy = preferences.get(row["name"])
                    preference = legacy if legacy is not None and legacy["source_type"] == "team_shared" else None
                active = (
                    row["status"] == "enabled"
                    and row["approval"] == "approved"
                    and (row["visibility"] != "team_wide" or bool(row["approved_sha256"]))
                    and (preference is None or bool(preference["enabled"]))
                )
            metadata = index.find(kind, row) if index is not None and active else None
            incomplete |= active and metadata is None
            items.append(
                SkillCatalogItem(
                    source_kind=kind,
                    id=row["id"],
                    label=metadata.label if metadata else str(row.get("display_name") or row["name"])[:256],
                    summary=metadata.summary if metadata else str(row.get("description") or "")[:1024],
                    resource=SkillResourceRef(kind="skill", source_kind=kind, id=row["id"], version=metadata.revision) if metadata else None,
                    read_only_parameter_schema=metadata.parameter_schema() if metadata else None,
                    metadata_name=metadata.name if metadata else None,
                    metadata_entry_json=metadata.read_only_entry_json if metadata else None,
                    metadata_strict_access=strict_access,
                    reason_code="resource_unavailable" if not active else "unsupported_entry" if metadata else "catalog_metadata_unavailable",
                )
            )
        if await access_store.get_policy_epoch(team_id) != epoch:
            raise ValueError("catalog_metadata_unavailable")
        return ReferenceCatalogGroup(id="skills", items=items, error="catalog_metadata_unavailable" if incomplete else None)
    except Exception:
        return ReferenceCatalogGroup(id="skills", error="catalog_metadata_unavailable")


async def _system_agents(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
    team_id: str,
    workspace_id: str,
    locale: str,
    index: SystemAgentReferenceMetadataIndex | None,
) -> ReferenceCatalogGroup:
    if index is None:
        return ReferenceCatalogGroup(id="system_agents", error="catalog_adapter_unavailable")
    if Path(index.db_path) != db_path or (team_id, workspace_id) not in index.scopes:
        return ReferenceCatalogGroup(id="system_agents", error="catalog_metadata_unavailable")
    try:
        # Scope was checked on this transaction; system-required membership
        # follows the existing product rule. No user assignment is fabricated.
        async with db.execute("SELECT name,enabled,row_version FROM team_builtin_subagent_override WHERE team_id=?", (team_id,)) as cursor:
            cursor.row_factory = aiosqlite.Row
            overrides = {row["name"]: dict(row) for row in await cursor.fetchall()}
        items = []
        incomplete = False
        for name in SYSTEM_AGENT_LISTINGS:
            if (team_id, workspace_id, name) not in index.names:
                continue
            override = overrides.get(name)
            if override is not None and not override["enabled"]:
                continue
            metadata = index.find(team_id=team_id, workspace_id=workspace_id, name=name, override=override)
            incomplete |= metadata is None
            items.append(
                ReferenceCatalogItem(
                    source_kind="system",
                    id="system:" + name,
                    label=metadata.localized_label(locale) if metadata else system_agent_localized_display_name(name, locale),
                    summary=metadata.localized_summary(locale) if metadata else system_agent_localized_summary(name, locale),
                    resource=AgentResourceRef(kind="agent", source_kind="system", id="system:" + name, version=metadata.revision)
                    if metadata
                    else None,
                    receiver_metadata=metadata.receiver if metadata else None,
                    metadata_profile_name=name,
                    reason_code="unsupported_entry"
                    if metadata
                    else "unsupported_binding"
                    if override and not system_reference_runtime_modes()[1]
                    else "catalog_metadata_unavailable",
                )
            )
        return ReferenceCatalogGroup(id="system_agents", items=items, error="catalog_metadata_unavailable" if incomplete else None)
    except Exception:
        return ReferenceCatalogGroup(id="system_agents", error="catalog_metadata_unavailable")


def _capabilities(locale: str) -> ReferenceCatalogGroup:
    catalog = default_catalog()
    if catalog is None:
        return ReferenceCatalogGroup(id="capabilities", error="catalog_metadata_unavailable")
    try:
        catalog.validate_reviewed_evidence()
        selected_locale = locale if locale in {"en-US", "zh-CN"} else "en-US"
        items = []
        for definition in catalog.definitions:
            if definition.lifecycle != "active" or not definition.market_visible or not definition.authoring_selectable:
                continue
            listing = definition.listing[selected_locale]
            reviewed = any(
                implementation.lifecycle == "active"
                and implementation.review_state == "reviewed"
                and implementation.exact_ref == "capi_image_generation_native@1"
                for implementation in catalog.implementations_for(definition)
            )
            items.append(
                CapabilityCatalogItem(
                    id=definition.id,
                    label=listing.name,
                    summary=listing.description,
                    resource=CapabilityResourceRef(kind="capability", id=definition.id, version=definition.version),
                    parameter_schema=definition.parameters_schema,
                    reason_code="unsupported_entry" if reviewed else "unsupported_binding",
                )
            )
        return ReferenceCatalogGroup(id="capabilities", items=items)
    except Exception:
        return ReferenceCatalogGroup(id="capabilities", error="catalog_metadata_unavailable")


async def _mcp_sources(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
    team_id: str,
    user_id: str,
    workspace_id: str,
    epoch: int,
    index: ReferenceMcpMetadataIndex | None,
    access_factory: SkillAccessFactory | None,
) -> ReferenceCatalogGroup:
    if index is None:
        return ReferenceCatalogGroup(id="mcp", error="catalog_adapter_unavailable")
    if Path(index.db_path) != db_path or not index.ready:
        return ReferenceCatalogGroup(id="mcp", error="catalog_metadata_unavailable")
    try:
        if access_factory is None:
            raise ValueError("catalog_metadata_unavailable")
        access_store, requested = await access_factory()
        if Path(access_store.db_path) != db_path or (requested.team_id, requested.user_id, requested.workspace_id, requested.policy_epoch) != (
            team_id,
            user_id,
            workspace_id,
            epoch,
        ):
            raise ValueError("catalog_metadata_unavailable")
        items: list[CatalogItem] = []
        async with db.execute(
            "SELECT id,status,updated_at,visibility,created_by FROM team_tool_source WHERE team_id=?",
            (team_id,),
        ) as cursor:
            current_rows = {str(row["id"]): row for row in await cursor.fetchall()}
        indexed = {source.source_id: source for source in index.for_team(team_id)}
        current_enabled = {source_id for source_id, row in current_rows.items() if row["status"] == "enabled"}
        incomplete = team_id in index.incomplete_teams or current_enabled != set(indexed)
        for source in indexed.values():
            current = current_rows.get(source.source_id)
            if current is None or current["status"] != "enabled":
                continue
            current_created_by = str(current["created_by"]) if current["created_by"] is not None else None
            if (
                source.state_version != str(current["updated_at"])
                or source.visibility != str(current["visibility"])
                or source.created_by != current_created_by
            ):
                incomplete = True
                continue
            if source.visibility == "creator_only" and source.created_by != user_id:
                continue
            source_decision = await access_store.explain_decision(requested, "team_tool_source", source.source_id, "use", connection=db)
            if resource_grants_enforce_enabled() and not source_decision.get("allowed"):
                continue
            source_reason: Reason = "unsupported_entry" if source.resource is not None else "unsupported_binding"
            operation_items: list[McpToolCatalogItem] = []
            prompt_items: list[McpPromptCatalogItem] = []
            reviewed_operation_names: list[str] = []
            for operation in source.operations:
                operation_decision = await access_store.explain_decision(
                    requested,
                    "team_tool_op",
                    operation.id,
                    "use",
                    connection=db,
                )
                if resource_grants_enforce_enabled() and not operation_decision.get("allowed"):
                    continue
                parameter_schema = operation.parameter_schema()
                exact = operation.resource is not None and parameter_schema is not None
                reason: Reason = "unsupported_entry" if exact and operation.reviewed_read_contract else "unsupported_binding"
                operation_items.append(
                    McpToolCatalogItem(
                        id=operation.id,
                        source_id=source.source_id,
                        label=operation.label,
                        summary=operation.summary,
                        resource=operation.resource,
                        parameter_schema=parameter_schema,
                        remote_version=McpRemoteVersion(),
                        reason_code=reason,
                        source_revision=operation.source_revision,
                        reviewed_read_contract=operation.reviewed_read_contract,
                    )
                )
                if reason == "unsupported_entry":
                    reviewed_operation_names.append(operation.id)
            for prompt in source.prompts:
                parameter_schema = prompt.parameter_schema()
                exact = prompt.resource is not None and parameter_schema is not None
                prompt_items.append(
                    McpPromptCatalogItem(
                        id=prompt.id,
                        source_id=source.source_id,
                        prompt_name=prompt.prompt_name,
                        label=prompt.label,
                        summary=prompt.summary,
                        resource=prompt.resource,
                        parameter_schema=parameter_schema,
                        remote_version=McpRemoteVersion(),
                        reason_code=("unsupported_entry" if exact and index.prompt_cache_ready else "unsupported_binding"),
                        source_revision=prompt.source_revision,
                    )
                )
            items.append(
                McpSourceCatalogItem(
                    id=source.source_id,
                    source_id=source.source_id,
                    label=source.label,
                    summary=source.summary,
                    resource=source.resource,
                    remote_version=McpRemoteVersion(),
                    reason_code=source_reason,
                    source_revision=source.state_version,
                    reviewed_operation_names=tuple(reviewed_operation_names),
                )
            )
            items.extend(operation_items)
            items.extend(prompt_items)
        if await access_store.get_policy_epoch(team_id) != epoch:
            raise ValueError("catalog_metadata_unavailable")
        return ReferenceCatalogGroup(
            id="mcp",
            items=items,
            error="catalog_metadata_unavailable" if incomplete else None,
        )
    except Exception:
        return ReferenceCatalogGroup(id="mcp", error="catalog_metadata_unavailable")


async def read_reference_catalog(
    db_path: str | Path,
    *,
    team_id: str,
    user_id: str,
    query: ReferenceCatalogRequest,
    locale: str = "en-US",
    builtin_skill_index: BuiltinSkillMetadataIndex | None = None,
    builtin_skill_access: SkillAccessFactory | None = None,
    uploaded_skill_index: UploadedSkillMetadataIndex | None = None,
    system_agent_index: SystemAgentReferenceMetadataIndex | None = None,
    receiver_index: ReferenceReceiverMetadataIndex | None = None,
    mcp_index: ReferenceMcpMetadataIndex | None = None,
    initial_writer_available: bool = False,
    capability_writer_available: bool = False,
    mcp_writer_available: bool = False,
    mcp_prompt_writer_available: bool = False,
    automatic_receiver_identities: frozenset[tuple[str, str, str]] | None = None,
    tool_registry: ToolRegistry | None = None,
) -> ReferenceCatalogResponse:
    query = ReferenceCatalogRequest.model_validate(query.model_dump(mode="python"))
    epoch, groups, compatibility_groups = await read_reference_metadata(
        db_path,
        team_id=team_id,
        user_id=user_id,
        query=query,
        locale=locale,
        builtin_skill_index=builtin_skill_index,
        builtin_skill_access=builtin_skill_access,
        uploaded_skill_index=uploaded_skill_index,
        system_agent_index=system_agent_index,
        receiver_index=receiver_index,
        mcp_index=mcp_index,
    )
    return _catalog_response(
        team_id=team_id,
        user_id=user_id,
        query=query,
        locale=locale,
        epoch=epoch,
        groups=groups,
        metadata_generation=builtin_skill_index.generation if query.trigger == "/" and builtin_skill_index else None,
        initial_writer_available=initial_writer_available,
        capability_writer_available=capability_writer_available,
        mcp_writer_available=mcp_writer_available,
        mcp_prompt_writer_available=mcp_prompt_writer_available and bool(mcp_index and mcp_index.prompt_cache_ready),
        compatibility_groups=compatibility_groups,
        automatic_receiver_identities=automatic_receiver_identities,
        tool_registry=tool_registry,
    )


async def read_reference_metadata(
    db_path: str | Path,
    *,
    team_id: str,
    user_id: str,
    query: ReferenceCatalogRequest,
    locale: str = "en-US",
    builtin_skill_index: BuiltinSkillMetadataIndex | None = None,
    builtin_skill_access: SkillAccessFactory | None = None,
    uploaded_skill_index: UploadedSkillMetadataIndex | None = None,
    system_agent_index: SystemAgentReferenceMetadataIndex | None = None,
    receiver_index: ReferenceReceiverMetadataIndex | None = None,
    mcp_index: ReferenceMcpMetadataIndex | None = None,
    include_receivers: bool = False,
) -> tuple[int, list[ReferenceCatalogGroup], list[ReferenceCatalogGroup]]:
    """Internal full snapshot; callers must not expose private dependency metadata.

    Both explicit composition discovery and automatic receiver discovery consume
    these current access decisions before search or UI pagination is applied.
    """
    query = ReferenceCatalogRequest.model_validate(query.model_dump(mode="python"))
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        try:
            epoch = await _require_scope(db, team_id=team_id, user_id=user_id, query=query)
            needs_combination = (
                include_receivers
                or query.exact_resource is None
                and (
                    query.trigger == "/"
                    or query.trigger == "@"
                    and any(
                        isinstance(
                            ref,
                            SkillResourceRef | CapabilityResourceRef | McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef,
                        )
                        for ref in query.selected_refs
                    )
                )
            )
            if query.trigger == "@" or needs_combination:
                try:
                    team = await _team_agents(
                        db, db_path=Path(db_path), team_id=team_id, user_id=user_id, locale=locale, receiver_index=receiver_index
                    )
                except sqlite3.Error:
                    team = ReferenceCatalogGroup(id="team_agents", error="catalog_metadata_unavailable")
                system = await _system_agents(
                    db, db_path=Path(db_path), team_id=team_id, workspace_id=query.workspace_id, locale=locale, index=system_agent_index
                )
                agents = [team, system]
            else:
                agents = []
            needs_baseline = any(item.receiver_metadata and item.receiver_metadata.grants for group in agents for item in group.items)
            if query.trigger == "/" or needs_combination or needs_baseline:
                skills = await _builtin_skills(
                    db, team_id=team_id, user_id=user_id, query=query, epoch=epoch, index=builtin_skill_index, access_factory=builtin_skill_access
                )
                uploaded = await _uploaded_skills(
                    db,
                    db_path=Path(db_path),
                    team_id=team_id,
                    user_id=user_id,
                    query=query,
                    epoch=epoch,
                    index=uploaded_skill_index,
                    access_factory=builtin_skill_access,
                )
                items = [*skills.items, *uploaded.items]
                errors = [group.error for group in (skills, uploaded) if group.error]
                error = ("catalog_metadata_unavailable" if items or "catalog_metadata_unavailable" in errors else errors[0]) if errors else None
                skills = ReferenceCatalogGroup(id="skills", items=items, error=error)
                resource_groups = [skills]
                if query.trigger == "/" or needs_combination:
                    resource_groups.extend(
                        [
                            _capabilities(locale),
                            await _mcp_sources(
                                db,
                                db_path=Path(db_path),
                                team_id=team_id,
                                user_id=user_id,
                                workspace_id=query.workspace_id,
                                epoch=epoch,
                                index=mcp_index,
                                access_factory=builtin_skill_access,
                            ),
                        ]
                    )
            else:
                resource_groups = []
            groups = agents if query.trigger == "@" else resource_groups
            compatibility_groups = [*agents, *resource_groups] if needs_combination or needs_baseline else []
        finally:
            await db.rollback()
    return epoch, groups, compatibility_groups


def _catalog_response(
    *,
    team_id: str,
    user_id: str,
    query: ReferenceCatalogRequest,
    locale: str,
    epoch: int,
    groups: list[ReferenceCatalogGroup],
    metadata_generation: str | None = None,
    initial_writer_available: bool = False,
    capability_writer_available: bool = False,
    mcp_writer_available: bool = False,
    mcp_prompt_writer_available: bool = False,
    compatibility_groups: list[ReferenceCatalogGroup] | None = None,
    automatic_receiver_identities: frozenset[tuple[str, str, str]] | None = None,
    tool_registry: ToolRegistry | None = None,
) -> ReferenceCatalogResponse:
    try:
        support = _validated_entry_support(query.entry_mode)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReferenceCatalogError("reference_catalog_unavailable") from exc
    mcp_group = next(
        (group for group in [*(compatibility_groups or []), *groups] if group.id == "mcp"),
        None,
    )
    if mcp_group is not None and mcp_group.error is not None:
        mcp_writer_available = False
    if not initial_writer_available:
        support = [
            cell.model_copy(
                update={
                    "adapter_id": "unavailable",
                    "adapter_version": 0,
                    "supported": False,
                    "reason_code": "unsupported_entry",
                }
            )
            for cell in support
        ]
    else:
        support = [
            cell.model_copy(
                update={
                    "adapter_id": "unavailable",
                    "adapter_version": 0,
                    "supported": False,
                    "reason_code": "unsupported_entry",
                }
            )
            if (cell.resource_kind == "capability" and not capability_writer_available)
            or (cell.resource_kind in {"mcp_source", "mcp_tool"} and not mcp_writer_available)
            or (cell.resource_kind == "mcp_prompt" and not (mcp_prompt_writer_available and mcp_writer_available))
            else cell
            for cell in support
        ]
    agent_ready = any(
        cell.entry_mode == query.entry_mode
        and cell.resource_kind == "agent"
        and cell.authorization_mode == "baseline"
        and cell.supported
        and cell.adapter_id == "initial-agent-skill"
        and cell.adapter_version == 1
        for cell in support
    )
    if query.entry_mode == "plan":
        # The first Plan cell has no automatic/system receiver or invocable
        # Skill baseline. Native Tool effects are rechecked at admission.
        automatic_receiver_identities = frozenset()

        def plan_receiver(item):
            if isinstance(item, ReferenceCatalogItem) and (
                item.source_kind != "team"
                or item.receiver_metadata is None
                or item.receiver_metadata.team_tools
                or any(grant.permissions != ("read",) for grant in item.receiver_metadata.grants)
            ):
                return item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
            if isinstance(item, ReferenceCatalogItem) and item.receiver_metadata is not None:
                from bowhead_core.chat_reference_plan import require_readonly_tools

                try:
                    require_readonly_tools(item.receiver_metadata.tool_refs, tool_registry)
                except ValueError:
                    return item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
            return item

        groups = [group.model_copy(update={"items": [plan_receiver(item) for item in group.items]}) for group in groups]
        compatibility_groups = [
            group.model_copy(update={"items": [plan_receiver(item) for item in group.items]}) for group in compatibility_groups or []
        ]
    # A standalone Agent still carries its entire exact Skill baseline. Use the
    # same metadata check as mixed selections, including selected-token refresh.
    if agent_ready and all(isinstance(ref, AgentResourceRef) for ref in query.selected_refs):
        skills = reference_skill_metadata_map(compatibility_groups or groups)

        def standalone(item):
            if not isinstance(item, ReferenceCatalogItem) or item.resource is None or item.reason_code != "unsupported_entry":
                return item
            if item.receiver_metadata is None:
                return item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
            if reference_skill_modes(item.receiver_metadata, (), skills) is None:
                return item.model_copy(update={"availability": "unavailable", "reason_code": "resource_unavailable"})
            return item.model_copy(update={"availability": "baseline", "reason_code": None})

        groups = [group.model_copy(update={"items": [standalone(item) for item in group.items]}) for group in groups]
    if agent_ready and compatibility_groups:
        groups = _compatible_choices(
            query,
            groups,
            compatibility_groups,
            support,
            automatic_receiver_identities=automatic_receiver_identities,
        )
    if query.exact_resource is not None:
        groups = [group.model_copy(update={"items": [item for item in group.items if item.resource == query.exact_resource]}) for group in groups]
    generation = _digest(
        {
            "catalog_adapter": "reference-listing.v3",
            "metadata_generation": metadata_generation,
            "team": team_id,
            "user": user_id,
            "locale": locale,
            "epoch": epoch,
            "query": query.model_dump(mode="json", exclude={"cursor", "exact_resource"} if query.exact_resource is None else {"cursor"}),
            "groups": [
                group.model_copy(update={"items": sorted(group.items, key=lambda item: (item.kind, item.source_kind, item.id))}).model_dump(
                    mode="json"
                )
                for group in groups
            ],
            "support": [cell.model_dump(mode="json") for cell in support],
        }
    )
    search = query.query.casefold().strip()
    items = sorted(
        ((group.id, item) for group in groups for item in group.items if search in f"{item.label}\n{item.summary}".casefold()),
        key=lambda pair: (pair[1].availability == "unavailable", pair[1].label.casefold(), pair[1].kind, pair[1].source_kind, pair[1].id),
    )

    def identity(pair):
        item = pair[1]
        return json.dumps([pair[0], item.kind, item.source_kind, item.id], separators=(",", ":"))

    ids = [identity(pair) for pair in items]
    if len(set(ids)) != len(ids):
        raise ReferenceCatalogError("reference_catalog_unavailable")
    offset = _page_offset(query.cursor, generation=generation, ids=ids)
    page = items[offset : offset + PAGE_SIZE]
    # Every page rebuilds current scope/access and verifies the full generation.
    next_cursor = (
        base64.urlsafe_b64encode(json.dumps({"generation": generation, "last": identity(page[-1])}).encode()).decode()
        if offset + PAGE_SIZE < len(items)
        else None
    )
    return ReferenceCatalogResponse(
        catalog_generation=generation,
        entry_support=support,
        next_cursor=next_cursor,
        groups=[group.model_copy(update={"items": [item for group_id, item in page if group_id == group.id]}) for group in groups],
    )


def reference_skill_metadata_map(metadata_groups: list[ReferenceCatalogGroup]) -> dict[SkillResourceRef, ReferenceSkillMetadata]:
    return {
        item.resource: ReferenceSkillMetadata(
            item.metadata_name, item.metadata_entry_json, item.reason_code == "unsupported_entry", item.metadata_strict_access
        )
        for group in metadata_groups
        for item in group.items
        if isinstance(item, SkillCatalogItem) and item.resource is not None and item.metadata_name is not None
    }


def _compatible_choices(query, groups, metadata_groups, support, *, automatic_receiver_identities=None):
    from bowhead_core.chat_reference_capability import capability_supported_by_behavior

    agents = [item for group in metadata_groups for item in group.items if isinstance(item, ReferenceCatalogItem)]
    skills = reference_skill_metadata_map(metadata_groups)
    selected_agents = [ref for ref in query.selected_refs if isinstance(ref, AgentResourceRef)]
    selected_skills = [ref for ref in query.selected_refs if isinstance(ref, SkillResourceRef)]
    selected_capabilities = [ref for ref in query.selected_refs if isinstance(ref, CapabilityResourceRef)]
    selected_mcp_sources = [ref for ref in query.selected_refs if isinstance(ref, McpSourceResourceRef)]
    selected_mcp_tools = [ref for ref in query.selected_refs if isinstance(ref, McpToolResourceRef)]
    selected_mcp_prompts = [ref for ref in query.selected_refs if isinstance(ref, McpPromptResourceRef)]
    selected_mcp = [*selected_mcp_sources, *selected_mcp_tools, *selected_mcp_prompts]
    if (
        len(selected_agents) > 1
        or len(selected_capabilities) > 1
        or len(selected_mcp_sources) > 3
        or len(selected_mcp_tools) > 3
        or len(selected_mcp_prompts) > 3
        or len(selected_agents) + len(selected_skills) + len(selected_capabilities) + len(selected_mcp) != len(query.selected_refs)
    ):
        return groups
    ready_modes = {
        cell.authorization_mode
        for cell in support
        if cell.resource_kind == "skill" and cell.supported and cell.adapter_id == "initial-agent-skill" and cell.adapter_version == 1
    }
    capability_ready = any(
        cell.resource_kind == "capability"
        and cell.authorization_mode == "baseline"
        and cell.supported
        and cell.adapter_id == "initial-capability"
        and cell.adapter_version == 1
        for cell in support
    )
    agent_ready = any(
        cell.resource_kind == "agent"
        and cell.authorization_mode == "baseline"
        and cell.supported
        and cell.adapter_id == "initial-agent-skill"
        and cell.adapter_version == 1
        for cell in support
    )
    mcp_ready = (
        {
            cell.resource_kind
            for cell in support
            if cell.resource_kind in {"mcp_source", "mcp_tool", "mcp_prompt"}
            and cell.authorization_mode == "baseline"
            and cell.supported
            and cell.adapter_id == ("initial-mcp-prompt" if cell.resource_kind == "mcp_prompt" else "initial-mcp-read")
            and cell.adapter_version == 1
        }
        if agent_ready
        else set()
    )
    if any(resource.kind not in mcp_ready for resource in selected_mcp):
        return groups
    if selected_mcp and (selected_skills or selected_capabilities) or selected_mcp_prompts and (selected_mcp_sources or selected_mcp_tools):
        return groups
    mcp_items = {
        item.resource: item
        for group in metadata_groups
        for item in group.items
        if isinstance(item, McpSourceCatalogItem | McpToolCatalogItem | McpPromptCatalogItem) and item.resource is not None
    }

    def receiver_supports_mcp(
        receiver: ReferenceReceiverMetadata | None, resource: McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef
    ) -> bool:
        item = mcp_items.get(resource)
        if receiver is None or item is None:
            return False
        if isinstance(item, McpPromptCatalogItem):
            return item.reason_code == "unsupported_entry" and reference_skill_modes(receiver, (), skills) is not None
        if isinstance(item, McpToolCatalogItem):
            baseline = receiver.team_tool_baseline(
                source_id=item.source_id,
                operation_name=item.id,
                source_revision=item.source_revision,
            )
            return bool(item.reviewed_read_contract and baseline is not None and baseline.risk == "read" and not baseline.approval_required)
        return any(
            baseline.source_id == item.source_id
            and baseline.source_revision == item.source_revision
            and baseline.operation_name in item.reviewed_operation_names
            and baseline.risk == "read"
            and not baseline.approval_required
            for baseline in receiver.team_tools
        )

    def receiver_supports_all_mcp(receiver: ReferenceReceiverMetadata | None, resources=selected_mcp) -> bool:
        return receiver is not None and all(receiver_supports_mcp(receiver, resource) for resource in resources)

    def receiver_accepts_current(item: ReferenceCatalogItem) -> bool:
        if item.resource is None or item.reason_code not in {"unsupported_entry", None} or item.receiver_metadata is None:
            return False
        if selected_mcp_prompts and item.resource.source_kind != "team":
            return False
        modes = reference_skill_modes(item.receiver_metadata, selected_skills, skills)
        return bool(
            modes is not None
            and set(modes) <= ready_modes
            and (
                not selected_capabilities
                or capability_ready
                and capability_supported_by_behavior(item.receiver_metadata.behavior, selected_capabilities[0])
            )
            and receiver_supports_all_mcp(item.receiver_metadata)
        )

    def available_receivers() -> list[ReferenceCatalogItem]:
        return [
            item
            for item in agents
            if (not selected_agents or item.resource == selected_agents[0])
            and (
                selected_agents
                or automatic_receiver_identities is None
                or (item.source_kind, item.id, item.metadata_profile_name) in automatic_receiver_identities
            )
            and receiver_accepts_current(item)
        ]

    if mcp_ready:
        selected_source_ids = {resource.source_id for resource in selected_mcp}

        def mcp_project(item):
            if isinstance(item, ReferenceCatalogItem):
                if not selected_mcp or item.resource is None or item.reason_code != "unsupported_entry":
                    return item
                return (
                    item.model_copy(update={"availability": "baseline", "reason_code": None})
                    if receiver_accepts_current(item)
                    else item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
                )
            if not isinstance(item, McpSourceCatalogItem | McpToolCatalogItem | McpPromptCatalogItem):
                return item
            if item.kind not in mcp_ready or item.resource is None or item.reason_code != "unsupported_entry":
                return item
            if (
                selected_skills
                or selected_capabilities
                or (selected_mcp_prompts and not isinstance(item, McpPromptCatalogItem))
                or (isinstance(item, McpPromptCatalogItem) and (selected_mcp_sources or selected_mcp_tools))
            ):
                return item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
            if (
                item.resource in selected_mcp
                or len(query.selected_refs) >= 10
                or isinstance(item, McpSourceCatalogItem)
                and len(selected_mcp_sources) >= 3
                or isinstance(item, McpToolCatalogItem)
                and len(selected_mcp_tools) >= 3
                or isinstance(item, McpPromptCatalogItem)
                and len(selected_mcp_prompts) >= 3
                or len(selected_source_ids | {item.source_id}) > 3
            ):
                return item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
            resources = [*selected_mcp, item.resource]
            receivers = available_receivers()
            supported = any(
                (not isinstance(item, McpPromptCatalogItem) or receiver.source_kind == "team")
                and receiver_supports_all_mcp(receiver.receiver_metadata, resources)
                for receiver in receivers
            )
            return item.model_copy(
                update={
                    "availability": "baseline" if supported else "unavailable",
                    "reason_code": None if supported else "unsupported_binding",
                }
            )

        groups = [group.model_copy(update={"items": [mcp_project(item) for item in group.items]}) for group in groups]

    if selected_capabilities or any(isinstance(item, CapabilityCatalogItem) for group in groups for item in group.items):

        def receiver_matches(item: ReferenceCatalogItem, capability: CapabilityResourceRef) -> bool:
            modes = reference_skill_modes(item.receiver_metadata, selected_skills, skills) if item.receiver_metadata is not None else None
            return bool(
                item.resource is not None
                and item.reason_code in {"unsupported_entry", None}
                and item.receiver_metadata is not None
                and capability_supported_by_behavior(item.receiver_metadata.behavior, capability)
                and modes is not None
                and set(modes) <= ready_modes
                and receiver_supports_all_mcp(item.receiver_metadata)
            )

        def capability_project(item):
            if item.reason_code != "unsupported_entry" or item.resource is None:
                return item
            if not capability_ready:
                return item
            if isinstance(item, ReferenceCatalogItem):
                if not selected_capabilities:
                    return item
                return (
                    item.model_copy(update={"availability": "baseline", "reason_code": None})
                    if receiver_matches(item, selected_capabilities[0])
                    else item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
                )
            if not isinstance(item, CapabilityCatalogItem) or selected_capabilities:
                return item
            if selected_mcp:
                return item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
            receivers = [
                agent
                for agent in agents
                if (
                    (not selected_agents or agent.resource == selected_agents[0])
                    and (
                        selected_agents
                        or automatic_receiver_identities is None
                        or (agent.source_kind, agent.id, agent.metadata_profile_name) in automatic_receiver_identities
                    )
                )
            ]
            return (
                item.model_copy(update={"availability": "baseline", "reason_code": None})
                if any(receiver_matches(receiver, item.resource) for receiver in receivers)
                else item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
            )

        groups = [group.model_copy(update={"items": [capability_project(item) for item in group.items]}) for group in groups]

    def project(item):
        if item.resource is None or item.reason_code != "unsupported_entry":
            return item
        if isinstance(
            item,
            McpSourceCatalogItem | McpToolCatalogItem | McpPromptCatalogItem,
        ):
            # MCP has its own receiver/effect compatibility contract.  Until
            # that adapter is released, Agent/Skill/Capability compatibility must not
            # reinterpret an exact MCP identity as a Skill extension.
            return item
        if isinstance(item, CapabilityCatalogItem):
            return item
        if selected_mcp and isinstance(item, SkillCatalogItem):
            return item
        if isinstance(item, ReferenceCatalogItem):
            receiver = item.receiver_metadata
            roots = selected_skills
        else:
            roots = [*selected_skills, item.resource]
            owner = next(
                (
                    agent
                    for agent in agents
                    if len(selected_agents) == 1
                    and agent.resource == selected_agents[0]
                    and agent.reason_code in {"unsupported_entry", None}
                    and receiver_supports_all_mcp(agent.receiver_metadata)
                ),
                None,
            )
            receivers = (
                [owner.receiver_metadata]
                if owner is not None
                else [
                    agent.receiver_metadata
                    for agent in agents
                    if not selected_agents
                    and agent.resource is not None
                    and agent.reason_code in {"unsupported_entry", None}
                    and receiver_supports_all_mcp(agent.receiver_metadata)
                    and (
                        automatic_receiver_identities is None
                        or (agent.source_kind, agent.id, agent.metadata_profile_name) in automatic_receiver_identities
                    )
                ]
            )
            matches = [
                (receiver, modes)
                for receiver in receivers
                if receiver is not None
                and (not selected_capabilities or capability_supported_by_behavior(receiver.behavior, selected_capabilities[0]))
                and (modes := reference_skill_modes(receiver, roots, skills)) is not None
                and set(modes) <= ready_modes
            ]
            if not matches:
                return item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
            mode = (
                "baseline"
                if any(
                    any((grant.kind, grant.id) == (item.resource.source_kind, item.resource.id) for grant in receiver.grants)
                    for receiver, _ in matches
                )
                else "readonly_extension"
            )
            return item.model_copy(update={"availability": mode, "reason_code": None})
        if not roots:
            return item
        modes = reference_skill_modes(receiver, roots, skills) if receiver is not None else None
        if modes is None:
            return item.model_copy(update={"availability": "unavailable", "reason_code": "unsupported_binding"})
        if not set(modes) <= ready_modes:
            return item
        mode = "baseline"
        return item.model_copy(update={"availability": mode, "reason_code": None})

    return [group.model_copy(update={"items": [project(item) for item in group.items]}) for group in groups]
