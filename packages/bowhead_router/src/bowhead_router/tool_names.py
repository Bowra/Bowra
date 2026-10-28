"""Provider-safe tool names on the wire.

OpenAI (Chat Completions and Responses), Anthropic and DeepSeek require tool and
function-call names to match ``^[a-zA-Z0-9_-]{1,64}$``. Bowhead canonical names can
carry namespace dots (``teammcp.<connection>.<operation>``), which those providers
reject with HTTP 400 before the model runs.

Names that already comply are sent unchanged, so built-in tools keep their exact
names. Other names are rewritten deterministically (``.`` → ``__``, any other
disallowed character → ``_``, over-long names truncated with a digest suffix).
Inbound tool calls are mapped back through a per-request table built from the
request's own tool definitions and history, never by reversing the string rewrite,
so a canonical name that itself contains ``__`` cannot be corrupted.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bowhead_router.types import LLMRequest

logger = logging.getLogger(__name__)

WIRE_TOOL_NAME_MAX_LEN = 64
_WIRE_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")
_DIGEST_LEN = 8


def to_wire_tool_name(name: str) -> str:
    """Return a provider-safe name; compliant names are returned unchanged."""
    if _WIRE_TOOL_NAME.fullmatch(name):
        return name
    wire = _DISALLOWED.sub("_", name.replace(".", "__")) or "tool"
    if len(wire) > WIRE_TOOL_NAME_MAX_LEN:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
        wire = f"{wire[: WIRE_TOOL_NAME_MAX_LEN - _DIGEST_LEN - 1]}_{digest}"
    return wire


class ToolNameMap:
    """Maps wire names returned by a provider back to the request's canonical names."""

    def __init__(self, canonical_names: Iterable[str]) -> None:
        self._canonical: dict[str, str] = {}
        for name in canonical_names:
            if not name:
                continue
            wire = to_wire_tool_name(name)
            existing = self._canonical.get(wire)
            if existing is None or existing == name:
                self._canonical[wire] = name
            elif name == wire:
                # A compliant name is its own wire form and must keep resolving to itself.
                self._canonical[wire] = name
            elif existing != wire:
                logger.warning("tool_names.wire_collision wire=%s kept=%s dropped=%s", wire, existing, name)

    @classmethod
    def for_request(cls, req: LLMRequest) -> ToolNameMap:
        names = [tool.name for tool in req.tools or []]
        for message in req.messages or []:
            names.extend(call.name for call in message.tool_calls or [])
        return cls(names)

    def to_canonical(self, wire_name: str) -> str:
        """Unknown names pass through so the dispatcher reports them as unknown tools."""
        return self._canonical.get(wire_name, wire_name)
