"""Gateway runtime wrapper for Subagent compatibility heartbeats."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

import anyio
from bowhead_core.subagent_compatibility import (
    ApplicationCompatibilityDecision,
    TeamSubagentApplicationIncompatibleError,
    TeamSubagentCompatibilityStore,
)

SUBAGENT_COMPATIBILITY_HEARTBEAT_SECONDS = 30
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GatewayCompatibilityState:
    status: str
    decision: ApplicationCompatibilityDecision | None = None
    error_code: str | None = None

    def safe_runtime_info(self) -> dict[str, str]:
        """Return health-safe fields without the process instance id."""
        info = {"subagent_compatibility": self.status}
        if self.decision is None:
            return info
        return {
            **info,
            "subagent_schema_version": str(self.decision.schema_version),
            "subagent_reader_version": self.decision.reader_version,
            "subagent_writer_version": self.decision.writer_version,
            "subagent_minimum_reader_version": (self.decision.minimum_reader_version),
            "subagent_minimum_writer_version": (self.decision.minimum_writer_version),
        }


class GatewaySubagentCompatibilityHeartbeat:
    """Fail-closed startup admission plus a recoverable heartbeat loop."""

    def __init__(
        self,
        store: TeamSubagentCompatibilityStore,
        *,
        instance_id: str,
        reader_version: str,
        writer_version: str,
        interval_seconds: float = SUBAGENT_COMPATIBILITY_HEARTBEAT_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.store = store
        self.instance_id = instance_id
        self.reader_version = reader_version
        self.writer_version = writer_version
        self.interval_seconds = interval_seconds
        self.state = GatewayCompatibilityState(status="starting")

    async def admit(self) -> ApplicationCompatibilityDecision:
        """Register the process or raise before any background worker starts."""
        decision = await self.store.record_heartbeat(
            instance_id=self.instance_id,
            reader_version=self.reader_version,
            writer_version=self.writer_version,
        )
        self.state = GatewayCompatibilityState(
            status="ready",
            decision=decision,
        )
        return decision

    async def heartbeat_once(self) -> bool:
        """Refresh once; return false when a raised floor is unrecoverable."""
        try:
            decision = await self.store.record_heartbeat(
                instance_id=self.instance_id,
                reader_version=self.reader_version,
                writer_version=self.writer_version,
            )
        except TeamSubagentApplicationIncompatibleError as exc:
            self.state = GatewayCompatibilityState(
                status="incompatible",
                decision=exc.decision,
                error_code=exc.code,
            )
            _logger.error(
                "Subagent compatibility floor fenced this Gateway process",
            )
            return False
        except Exception:
            self.state = GatewayCompatibilityState(
                status="heartbeat_failed",
                decision=self.state.decision,
                error_code="team_subagent_compatibility_heartbeat_failed",
            )
            _logger.exception(
                "Subagent compatibility heartbeat failed; readiness denied",
            )
            return True
        self.state = GatewayCompatibilityState(
            status="ready",
            decision=decision,
        )
        return True

    async def run(self) -> None:
        """Refresh every 30 seconds until cancelled or permanently fenced."""
        while True:
            await anyio.sleep(self.interval_seconds)
            if not await self.heartbeat_once():
                return

    async def unregister(self) -> None:
        """Remove a graceful shutdown from the live compatibility fleet."""
        await self.store.remove_heartbeat(self.instance_id)
        self.state = GatewayCompatibilityState(
            status="stopped",
            decision=self.state.decision,
        )


def new_gateway_compatibility_instance_id() -> str:
    """Create a per-process opaque id; it is persisted but never exposed."""
    return f"gateway:{uuid4().hex}"
