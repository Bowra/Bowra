"""Durable chat execution rollout and protocol selection truth."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bowhead_core.terminal_protocol import (
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
)
from bowhead_core.terminal_protocol_store import SessionProtocolFacts

DURABLE_CHAT_EXECUTION_FLAG = "FEATURE_DURABLE_CHAT_EXECUTION_V1"
DURABLE_CHAT_EXECUTION_TEAM_IDS_FLAG = f"{DURABLE_CHAT_EXECUTION_FLAG}_TEAM_IDS"
DURABLE_CHAT_EXECUTION_PERCENTAGE_FLAG = f"{DURABLE_CHAT_EXECUTION_FLAG}_PCT"

# The rollout policy is shipped before fresh v1 admission. Configuration alone
# must not cross this release boundary; later slices remove the latch only after
# the Web controller and effect/control parity gates are complete.
_FRESH_TERMINAL_V1_ADMISSION_RELEASED = False
_ROLLOUT_SALT = "durable-chat-execution-v1"
_PERCENTAGE_RE = re.compile(r"(?:0|[1-9][0-9]{0,2})\Z")


class DurableChatExecutionMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ROLLOUT = "rollout"
    REQUIRED = "required"
    INVALID = "invalid"


class ChatExecutionWriteProtocol(StrEnum):
    TERMINAL_V1 = "terminal_v1"
    LEGACY_V0 = "legacy_v0"
    REFRESH_REQUIRED = "refresh_required"
    UNAVAILABLE = "unavailable"


class PersistedChatExecutionProtocol(StrEnum):
    TERMINAL_V1 = "terminal_v1"
    LEGACY_V0 = "legacy_v0"


@dataclass(frozen=True, slots=True)
class DurableChatExecutionConfig:
    mode: DurableChatExecutionMode
    team_ids: frozenset[str]
    percentage: int
    config_generation: str

    @property
    def valid(self) -> bool:
        return self.mode is not DurableChatExecutionMode.INVALID

    def team_eligible(self, team_id: str | None) -> bool:
        if not team_id or self.mode not in {
            DurableChatExecutionMode.SHADOW,
            DurableChatExecutionMode.ROLLOUT,
            DurableChatExecutionMode.REQUIRED,
        }:
            return False
        if self.mode is DurableChatExecutionMode.REQUIRED:
            return True
        if team_id in self.team_ids:
            return True
        return self.percentage == 100 or (self.percentage > 0 and stable_durable_chat_team_bucket(team_id) < self.percentage)


@dataclass(frozen=True, slots=True)
class ChatExecutionAdmissionDecision:
    write_protocol: ChatExecutionWriteProtocol
    reason: str
    cohort_eligible: bool


@dataclass(frozen=True, slots=True)
class ChatExecutionWriterReadiness:
    existing_writer_ready: bool
    fresh_writer_ready: bool
    readiness_generation: str


def _config_generation(
    mode: DurableChatExecutionMode,
    team_ids: frozenset[str],
    percentage: int,
) -> str:
    payload = "\0".join(
        (
            mode.value,
            ",".join(sorted(team_ids)),
            str(percentage),
        )
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return f"durable_chat_v1_{digest}"


def parse_durable_chat_execution_config(
    raw: str | None,
    team_ids_raw: str | None,
    percentage_raw: str | None,
) -> DurableChatExecutionConfig:
    """Parse the exact default-off rollout contract without legacy fallback."""

    if raw is None:
        mode = DurableChatExecutionMode.OFF
    else:
        try:
            mode = DurableChatExecutionMode(raw)
        except ValueError:
            mode = DurableChatExecutionMode.INVALID

    team_ids: frozenset[str] = frozenset()
    percentage = 0
    if mode in {
        DurableChatExecutionMode.SHADOW,
        DurableChatExecutionMode.ROLLOUT,
    }:
        candidate_percentage = "0" if percentage_raw is None else percentage_raw
        if _PERCENTAGE_RE.fullmatch(candidate_percentage) is None:
            mode = DurableChatExecutionMode.INVALID
        else:
            percentage = int(candidate_percentage)
            if percentage > 100:
                mode = DurableChatExecutionMode.INVALID
        if mode is not DurableChatExecutionMode.INVALID:
            team_ids = frozenset(item for token in (team_ids_raw or "").split(",") if (item := token.strip()))

    if mode is DurableChatExecutionMode.INVALID:
        team_ids = frozenset()
        percentage = 0
    return DurableChatExecutionConfig(
        mode=mode,
        team_ids=team_ids,
        percentage=percentage,
        config_generation=_config_generation(mode, team_ids, percentage),
    )


def durable_chat_execution_config() -> DurableChatExecutionConfig:
    return parse_durable_chat_execution_config(
        os.environ.get(DURABLE_CHAT_EXECUTION_FLAG),
        os.environ.get(DURABLE_CHAT_EXECUTION_TEAM_IDS_FLAG),
        os.environ.get(DURABLE_CHAT_EXECUTION_PERCENTAGE_FLAG),
    )


def stable_durable_chat_team_bucket(team_id: str) -> int:
    if not isinstance(team_id, str) or not team_id:
        raise ValueError("durable chat rollout team identity is invalid")
    digest = hashlib.sha256(f"{_ROLLOUT_SALT}\0{team_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def terminal_v1_web_client_capable(headers: Mapping[str, str]) -> bool:
    """Read the one bounded Web reader-capability declaration."""

    return (
        headers.get(
            "x-bowhead-terminal-protocol-capability",
            "",
        ).strip()
        == "1"
    )


def terminal_v1_attachment_refs_wire_version() -> int:
    """Advertise the cluster-safe typed attachment wire."""

    return 1


def fresh_terminal_v1_admission_enabled(
    config: DurableChatExecutionConfig | None = None,
) -> bool:
    effective = config or durable_chat_execution_config()
    return bool(
        _FRESH_TERMINAL_V1_ADMISSION_RELEASED
        and effective.mode
        in {
            DurableChatExecutionMode.ROLLOUT,
            DurableChatExecutionMode.REQUIRED,
        }
    )


def chat_execution_writer_readiness(
    service: Any,
    *,
    config: DurableChatExecutionConfig | None = None,
) -> ChatExecutionWriterReadiness:
    """Project bounded writer truth for bootstrap; never expose lease identity."""

    effective = config or durable_chat_execution_config()
    existing_ready = bool(service is not None and getattr(service, "ready", False))
    fresh_ready = bool(fresh_terminal_v1_admission_enabled(effective) and service is not None and getattr(service, "fresh_admission_ready", False))
    epoch = "inactive"
    if existing_ready:
        try:
            snapshot = service.writer_snapshot()
            epoch = f"{snapshot.lease_epoch}:{snapshot.admission_epoch}:{int(snapshot.recovery_only)}"
        except Exception:
            existing_ready = False
            fresh_ready = False
            epoch = "unavailable"
    payload = f"{effective.config_generation}\0{epoch}\0{int(existing_ready)}\0{int(fresh_ready)}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return ChatExecutionWriterReadiness(
        existing_writer_ready=existing_ready,
        fresh_writer_ready=fresh_ready,
        readiness_generation=f"durable_chat_ready_v1_{digest}",
    )


def resolve_chat_execution_admission(
    *,
    config: DurableChatExecutionConfig,
    team_id: str | None,
    session_facts: SessionProtocolFacts | None,
    client_capable: bool,
    action_identity_complete: bool,
    require_action_identity: bool,
    existing_writer_ready: bool,
    fresh_writer_ready: bool,
) -> ChatExecutionAdmissionDecision:
    """Choose one write protocol from config, persisted facts and surface truth."""

    cohort_eligible = config.team_eligible(team_id)
    if not config.valid:
        return ChatExecutionAdmissionDecision(
            ChatExecutionWriteProtocol.UNAVAILABLE,
            "config_invalid",
            False,
        )

    has_v1_history = bool(session_facts is not None and session_facts.has_v1_history)
    if has_v1_history:
        if not client_capable or (require_action_identity and not action_identity_complete):
            return ChatExecutionAdmissionDecision(
                ChatExecutionWriteProtocol.REFRESH_REQUIRED,
                "persisted_v1_client_refresh_required",
                True,
            )
        if not existing_writer_ready:
            return ChatExecutionAdmissionDecision(
                ChatExecutionWriteProtocol.UNAVAILABLE,
                "persisted_v1_writer_unavailable",
                True,
            )
        return ChatExecutionAdmissionDecision(
            ChatExecutionWriteProtocol.TERMINAL_V1,
            "persisted_v1_sticky",
            True,
        )

    if config.mode in {
        DurableChatExecutionMode.OFF,
        DurableChatExecutionMode.SHADOW,
    }:
        protocol = (
            ChatExecutionWriteProtocol.REFRESH_REQUIRED
            if require_action_identity and action_identity_complete
            else ChatExecutionWriteProtocol.LEGACY_V0
        )
        return ChatExecutionAdmissionDecision(
            protocol,
            "stale_terminal_action" if protocol is ChatExecutionWriteProtocol.REFRESH_REQUIRED else config.mode.value,
            cohort_eligible,
        )

    if config.mode is DurableChatExecutionMode.ROLLOUT and not cohort_eligible:
        protocol = (
            ChatExecutionWriteProtocol.REFRESH_REQUIRED
            if require_action_identity and action_identity_complete
            else ChatExecutionWriteProtocol.LEGACY_V0
        )
        return ChatExecutionAdmissionDecision(
            protocol,
            "stale_terminal_action" if protocol is ChatExecutionWriteProtocol.REFRESH_REQUIRED else "rollout_not_eligible",
            False,
        )

    if not client_capable:
        if config.mode is DurableChatExecutionMode.ROLLOUT:
            return ChatExecutionAdmissionDecision(
                ChatExecutionWriteProtocol.LEGACY_V0,
                "rollout_client_not_capable",
                True,
            )
        return ChatExecutionAdmissionDecision(
            ChatExecutionWriteProtocol.REFRESH_REQUIRED,
            "required_client_refresh_required",
            True,
        )
    if require_action_identity and not action_identity_complete:
        if config.mode is DurableChatExecutionMode.ROLLOUT:
            return ChatExecutionAdmissionDecision(
                ChatExecutionWriteProtocol.LEGACY_V0,
                "rollout_action_identity_incomplete",
                True,
            )
        return ChatExecutionAdmissionDecision(
            ChatExecutionWriteProtocol.REFRESH_REQUIRED,
            "terminal_action_required",
            True,
        )
    if not fresh_writer_ready:
        return ChatExecutionAdmissionDecision(
            ChatExecutionWriteProtocol.UNAVAILABLE,
            "fresh_writer_unavailable",
            True,
        )
    return ChatExecutionAdmissionDecision(
        ChatExecutionWriteProtocol.TERMINAL_V1,
        "required" if config.mode is DurableChatExecutionMode.REQUIRED else "rollout_eligible",
        True,
    )


def resolve_persisted_chat_execution_protocol(
    protocol_version: object,
) -> PersistedChatExecutionProtocol:
    """Classify persisted Turn truth without consulting rollout configuration."""

    if protocol_version is None:
        return PersistedChatExecutionProtocol.LEGACY_V0
    if isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
        raise TerminalProtocolMalformedError("persisted Turn protocol is malformed")
    if protocol_version == 1:
        return PersistedChatExecutionProtocol.TERMINAL_V1
    if protocol_version > 1:
        raise TerminalProtocolFutureVersionError("Turn uses a future terminal protocol")
    raise TerminalProtocolMalformedError("persisted Turn protocol is malformed")


__all__ = [
    "DURABLE_CHAT_EXECUTION_FLAG",
    "DURABLE_CHAT_EXECUTION_PERCENTAGE_FLAG",
    "DURABLE_CHAT_EXECUTION_TEAM_IDS_FLAG",
    "ChatExecutionAdmissionDecision",
    "ChatExecutionWriteProtocol",
    "ChatExecutionWriterReadiness",
    "DurableChatExecutionConfig",
    "DurableChatExecutionMode",
    "PersistedChatExecutionProtocol",
    "chat_execution_writer_readiness",
    "durable_chat_execution_config",
    "fresh_terminal_v1_admission_enabled",
    "parse_durable_chat_execution_config",
    "resolve_chat_execution_admission",
    "resolve_persisted_chat_execution_protocol",
    "stable_durable_chat_team_bucket",
    "terminal_v1_attachment_refs_wire_version",
    "terminal_v1_web_client_capable",
]
