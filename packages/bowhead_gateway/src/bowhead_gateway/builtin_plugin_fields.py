"""Shared handling of the optional team Agent definition field ``builtin_plugin_ids``.

Capability Center tech design §6: drafts, Coach and the chat editor accept unversioned
``builtin:<name>`` selections. The field is omitted when absent, so stored definitions and their
hashes do not change for Agents that never select a built-in Plugin.
"""

from __future__ import annotations

from typing import Any

from bowhead_core.system_plugins import BuiltinPluginSelectionError, selected_builtin_plugin_ids
from pydantic import BaseModel, model_serializer

RESOURCE_SELECTION_KEYS = ("skill_grants", "capability_ids", "builtin_plugin_ids")


def validate_builtin_plugin_ids(value: list[str] | None) -> list[str] | None:
    """Validate with the core's rules; errors carry the core's stable codes.

    An empty list means "nothing selected" and is normalized to absent, so it neither changes the
    definition hash nor trips the switch-off check.
    """
    if not value:
        return None
    try:
        selected_builtin_plugin_ids({"builtin_plugin_ids": value})
    except BuiltinPluginSelectionError as exc:
        raise ValueError(exc.code) from exc
    return list(value)


def has_resource_selection(definition: Any) -> bool:
    """True when a definition selects Skills or Plugins, so it may have no directly selected Tools."""
    if isinstance(definition, BaseModel):
        return any(getattr(definition, key, None) for key in RESOURCE_SELECTION_KEYS)
    return any((definition or {}).get(key) for key in RESOURCE_SELECTION_KEYS)


class OmitAbsentBuiltinPlugins(BaseModel):
    """Drop ``builtin_plugin_ids`` from dumps when it was not given."""

    @model_serializer(mode="wrap")
    def _omit_absent_builtin_plugins(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if isinstance(data, dict) and data.get("builtin_plugin_ids") is None:
            data.pop("builtin_plugin_ids", None)
        return data


def expand_selected_builtin_plugins(definition: dict[str, Any], *, strict: bool = True) -> dict[str, Any]:
    """Expand built-in Plugin selections after team Plugins, as publication and run admission do.

    Unchanged when nothing is selected. Selection errors are
    ``ValueError`` subclasses carrying the core's codes.
    """
    from bowhead_core.system_plugins import expand_builtin_plugins

    if not definition.get("builtin_plugin_ids"):
        return definition
    return expand_builtin_plugins(definition, strict=strict)
