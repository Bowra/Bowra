"""Bounded MCP Prompt expansion candidates and encrypted-snapshot codec.

This module performs one remote ``prompts/get`` only when called explicitly.
It does not admit a Turn, persist a result or grant a receiver access. Callers
must bind the exact Prompt to a canonical plan and current worker fence before
storing or using the returned bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, NoReturn

import mcp.types
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.chat_reference_storage import ContentDescriptor
from bowhead_core.chat_references import McpPromptResourceRef, NodeId, decode_reference_json
from bowhead_core.openapi_tool_source import connect_remote_mcp

MAX_MCP_PROMPT_RESULT_BYTES = 256 * 1024
MAX_MCP_PROMPT_WIRE_RESPONSE_BYTES = 1024 * 1024
MAX_MCP_PROMPT_MESSAGES = 64
MAX_MCP_PROMPT_ARGUMENT_VALUE_BYTES = 64 * 1024


class McpPromptExpansionError(ValueError):
    """Finite expansion/codec failure without remote content or arguments."""


def _fail(code: str = "reference_mcp_prompt_expansion_unavailable") -> NoReturn:
    raise McpPromptExpansionError(code) from None


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _document(content: bytes) -> McpPromptExpansionDocument:
    value = decode_reference_json(
        content,
        max_bytes=MAX_MCP_PROMPT_RESULT_BYTES,
        max_depth=12,
    )
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        _fail("reference_mcp_prompt_snapshot_invalid")
    value["messages"] = tuple(value["messages"])
    try:
        document = McpPromptExpansionDocument.model_validate(value)
        _parameters(document.parameters)
        return document
    except Exception:
        _fail("reference_mcp_prompt_snapshot_invalid")


class McpPromptExpansionMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    role: Literal["user", "assistant"]
    text: str


class McpPromptExpansionDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    result_contract: Literal["mcp-prompt-result.v1"] = "mcp-prompt-result.v1"
    resource: McpPromptResourceRef
    parameters: dict[str, str]
    description: Annotated[str, Field(max_length=4096)] = ""
    messages: Annotated[
        tuple[McpPromptExpansionMessage, ...],
        Field(min_length=1, max_length=MAX_MCP_PROMPT_MESSAGES),
    ]


@dataclass(frozen=True, slots=True)
class PreparedMcpPromptExpansion:
    content: bytes = field(repr=False)
    content_hash: str
    materialization_hash: str
    resource: McpPromptResourceRef

    def descriptor(self, *, node_id: NodeId, content_key: str) -> ContentDescriptor:
        try:
            return ContentDescriptor(
                schema_version=1,
                content_key=content_key,
                node_id=node_id,
                resource=self.resource.model_dump(mode="python"),
                content_kind="prompt_result",
                materialization_hash=self.materialization_hash,
                content_hash=self.content_hash,
                byte_count=len(self.content),
            )
        except Exception:
            _fail("reference_mcp_prompt_snapshot_invalid")

    def document(self) -> McpPromptExpansionDocument:
        try:
            return _document(self.content)
        except Exception:
            _fail("reference_mcp_prompt_snapshot_invalid")


def _parameters(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 128:
        _fail("reference_mcp_prompt_parameters_invalid")
    result: dict[str, str] = {}
    total = 2
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 256 or not isinstance(item, str):
            _fail("reference_mcp_prompt_parameters_invalid")
        size = len(item.encode())
        total += len(key.encode()) + size
        if size > MAX_MCP_PROMPT_ARGUMENT_VALUE_BYTES or total > MAX_MCP_PROMPT_RESULT_BYTES:
            _fail("reference_mcp_prompt_parameters_too_large")
        result[key] = item
    if len(_json({"parameters": result})) > MAX_MCP_PROMPT_RESULT_BYTES:
        _fail("reference_mcp_prompt_parameters_too_large")
    return result


def _require_candidate_capacity(
    resource: McpPromptResourceRef,
    parameters: dict[str, str],
) -> None:
    minimum = McpPromptExpansionDocument(
        resource=resource,
        parameters=parameters,
        messages=(McpPromptExpansionMessage(role="user", text=""),),
    )
    if len(_json(minimum.model_dump(mode="json"))) > MAX_MCP_PROMPT_RESULT_BYTES:
        _fail("reference_mcp_prompt_parameters_too_large")


def prepare_mcp_prompt_expansion(
    *,
    resource: McpPromptResourceRef,
    parameters: object,
    result: mcp.types.GetPromptResult,
) -> PreparedMcpPromptExpansion:
    """Detach a text-only Prompt result from SDK objects without trusting metadata."""
    try:
        exact = McpPromptResourceRef.model_validate(
            resource.model_dump(mode="python", warnings="error"),
        )
        arguments = _parameters(parameters)
        if not isinstance(result, mcp.types.GetPromptResult):
            _fail()
        messages: list[McpPromptExpansionMessage] = []
        if not result.messages or len(result.messages) > MAX_MCP_PROMPT_MESSAGES:
            _fail("reference_mcp_prompt_result_invalid")
        for message in result.messages:
            if not isinstance(message, mcp.types.PromptMessage) or not isinstance(
                message.content,
                mcp.types.TextContent,
            ):
                _fail("reference_mcp_prompt_content_unsupported")
            messages.append(
                McpPromptExpansionMessage(
                    role=message.role,
                    text=message.content.text,
                )
            )
        document = McpPromptExpansionDocument(
            resource=exact,
            parameters=arguments,
            description=result.description or "",
            messages=tuple(messages),
        )
        content = _json(document.model_dump(mode="json"))
        if len(content) > MAX_MCP_PROMPT_RESULT_BYTES:
            _fail("reference_mcp_prompt_result_too_large")
        materialization_hash = canonical_fingerprint(
            {
                "contract": document.result_contract,
                "resource": exact.model_dump(mode="json"),
                "parameters": arguments,
            }
        )
        return PreparedMcpPromptExpansion(
            content,
            _hash(content),
            materialization_hash,
            exact,
        )
    except McpPromptExpansionError:
        raise
    except Exception:
        _fail()


def restore_mcp_prompt_expansion(
    content: bytes,
    *,
    descriptor: ContentDescriptor,
) -> PreparedMcpPromptExpansion:
    """Verify decrypted Prompt bytes against an already scoped descriptor."""
    try:
        exact_descriptor = ContentDescriptor.model_validate(
            descriptor.model_dump(mode="python", warnings="error"),
        )
        if (
            exact_descriptor.content_kind != "prompt_result"
            or not isinstance(exact_descriptor.resource, McpPromptResourceRef)
            or not isinstance(content, bytes)
            or len(content) != exact_descriptor.byte_count
            or len(content) > MAX_MCP_PROMPT_RESULT_BYTES
            or _hash(content) != exact_descriptor.content_hash
        ):
            _fail("reference_mcp_prompt_snapshot_invalid")
        document = _document(content)
        materialization_hash = canonical_fingerprint(
            {
                "contract": document.result_contract,
                "resource": document.resource.model_dump(mode="json"),
                "parameters": document.parameters,
            }
        )
        if (
            document.resource != exact_descriptor.resource
            or materialization_hash != exact_descriptor.materialization_hash
            or _json(document.model_dump(mode="json")) != content
        ):
            _fail("reference_mcp_prompt_snapshot_invalid")
        return PreparedMcpPromptExpansion(
            content,
            exact_descriptor.content_hash,
            materialization_hash,
            document.resource,
        )
    except McpPromptExpansionError:
        raise
    except Exception:
        _fail("reference_mcp_prompt_snapshot_invalid")


async def fetch_mcp_prompt_expansion(
    base_url: str,
    *,
    resource: McpPromptResourceRef,
    parameters: object,
    headers: dict[str, str] | None = None,
    timeout: float = 8.0,
    auth: Any = None,
) -> PreparedMcpPromptExpansion:
    """Fetch one exact Prompt under an absolute deadline; never persist or inject it."""
    try:
        exact = McpPromptResourceRef.model_validate(
            resource.model_dump(mode="python", warnings="error"),
        )
        arguments = _parameters(parameters)
        _require_candidate_capacity(exact, arguments)

        async def run() -> mcp.types.GetPromptResult:
            async with connect_remote_mcp(
                base_url,
                headers,
                timeout=timeout,
                call_timeout=timeout,
                auth=auth,
                response_bytes_limit=MAX_MCP_PROMPT_WIRE_RESPONSE_BYTES,
            ) as session:
                capabilities = session.get_server_capabilities()
                if getattr(capabilities, "prompts", None) is None:
                    _fail("reference_mcp_prompt_capability_unavailable")
                return await session.get_prompt(exact.prompt_name, arguments)

        result = await asyncio.wait_for(run(), timeout=timeout)
        return prepare_mcp_prompt_expansion(
            resource=exact,
            parameters=arguments,
            result=result,
        )
    except McpPromptExpansionError:
        raise
    except TimeoutError:
        _fail("reference_mcp_prompt_expansion_timeout")
    except Exception:
        _fail()


__all__ = [
    "MAX_MCP_PROMPT_RESULT_BYTES",
    "MAX_MCP_PROMPT_WIRE_RESPONSE_BYTES",
    "McpPromptExpansionDocument",
    "McpPromptExpansionError",
    "McpPromptExpansionMessage",
    "PreparedMcpPromptExpansion",
    "fetch_mcp_prompt_expansion",
    "prepare_mcp_prompt_expansion",
    "restore_mcp_prompt_expansion",
]
