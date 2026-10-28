"""Consumer-safe product metadata for system Agents.

System manifests are the authority: each listed Agent declares its name,
summary, capability tags, display order and allowed Team overrides in its
``listing`` block. This module reads them through a lazy cached accessor, so
importing it never bootstraps the Agent registry.

Localized names and summaries come from the controlled marketplace message
table (``agent_marketplace_search_text``), which is contract-tested against the
frontend message catalogs, so every surface renders the same text.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

SYSTEM_AGENT_CAPABILITIES: dict[str, tuple[str, str]] = {
    "cap_analysis": ("Analyze and handle business work", "analysis"),
    "cap_compute": ("Run isolated computations", "analysis"),
    "cap_data_query": ("Query and analyze data", "data"),
    "cap_document": ("Create or edit documents", "document"),
    "cap_knowledge_search": ("Search team knowledge", "knowledge"),
    "cap_presentation": ("Create presentations", "presentation"),
    "cap_report": ("Write reports", "document"),
    "cap_visualize": ("Create charts and visuals", "visual"),
    "cap_web_research": ("Research the public web", "research"),
}

# Names reserved for system Agents that have no listing: Lead's localized name
# and future product names.
_UNLISTED_RESERVED_DISPLAY_NAMES: dict[str, tuple[str, ...]] = {"lead": ("主控",)}
SYSTEM_FUTURE_RESERVED_DISPLAY_NAMES = frozenset({"HR"})


@functools.cache
def _system_manifest_listings() -> tuple[Mapping[str, Mapping[str, Any]], frozenset[str], Mapping[str, frozenset[str]]]:
    """Read every system manifest once: listed Agents, all system names, override policy."""
    from bowhead_core.agents.manifest import TEAM_OVERRIDE_FIELDS
    from bowhead_core.agents.registry import AgentRegistry

    registry = AgentRegistry.bootstrap()
    listed: list[tuple[int, str, Mapping[str, Any]]] = []
    overrides: dict[str, frozenset[str]] = {}
    names = frozenset(registry.list_all())
    for name in names:
        listing = registry.get(name).listing
        if listing is None:
            continue
        allowed = frozenset(TEAM_OVERRIDE_FIELDS if listing.team_overrides is None else listing.team_overrides)
        overrides[name] = allowed
        if not listing.listed:
            continue
        listed.append(
            (
                listing.order,
                name,
                MappingProxyType(
                    {
                        "display_name": listing.display_name,
                        "summary": listing.summary,
                        "capabilities": tuple(listing.capabilities),
                        "order": listing.order,
                        "team_overrides": allowed,
                    }
                ),
            )
        )
    listed.sort(key=lambda row: (row[0], row[1]))
    return (
        MappingProxyType({name: value for _, name, value in listed}),
        names,
        MappingProxyType(overrides),
    )


def listed_system_agents() -> Mapping[str, Mapping[str, Any]]:
    """Listed system Agents in display order (marketplace, @ list, Team settings)."""
    return _system_manifest_listings()[0]


def system_manifest_names() -> frozenset[str]:
    """Every system manifest name, listed or not."""
    return _system_manifest_listings()[1]


def system_agent_team_override_fields(name: str) -> frozenset[str]:
    """Team override fields a system Agent's manifest allows; every field when it declares none."""
    from bowhead_core.agents.manifest import TEAM_OVERRIDE_FIELDS

    allowed = _system_manifest_listings()[2].get(name)
    return allowed if allowed is not None else frozenset(TEAM_OVERRIDE_FIELDS)


class _ListedSystemAgents(Mapping[str, Mapping[str, Any]]):
    """Read-only view of ``listed_system_agents()`` that loads on first use."""

    def __getitem__(self, name: str) -> Mapping[str, Any]:
        return listed_system_agents()[name]

    def __iter__(self) -> Iterator[str]:
        return iter(listed_system_agents())

    def __len__(self) -> int:
        return len(listed_system_agents())

    def __repr__(self) -> str:
        return f"SYSTEM_AGENT_LISTINGS({list(self)!r})"


SYSTEM_AGENT_LISTINGS: Mapping[str, Mapping[str, Any]] = _ListedSystemAgents()


def _message_key(name: str, field: str) -> str:
    return f"system_{name.replace('-', '_')}_{field}"


def system_agent_default_display_name(name: str) -> str:
    listing = listed_system_agents().get(name)
    if listing is not None:
        return str(listing["display_name"])
    return name.replace("-", " ").title()


def _localized(name: str, field: str, locale: str) -> str | None:
    from bowhead_core.agent_marketplace_search_text import MARKETPLACE_SEARCH_TEXT

    return MARKETPLACE_SEARCH_TEXT.get(_message_key(name, field), {}).get(locale)


def system_agent_localized_display_name(name: str, locale: str) -> str:
    return _localized(name, "name", locale) or system_agent_default_display_name(name)


def system_agent_localized_summary(name: str, locale: str) -> str:
    listing = listed_system_agents().get(name)
    fallback = str(listing["summary"]) if listing is not None else ""
    return _localized(name, "summary", locale) or fallback


def system_agent_reserved_display_names(name: str) -> frozenset[str]:
    from bowhead_core.agent_marketplace_search_text import MARKETPLACE_SEARCH_TEXT

    return frozenset(
        {
            name,
            name.replace("-", " ").title(),
            system_agent_default_display_name(name),
            *MARKETPLACE_SEARCH_TEXT.get(_message_key(name, "name"), {}).values(),
            *_UNLISTED_RESERVED_DISPLAY_NAMES.get(name, ()),
        },
    )


def system_reserved_display_names() -> frozenset[str]:
    """Names no Team Agent may use: every rendering of a listed system Agent, plus Lead and future names."""
    return frozenset(
        {
            *SYSTEM_FUTURE_RESERVED_DISPLAY_NAMES,
            *(value for name in (*listed_system_agents(), *_UNLISTED_RESERVED_DISPLAY_NAMES) for value in system_agent_reserved_display_names(name)),
        },
    )


__all__ = [
    "SYSTEM_AGENT_CAPABILITIES",
    "SYSTEM_AGENT_LISTINGS",
    "SYSTEM_FUTURE_RESERVED_DISPLAY_NAMES",
    "listed_system_agents",
    "system_agent_default_display_name",
    "system_agent_localized_display_name",
    "system_agent_localized_summary",
    "system_agent_reserved_display_names",
    "system_agent_team_override_fields",
    "system_manifest_names",
    "system_reserved_display_names",
]
