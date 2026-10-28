"""Canonical policy for user-visible personal Memory.

The shared ``memory_entries`` table also contains operational state.  Product
surfaces and prompt recall must use this allowlist instead of inferring
visibility from a raw kind or JSON shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from bowhead_core.user_memory import CURRENT_MEMORY_POLICY_VERSION, MemoryRole


@dataclass(frozen=True)
class ResolvedMemoryPolicy:
    display_type: str | None
    user_visible: bool
    prompt_recall: bool
    regular_delete_reset: bool
    memory_export: bool
    full_privacy_erasure: bool
    display_schema: str | None
    edit_schema: str | None


_HIDDEN_POLICY = ResolvedMemoryPolicy(
    display_type=None,
    user_visible=False,
    prompt_recall=False,
    regular_delete_reset=False,
    memory_export=False,
    full_privacy_erasure=True,
    display_schema=None,
    edit_schema=None,
)

_VISIBLE_POLICIES: dict[tuple[str, str], ResolvedMemoryPolicy] = {
    ("preference", "text_v1"): ResolvedMemoryPolicy(
        display_type="preference",
        user_visible=True,
        prompt_recall=True,
        regular_delete_reset=True,
        memory_export=True,
        full_privacy_erasure=True,
        display_schema="text_v1",
        edit_schema="text_v1",
    ),
    ("preference", "boolean_v1"): ResolvedMemoryPolicy(
        display_type="preference",
        user_visible=True,
        prompt_recall=True,
        regular_delete_reset=True,
        memory_export=True,
        full_privacy_erasure=True,
        display_schema="boolean_v1",
        edit_schema="boolean_v1",
    ),
    ("preference", "number_v1"): ResolvedMemoryPolicy(
        display_type="preference",
        user_visible=True,
        prompt_recall=True,
        regular_delete_reset=True,
        memory_export=True,
        full_privacy_erasure=True,
        display_schema="number_v1",
        edit_schema="number_v1",
    ),
    ("constraint", "text_v1"): ResolvedMemoryPolicy(
        display_type="work_rule",
        user_visible=True,
        prompt_recall=True,
        regular_delete_reset=True,
        memory_export=True,
        full_privacy_erasure=True,
        display_schema="text_v1",
        edit_schema="text_v1",
    ),
    ("constraint", "boolean_v1"): ResolvedMemoryPolicy(
        display_type="work_rule",
        user_visible=True,
        prompt_recall=True,
        regular_delete_reset=True,
        memory_export=True,
        full_privacy_erasure=True,
        display_schema="boolean_v1",
        edit_schema="boolean_v1",
    ),
    ("note", "note_text_v1"): ResolvedMemoryPolicy(
        display_type="background",
        user_visible=True,
        prompt_recall=True,
        regular_delete_reset=True,
        memory_export=True,
        full_privacy_erasure=True,
        display_schema="text_v1",
        edit_schema="text_v1",
    ),
}


def resolve_memory_policy(
    *,
    memory_role: str,
    memory_policy_version: int,
    kind: str,
    value_schema: str | None,
    value: Any,
) -> ResolvedMemoryPolicy:
    """Resolve a fail-closed policy for one structured Memory row."""

    if memory_role != MemoryRole.PRODUCT_MEMORY or memory_policy_version != CURRENT_MEMORY_POLICY_VERSION or value_schema is None:
        return _HIDDEN_POLICY
    policy = _VISIBLE_POLICIES.get((kind, value_schema), _HIDDEN_POLICY)
    if not _value_matches_schema(value, value_schema):
        return _HIDDEN_POLICY
    return policy


def _value_matches_schema(value: Any, schema: str) -> bool:
    if schema in {"text_v1", "note_text_v1"}:
        return isinstance(value, str)
    if schema == "boolean_v1":
        return isinstance(value, bool)
    if schema == "number_v1":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return False


__all__ = [
    "ResolvedMemoryPolicy",
    "resolve_memory_policy",
]
