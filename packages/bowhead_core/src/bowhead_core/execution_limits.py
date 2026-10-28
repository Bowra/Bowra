"""Team sandbox and HTML limits, independent of agent loop feature switches."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from bowhead_skill.execution_policy import (
    SandboxExecutionPolicy,
    deployment_sandbox_default_timeout_s,
    deployment_sandbox_max_timeout_s,
    sandbox_execution_policy,
    skill_wall_timeout_s,
)

from bowhead_core.html_preview_policy import MAX_HTML_PREVIEW_BYTES, deployment_html_preview_bytes, html_preview_policy

logger = logging.getLogger(__name__)
SETTINGS_KEY = "execution_limits"


@dataclass(frozen=True)
class ExecutionLimits:
    sandbox_default_timeout_s: int = field(metadata={"description": "Default sandbox execution duration in seconds; cannot exceed the Team maximum."})
    sandbox_max_timeout_s: int = field(metadata={"description": "Maximum sandbox execution duration in seconds, bounded by the deployment ceiling."})
    html_max_bytes: int = field(
        metadata={"description": "Maximum bytes in a delivered HTML artifact, shared by server delivery and browser preview."}
    )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def sandbox_ceiling_s() -> int:
    """Leave transport/cleanup time inside an explicitly shorter host deadline."""
    return max(5, min(deployment_sandbox_max_timeout_s(), skill_wall_timeout_s() - 60))


def defaults() -> ExecutionLimits:
    maximum = sandbox_ceiling_s()
    return ExecutionLimits(min(maximum, deployment_sandbox_default_timeout_s()), maximum, deployment_html_preview_bytes())


class ExecutionLimitsInvalid(ValueError):
    def __init__(self, field: str, minimum: int, maximum: int) -> None:
        self.field, self.minimum, self.maximum = field, minimum, maximum
        super().__init__(f"{field} must be an integer between {minimum} and {maximum}")


def validate(payload: dict[str, Any]) -> ExecutionLimits:
    def integer(field: str, minimum: int, maximum: int) -> int:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ExecutionLimitsInvalid(field, minimum, maximum)
        return value

    maximum = integer("sandbox_max_timeout_s", 5, sandbox_ceiling_s())
    return ExecutionLimits(
        sandbox_default_timeout_s=integer("sandbox_default_timeout_s", 5, maximum),
        sandbox_max_timeout_s=maximum,
        html_max_bytes=integer("html_max_bytes", 1, MAX_HTML_PREVIEW_BYTES),
    )


def parse_stored(raw: str | None) -> ExecutionLimits:
    fallback = defaults()
    if raw is None:
        return fallback
    try:
        payload = json.loads(raw)
        if payload is None:
            return fallback
        # A reduced deployment ceiling clamps an existing Team value immediately.
        for field in ("sandbox_default_timeout_s", "sandbox_max_timeout_s", "html_max_bytes"):
            if isinstance(payload[field], bool) or not isinstance(payload[field], int) or payload[field] < 1:
                raise ValueError("Invalid stored execution limit")
        maximum = min(sandbox_ceiling_s(), max(5, payload["sandbox_max_timeout_s"]))
        return ExecutionLimits(
            min(maximum, max(5, payload["sandbox_default_timeout_s"])), maximum, min(payload["html_max_bytes"], MAX_HTML_PREVIEW_BYTES)
        )
    except (ValueError, TypeError, KeyError):
        logger.warning("execution_limits.stored_value_invalid")
        return fallback


async def load_team_limits(store: Any, team_id: str | None) -> ExecutionLimits:
    if store is None or not team_id:
        return defaults()
    # A failed policy read must not silently replace a saved Team ceiling.
    return parse_stored(await store.get(team_id, SETTINGS_KEY))


@contextmanager
def execution_limits_scope(limits: ExecutionLimits | None) -> Iterator[None]:
    if limits is None:
        yield
        return
    sandbox_token = sandbox_execution_policy.set(SandboxExecutionPolicy(limits.sandbox_default_timeout_s, limits.sandbox_max_timeout_s))
    html_token = html_preview_policy.set(limits.html_max_bytes)
    try:
        yield
    finally:
        html_preview_policy.reset(html_token)
        sandbox_execution_policy.reset(sandbox_token)
