"""Versioned code-shipped Tool/Skill resource bundles (built-in Plugins).

These are system Plugins, independent of historical Outcome cap_* roles.
Compilation produces the same exact Skill grants and Tool whitelist used by the
ordinary execution snapshot. Bundles do not confer caller permissions.

The registry lives in ``agents/manifests/plugins/builtin-v1.yaml``; see
docs/design/capability-center-plugins-tech-design.md §5.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bowhead_core.agents._generated.tool_kinds import TOOL_KINDS

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent / "agents" / "manifests" / "plugins" / "builtin-v1.yaml"


Availability = Literal["datarc_connection", "web_search_provider", "sandbox", "image_model", "google_workspace", "email_service"]
_LOCALES = frozenset({"zh-CN", "en-US", "de-DE", "fr-FR"})


class PluginListing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    # Reviewed picker copy for general Plugins: when to choose it and one example request.
    use_when: str | None = Field(default=None, min_length=1)
    example: str | None = Field(default=None, min_length=1)


class BuiltinPlugin(BaseModel):
    """One immutable version of a built-in Plugin.

    `general` Plugins bundle user-visible Tools and may attach prompt modules; `settings` Plugins
    carry the configuration Agent's parts and one exact settings Skill grant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: int = Field(ge=1)
    family: Literal["general", "settings"]
    selectable: bool
    listing: dict[str, PluginListing] | None = None
    tools: tuple[str, ...] = ()
    parts: tuple[str, ...] = ()
    skill_revision: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_modules: tuple[str, ...] = ()
    availability: tuple[Availability, ...] = ()

    @field_validator("tools", "parts", "prompt_modules", "availability")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item for item in value):
            raise ValueError("Plugin entries must be unique and non-empty")
        return value

    @field_validator("prompt_modules")
    @classmethod
    def _module_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for ref in value:
            module_id, sep, version = ref.rpartition("@")
            if not sep or not module_id or not version.isdigit() or int(version) < 1:
                raise ValueError(f"prompt module {ref!r} must be written as <id>@<version>")
        return value

    @model_validator(mode="after")
    def _family_shape(self) -> BuiltinPlugin:
        for tool in self.tools:
            if TOOL_KINDS.get(tool) != "tool":
                raise ValueError(f"{tool!r} is not a user-visible Tool")
        for part in self.parts:
            if TOOL_KINDS.get(part) != "part":
                raise ValueError(f"{part!r} is not a system part")
        if self.family == "general":
            if not self.tools or self.parts or self.skill_revision is not None:
                raise ValueError("a general Plugin has Tools and no parts or Skill grant")
            if self.listing is None or set(self.listing) != _LOCALES:
                raise ValueError(f"a general Plugin needs a listing for {sorted(_LOCALES)}")
            if any(item.use_when is None or item.example is None for item in self.listing.values()):
                raise ValueError("a general Plugin listing needs use_when and example in every locale")
            if not self.selectable:
                raise ValueError("a general Plugin is selectable: selecting it is the Owner's grant")
        elif not self.parts or self.skill_revision is None or self.prompt_modules:
            raise ValueError("a settings Plugin has parts and one Skill grant and no prompt modules")
        return self

    @property
    def ref(self) -> str:
        return f"builtin:{self.name}@{self.version}"

    @property
    def resources(self) -> tuple[str, ...]:
        """Tools first, then parts, in declaration order."""
        return self.tools + self.parts

    def skill_grant(self) -> dict[str, Any] | None:
        if self.skill_revision is None:
            return None
        return {
            "skill_ref": {"kind": "builtin", "id": "builtin:" + self.name, "name": self.name},
            "permissions": ["read"],
            "revision": self.skill_revision,
            "distribution": "reference",
        }


class _Registry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    plugins: tuple[BuiltinPlugin, ...]


def _validate(plugins: tuple[BuiltinPlugin, ...]) -> dict[str, BuiltinPlugin]:
    by_ref: dict[str, BuiltinPlugin] = {}
    for plugin in plugins:
        if plugin.ref in by_ref:
            raise ValueError(f"duplicate built-in Plugin {plugin.ref}")
        by_ref[plugin.ref] = plugin
    # A Tool belongs to at most one Plugin (comparing each Plugin's latest version), so disabling a
    # Plugin has one unambiguous effect.
    latest: dict[str, BuiltinPlugin] = {}
    for plugin in plugins:
        if plugin.name not in latest or plugin.version > latest[plugin.name].version:
            latest[plugin.name] = plugin
    owner: dict[str, str] = {}
    for plugin in latest.values():
        for tool in plugin.tools:
            if tool in owner:
                raise ValueError(f"Tool {tool!r} is in both {owner[tool]} and {plugin.name}")
            owner[tool] = plugin.name
    return by_ref


def load_registry(path: Path = _REGISTRY_PATH) -> dict[str, BuiltinPlugin]:
    """Load and validate a registry file; invalid content fails loudly."""
    registry = _Registry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    return _validate(registry.plugins)


