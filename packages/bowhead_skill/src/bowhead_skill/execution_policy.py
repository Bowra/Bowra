"""Sandbox time policy with a server-owned Team snapshot and deployment ceiling."""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxExecutionPolicy:
    default_timeout_s: int
    max_timeout_s: int


sandbox_execution_policy: ContextVar[SandboxExecutionPolicy | None] = ContextVar("sandbox_execution_policy", default=None)


def _seconds(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def deployment_sandbox_max_timeout_s() -> int:
    return max(5, _seconds("BOWHEAD_SANDBOX_MAX_TIMEOUT", 600))


def sandbox_max_timeout_s() -> int:
    policy = sandbox_execution_policy.get()
    return min(policy.max_timeout_s, deployment_sandbox_max_timeout_s()) if policy else deployment_sandbox_max_timeout_s()


def sandbox_default_timeout_s() -> int:
    policy = sandbox_execution_policy.get()
    if policy:
        return min(sandbox_max_timeout_s(), policy.default_timeout_s)
    return deployment_sandbox_default_timeout_s()


def deployment_sandbox_default_timeout_s() -> int:
    return min(deployment_sandbox_max_timeout_s(), max(5, _seconds("BOWHEAD_SANDBOX_DEFAULT_TIMEOUT_S", 120)))


def skill_wall_timeout_s() -> int:
    return max(30, _seconds("BOWHEAD_SKILL_WALL_TIMEOUT_S", deployment_sandbox_max_timeout_s() + 60))
