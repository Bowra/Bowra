"""Canonical bounded metadata shape for MCP prompts/list caches."""

from __future__ import annotations

import json
from typing import Any

MAX_MCP_PROMPT_COUNT = 2000
MAX_MCP_PROMPT_ARGUMENTS = 128
MAX_MCP_PROMPT_CACHE_BYTES = 2 * 1024 * 1024


def validate_mcp_prompt_cache(values: object) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > MAX_MCP_PROMPT_COUNT:
        raise ValueError("invalid MCP Prompt metadata")
    prompts: list[dict[str, Any]] = []
    names: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "name",
            "title",
            "description",
            "arguments",
        }:
            raise ValueError("invalid MCP Prompt metadata")
        name = value.get("name")
        title = value.get("title")
        description = value.get("description")
        arguments = value.get("arguments")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 512
            or name in names
            or not isinstance(title, str)
            or len(title) > 256
            or not isinstance(description, str)
            or len(description) > 4096
            or not isinstance(arguments, list)
            or len(arguments) > MAX_MCP_PROMPT_ARGUMENTS
        ):
            raise ValueError("invalid MCP Prompt metadata")
        names.add(name)
        seen_arguments: set[str] = set()
        projected_arguments: list[dict[str, Any]] = []
        for argument in arguments:
            if not isinstance(argument, dict) or set(argument) != {
                "name",
                "description",
                "required",
            }:
                raise ValueError("invalid MCP Prompt argument metadata")
            argument_name = argument.get("name")
            argument_description = argument.get("description")
            required = argument.get("required")
            if (
                not isinstance(argument_name, str)
                or not argument_name
                or len(argument_name) > 256
                or argument_name in seen_arguments
                or not isinstance(argument_description, str)
                or len(argument_description) > 2048
                or type(required) is not bool
            ):
                raise ValueError("invalid MCP Prompt argument metadata")
            seen_arguments.add(argument_name)
            projected_arguments.append(
                {
                    "name": argument_name,
                    "description": argument_description,
                    "required": required,
                }
            )
        prompts.append(
            {
                "name": name,
                "title": title,
                "description": description,
                "arguments": projected_arguments,
            }
        )
    encoded = json.dumps(
        prompts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if len(encoded) > MAX_MCP_PROMPT_CACHE_BYTES:
        raise ValueError("MCP Prompt metadata is too large")
    return prompts


__all__ = [
    "MAX_MCP_PROMPT_ARGUMENTS",
    "MAX_MCP_PROMPT_CACHE_BYTES",
    "MAX_MCP_PROMPT_COUNT",
    "validate_mcp_prompt_cache",
]