@lru_cache(maxsize=1)
def _registry() -> dict[str, BuiltinPlugin]:
    return load_registry()


def builtin_plugin(ref: str) -> BuiltinPlugin:
    """Return the exact Plugin version for ``builtin:<name>@<version>``."""
    try:
        return _registry()[ref]
    except KeyError:
        raise ValueError("unknown system Plugin") from None


def builtin_plugins() -> tuple[BuiltinPlugin, ...]:
    return tuple(_registry().values())


def resolve_system_plugins(refs: list[str], tools: list[str], grants: list[dict[str, Any]] | None) -> tuple[list[str], list[dict[str, Any]] | None]:
    """Compile pinned resource selections; unknown references and conflicts fail closed."""
    from bowhead_core.system_skill_snapshot import parse_system_manifest_skill_grants

    if not refs:
        return tools, grants
    if len(set(refs)) != len(refs):
        raise ValueError("duplicate system Plugin")
    resolved_tools = list(dict.fromkeys(tools))
    resolved_grants = list(grants or [])
    for ref in refs:
        plugin = builtin_plugin(ref)
        for tool in plugin.resources:
            if tool not in resolved_tools:
                resolved_tools.append(tool)
        grant = plugin.skill_grant()
        if grant is None:
            continue
        existing = [item for item in resolved_grants if item["skill_ref"]["id"] == grant["skill_ref"]["id"]]
        if existing and existing != [grant]:
            raise ValueError("conflicting system resource Skill grant")
        if not existing:
            resolved_grants.append(grant)
    return resolved_tools, [grant.to_canonical_dict() for grant in parse_system_manifest_skill_grants(resolved_grants)]


class BuiltinPluginSelectionError(ValueError):
    """A team Agent's ``builtin_plugin_ids`` cannot be used; ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_MAX_SELECTED_BUILTIN_PLUGINS = 16


def latest_builtin_plugin(name: str) -> BuiltinPlugin | None:
    versions = [plugin for plugin in builtin_plugins() if plugin.name == name]
    return max(versions, key=lambda plugin: plugin.version) if versions else None


def selected_builtin_plugin_ids(definition: Mapping[str, Any], *, strict: bool = True) -> tuple[str, ...]:
    """Validate a team Agent's ``builtin_plugin_ids`` (unversioned ``builtin:<name>``, at most 16).

    ``strict`` (saving and publishing) rejects unknown or unselectable Plugins. Run admission passes
    ``strict=False`` and skips them, so an Agent keeps running when a Plugin later stops being
    selectable.
    """
    value = definition.get("builtin_plugin_ids")
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or len(value) > _MAX_SELECTED_BUILTIN_PLUGINS
        or len(set(value)) != len(value)
        or not all(isinstance(item, str) and item.startswith("builtin:") and "@" not in item for item in value)
    ):
        raise BuiltinPluginSelectionError("builtin_plugin_ids_invalid")
    usable: list[str] = []
    for item in value:
        plugin = latest_builtin_plugin(item.removeprefix("builtin:"))
        code = "builtin_plugin_unknown" if plugin is None else None if plugin.selectable else "builtin_plugin_not_selectable"
        if code is None:
            usable.append(item)
        elif strict:
            raise BuiltinPluginSelectionError(code)
        else:
            logger.warning("builtin_plugin.skipped ref=%s reason=%s", item, code)
    return tuple(usable)


def expand_builtin_plugins(definition: Mapping[str, Any], *, strict: bool = True) -> dict[str, Any]:
    """Add the latest version of each selected built-in Plugin to a team Agent definition.

    Plugins float to their latest version (PRD §7.4). The resolved versions are recorded in
    ``resolved_builtin_plugins``; a Plugin with prompt modules turns on ``capability_protocol_block``.
    """
    ids = selected_builtin_plugin_ids(definition, strict=strict)
    expanded = dict(definition)
    if not ids:
        return expanded
    plugin_tools: list[str] = []
    resolved: list[dict[str, Any]] = []
    guidance = False
    for item in ids:
        plugin = latest_builtin_plugin(item.removeprefix("builtin:"))
        if plugin is None:  # validated above; the registry is immutable at run time
            raise BuiltinPluginSelectionError("builtin_plugin_unknown")
        plugin_tools.extend(plugin.tools)
        guidance = guidance or bool(plugin.prompt_modules)
        resolved.append({"name": plugin.name, "version": plugin.version})
    expanded["tool_refs"] = sorted({*(definition.get("tool_refs") or []), *plugin_tools})
    expanded["resolved_builtin_plugins"] = resolved
    if guidance:
        expanded["capability_protocol_block"] = True
    if "python_sandbox" in plugin_tools:
        # Selecting a Plugin that brings the Python sandbox requests its Skill, as selecting the Tool does.
        # Resolved on every expansion and never written back, so stored revisions and their hashes stay as they are.
        from bowhead_core.agent_dependency_compiler import with_python_sandbox_skill_grant

        expanded = with_python_sandbox_skill_grant(expanded)
    return expanded
