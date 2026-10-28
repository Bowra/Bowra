"""Composer reference wire codec. Valid input is not an execution grant.

Keep labels and draft node IDs out of semantic identity. Resource discovery,
authorization, parameter schemas and execution readiness belong to the resolver.
No HTTP or runtime writer is enabled by importing this module.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

MAX_DOCUMENT_BYTES = 256 * 1024
MAX_DOCUMENT_NODES = 512
MAX_REFERENCE_NODES = 10
MAX_PARAMETER_BYTES = 16 * 1024
MAX_PARAMETER_DEPTH = 8
MAX_JSON_DEPTH = 16
MAX_OBJECT_KEYS = 128
MAX_ARRAY_ITEMS = 512
MAX_JSON_VALUES = 16384
MAX_SAFE_INTEGER = (1 << 53) - 1
# Match ECMAScript trim used by the browser; never apply this to stored text.
_TASK_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"

Identity = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_.:-]+$")]
ContentHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
NodeId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _bounded_json(value: object, *, max_depth: int = MAX_JSON_DEPTH, max_bytes: int = MAX_DOCUMENT_BYTES) -> None:
    """Bound the original object before Pydantic recursion or normalization."""
    pending = [(value, 0)]
    count = 0
    string_bytes = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_JSON_VALUES:
            raise ValueError("reference_json_too_many_values")
        if isinstance(item, (dict, list)):
            if depth >= max_depth:
                raise ValueError("reference_json_too_deep")
            if isinstance(item, dict):
                if len(item) > MAX_OBJECT_KEYS or any(type(key) is not str for key in item):
                    raise ValueError("reference_json_invalid_object")
                pending.extend((key, depth + 1) for key in item)
                pending.extend((child, depth + 1) for child in item.values())
            else:
                if len(item) > MAX_ARRAY_ITEMS:
                    raise ValueError("reference_json_array_too_large")
                pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            item_bytes = len(item.encode("utf-8"))
            if item_bytes > max_bytes:
                raise ValueError("reference_json_string_too_large")
            string_bytes += item_bytes
            if string_bytes > max_bytes:
                raise ValueError("reference_json_too_large")
        elif type(item) in (int, float):
            if (type(item) is float and not math.isfinite(item)) or abs(item) > MAX_SAFE_INTEGER:
                raise ValueError("reference_json_unsafe_number")
        elif item is not None and type(item) is not bool:
            raise ValueError("reference_json_invalid_value")
    if len(_json_bytes(value)) > max_bytes:
        raise ValueError("reference_json_too_large")


def _normalize_numbers(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _normalize_numbers(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(child) for child in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AgentResourceRef(_WireModel):
    kind: Literal["agent"]
    source_kind: Literal["team", "system"]
    id: Identity
    version: Identity
    assignment_id: Identity | None = None
    release_id: Identity | None = None

    @model_validator(mode="after")
    def exact_source_fields(self) -> Self:
        if self.source_kind == "team" and (self.assignment_id is None or self.release_id is None):
            raise ValueError("reference_agent_assignment_required")
        if self.source_kind == "system" and (self.assignment_id is not None or self.release_id is not None):
            raise ValueError("reference_system_agent_has_team_fields")
        return self


class SkillResourceRef(_WireModel):
    kind: Literal["skill"]
    source_kind: Literal["builtin", "team", "user"]
    id: Identity
    version: ContentHash


class CapabilityResourceRef(_WireModel):
    kind: Literal["capability"]
    id: Identity
    # Definition versions are positive integers in agent_capability_catalog.
    version: int = Field(ge=1, le=MAX_SAFE_INTEGER)


class _McpBinding(_WireModel):
    source_id: Identity
    local_contract_revision: ContentHash
    connection_binding_id: Identity
    connection_subject_fingerprint: ContentHash


class McpSourceResourceRef(_McpBinding):
    kind: Literal["mcp_source"]


class McpToolResourceRef(_McpBinding):
    kind: Literal["mcp_tool"]
    operation_name: Annotated[str, Field(min_length=1, max_length=512)]
    operation_contract_revision: ContentHash


class McpPromptResourceRef(_McpBinding):
    kind: Literal["mcp_prompt"]
    prompt_name: Annotated[str, Field(min_length=1, max_length=512)]
    prompt_contract_revision: ContentHash


ResourceRef = Annotated[
    AgentResourceRef | SkillResourceRef | CapabilityResourceRef | McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef,
    Field(discriminator="kind"),
]


def requested_reference_operation(resource: ResourceRef) -> str:
    """Describe the user's selection; this does not grant permission to execute it."""
    return {
        "agent": "delegate",
        "skill": "read",
        "capability": "execute",
        "mcp_source": "provide",
        "mcp_tool": "invoke",
        "mcp_prompt": "expand",
    }[resource.kind]


class TextNode(_WireModel):
    type: Literal["text"]
    text: str


