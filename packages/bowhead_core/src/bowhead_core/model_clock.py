"""Core access to the model's clock (see ``bowhead_router.model_clock``).

Prompts and tool advice read "today" only through :func:`model_today` /
:func:`model_now`. Turn execution binds the viewer zone admitted with the turn
via :func:`turn_clock_scope`; anything outside such a turn uses the deployment
clock. Zone names come from clients and stored snapshots, so they are resolved
with :func:`bowhead_core.iana_time_zone.load_iana_time_zone`; an unknown name is
treated as absent.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any
from zoneinfo import ZoneInfo

from bowhead_router.model_clock import (
    deploy_time_zone,
    deploy_tz_offset_hours,
    model_clock_zone,
    model_now,
    model_time_zone,
    model_today,
    turn_time_zone,
)

from bowhead_core.iana_time_zone import load_iana_time_zone

__all__ = [
    "deploy_time_zone",
    "deploy_tz_offset_hours",
    "known_time_zone_name",
    "model_clock_zone",
    "model_now",
    "model_time_zone",
    "model_today",
    "request_time_zone_name",
    "turn_clock_scope",
    "turn_time_zone",
    "turn_time_zone_name",
    "viewer_clock_scope",
]


def viewer_clock_scope(time_zone_name: object) -> AbstractContextManager[None]:
    """Bind the named viewer zone as the model clock; an unknown or empty name binds the deployment clock."""
    return model_clock_zone(load_iana_time_zone(time_zone_name))


def known_time_zone_name(value: object) -> str:
    """``value`` as a known IANA zone name, or ``""``."""
    zone = load_iana_time_zone(value)
    return zone.key if zone is not None else ""


def request_time_zone_name(request: Any) -> str:
    """The viewer zone admitted with a turn request, or ``""``.

    ``language_context.time_zone`` is the carrier when the turn has a language
    snapshot; ``viewer_time_zone`` carries it when it does not (i18n disabled).
    """
    return known_time_zone_name(getattr(getattr(request, "language_context", None), "time_zone", "")) or known_time_zone_name(
        getattr(request, "viewer_time_zone", "")
    )


def turn_clock_scope(request: Any) -> AbstractContextManager[None]:
    """The model clock for executing ``request``: its admitted viewer zone, else the deployment clock."""
    return viewer_clock_scope(request_time_zone_name(request))


def turn_time_zone_name() -> str:
    """The IANA name of the viewer zone bound to the current turn, or ``""`` outside a viewer turn."""
    zone = turn_time_zone()
    return zone.key if isinstance(zone, ZoneInfo) else ""