class ReferenceNode(_WireModel):
    type: Literal["reference"]
    node_id: NodeId
    resource: ResourceRef
    label: Annotated[str, Field(min_length=1, max_length=256)]
    parameters: dict[str, JsonValue]

    @field_validator("parameters", mode="before")
    @classmethod
    def bound_parameters(cls, value: object) -> object:
        _bounded_json(value, max_depth=MAX_PARAMETER_DEPTH, max_bytes=MAX_PARAMETER_BYTES)
        return value

    @field_validator("parameters")
    @classmethod
    def normalize_parameter_numbers(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {key: _normalize_numbers(child) for key, child in value.items()}


InputNode = Annotated[TextNode | ReferenceNode, Field(discriminator="type")]


class InputDocument(_WireModel):
    schema_version: Literal[1]
    nodes: list[InputNode] = Field(max_length=MAX_DOCUMENT_NODES)

    @model_validator(mode="before")
    @classmethod
    def bound_original_document(cls, value: object) -> object:
        _bounded_json(value)
        # Literal[1] otherwise accepts True and 1.0 even with strict=True.
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
            raise ValueError("reference_schema_version_invalid")
        return value

    @field_validator("nodes")
    @classmethod
    def normalize_nodes(cls, nodes: list[InputNode]) -> list[InputNode]:
        result: list[InputNode] = []
        for node in nodes:
            if isinstance(node, TextNode) and result and isinstance(result[-1], TextNode):
                result[-1] = TextNode(type="text", text=result[-1].text + node.text)
            else:
                result.append(node)
        # Merge first: an editor may split a CRLF pair across adjacent nodes.
        # References remain boundaries, so text on either side is not joined.
        return [
            TextNode(type="text", text=node.text.replace("\r\n", "\n").replace("\r", "\n")) if isinstance(node, TextNode) else node for node in result
        ]

    @model_validator(mode="after")
    def check_selection(self) -> Self:
        refs = [node for node in self.nodes if isinstance(node, ReferenceNode)]
        if len(refs) > MAX_REFERENCE_NODES:
            raise ValueError("reference_count_exceeded")
        if len({node.node_id for node in refs}) != len(refs):
            raise ValueError("reference_duplicate_node_id")
        identities: set[tuple[str, ...]] = set()
        source_bindings: dict[str, bytes] = {}
        counts: dict[str, int] = {}
        for node in refs:
            resource = node.resource
            counts[resource.kind] = counts.get(resource.kind, 0) + 1
            if isinstance(resource, _McpBinding):
                binding = _json_bytes(resource.model_dump(include=set(_McpBinding.model_fields)))
                previous = source_bindings.setdefault(resource.source_id, binding)
                if previous != binding:
                    raise ValueError("reference_source_binding_conflict")
                name = (
                    resource.operation_name
                    if isinstance(resource, McpToolResourceRef)
                    else resource.prompt_name
                    if isinstance(resource, McpPromptResourceRef)
                    else ""
                )
                identity = (resource.kind, resource.source_id, name)
            else:
                identity = (resource.kind, getattr(resource, "source_kind", ""), resource.id)
            if identity in identities:
                # Same logical resource at two revisions is a conflict too.
                raise ValueError("reference_duplicate_resource")
            identities.add(identity)
        if any(counts.get(kind, 0) > maximum for kind, maximum in {"agent": 1, "skill": 3, "capability": 1}.items()):
            raise ValueError("reference_kind_quota_exceeded")
        if len(source_bindings) > 3 or counts.get("mcp_tool", 0) + counts.get("mcp_prompt", 0) > 3:
            raise ValueError("reference_mcp_quota_exceeded")
        return self

    def display_text(self) -> str:
        """Untrusted input snapshot for wire matching; NOT a viewer or LLM grant."""
        return "".join(
            node.text if isinstance(node, TextNode) else ("@" if node.resource.kind == "agent" else "/") + node.label for node in self.nodes
        )

    def text_content(self) -> str:
        """Only verbatim text nodes, excluding display labels."""
        return "".join(node.text for node in self.nodes if isinstance(node, TextNode))

    def validate_task(self, *, has_attachments: bool) -> None:
        if not self.text_content().strip(_TASK_WHITESPACE) and not has_attachments:
            raise ValueError("reference_task_content_required")
        if any(isinstance(node, ReferenceNode) for node in self.nodes) and self.text_content().lstrip(_TASK_WHITESPACE).startswith("/remember"):
            raise ValueError("reference_command_conflict")

    def semantic_projection(self) -> dict[str, object]:
        """HMAC input, never a local recovery checksum or authorized model prompt."""
        return {
            "schema_version": self.schema_version,
            "nodes": [
                node.model_dump(mode="json", exclude={"label", "node_id"}, exclude_none=True)
                if isinstance(node, ReferenceNode)
                else node.model_dump()
                for node in self.nodes
                if not isinstance(node, TextNode) or node.text != ""
            ],
        }


def decode_reference_json(
    raw: str | bytes,
    *,
    max_bytes: int = MAX_DOCUMENT_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
) -> JsonValue:
    """Bound JSON before parsing; shared by stored documents and HTTP envelopes."""
    if len(raw if isinstance(raw, bytes) else raw.encode("utf-8")) > max_bytes:
        raise ValueError("reference_json_too_large")
    raw_text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    depth, quoted, escaped = 0, False, False
    for char in raw_text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError("reference_json_too_deep")
        elif char in "]}":
            depth -= 1

    def unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("reference_json_duplicate_key")
            result[key] = value
        return result

    return json.loads(raw_text, object_pairs_hook=unique_object)


def decode_input_document(raw: str | bytes) -> InputDocument:
    """Strict stored/wire decoder; HTTP must also bound the surrounding envelope."""
    return InputDocument.model_validate(decode_reference_json(raw))
