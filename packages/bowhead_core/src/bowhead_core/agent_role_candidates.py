"""Closed, fail-closed candidate contract for behavior-driven Lead roles.

This module deliberately does not choose the first candidate.  The runtime
reader remains behind ``FEATURE_AGENT_BEHAVIOR_M3`` until role-specific parity
work lands; management APIs can already persist personal preferences.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from bowhead_core.agent_behavior_contract import LEAD_ROLE_IDS, LeadRoleId
from bowhead_core.agent_behavior_flags import agent_behavior_m3_enabled

_REASON_CODE = re.compile(r"^[a-z][a-z0-9_.:@/-]{0,255}$")
_CANDIDATE_ID = re.compile(r"^lrc_[0-9a-f]{32}$")
_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
logger = logging.getLogger(__name__)

# Runtime roles move to the exact behavior reader one independently-reviewed
# vertical slice at a time.  Keeping the cutover set code-owned prevents a new
# reviewed Catalog row from silently changing production routing before its
# parity suite and Artifact path are ready.
BEHAVIOR_CUTOVER_LEAD_ROLES: frozenset[LeadRoleId] = frozenset({"image"})


class LeadRoleCandidateError(RuntimeError):
    """The model choice cannot be proven against the exact turn roster."""


@dataclass(frozen=True, slots=True)
class LeadRoleCandidate:
    candidate_id: str
    role_id: LeadRoleId
    agent_id: str
    runtime_profile_name: str
    assignment_id: str | None
    revision_ref: str
    release_id: str | None
    behavior_fingerprint: str
    is_primary: bool
    is_preferred: bool
    display_name: str
    description: str


@dataclass(frozen=True, slots=True)
class LeadRoleChoice:
    role_id: LeadRoleId
    candidate_id: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class ExactAgentRoleChoice:
    """Planner-approved role choice frozen for later Plan execution.

    ``candidate_id`` is only the opaque selection handle shown to the model.
    Execution never tries to recover identity from it: the exact Agent,
    assignment, immutable revision/release and behavior fingerprint are all
    persisted beside the Plan step and rechecked by admission.
    """

    role_id: LeadRoleId
    candidate_id: str
    reason_code: str
    agent_id: str
    runtime_profile_name: str
    assignment_id: str | None
    revision_ref: str
    release_id: str | None
    behavior_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.role_id not in LEAD_ROLE_IDS
            or _CANDIDATE_ID.fullmatch(self.candidate_id) is None
            or _REASON_CODE.fullmatch(self.reason_code) is None
            or not self.agent_id
            or len(self.agent_id) > 200
            or not self.runtime_profile_name
            or len(self.runtime_profile_name) > 200
            or (self.assignment_id is not None and not self.assignment_id.startswith("asa_"))
            or not self.revision_ref.startswith("arv_")
            or (self.release_id is not None and not self.release_id.startswith("arl_"))
            or ((self.assignment_id is None) != (self.release_id is None))
            or _SHA256_REF.fullmatch(self.behavior_fingerprint) is None
        ):
            raise LeadRoleCandidateError("candidate_choice_invalid")

    @classmethod
    def freeze(
        cls,
        *,
        choice: LeadRoleChoice,
        candidate: LeadRoleCandidate,
    ) -> ExactAgentRoleChoice:
        if choice.role_id != candidate.role_id or choice.candidate_id != candidate.candidate_id:
            raise LeadRoleCandidateError("candidate_choice_invalid")
        return cls(
            role_id=choice.role_id,
            candidate_id=choice.candidate_id,
            reason_code=choice.reason_code,
            agent_id=candidate.agent_id,
            runtime_profile_name=candidate.runtime_profile_name,
            assignment_id=candidate.assignment_id,
            revision_ref=candidate.revision_ref,
            release_id=candidate.release_id,
            behavior_fingerprint=candidate.behavior_fingerprint,
        )

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "role_id": self.role_id,
                "candidate_id": self.candidate_id,
                "reason_code": self.reason_code,
                "agent_id": self.agent_id,
                "runtime_profile_name": self.runtime_profile_name,
                "assignment_id": self.assignment_id,
                "revision_ref": self.revision_ref,
                "release_id": self.release_id,
                "behavior_fingerprint": self.behavior_fingerprint,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> ExactAgentRoleChoice:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LeadRoleCandidateError("candidate_choice_invalid") from exc
        expected_keys = {
            "schema_version",
            "role_id",
            "candidate_id",
            "reason_code",
            "agent_id",
            "runtime_profile_name",
            "assignment_id",
            "revision_ref",
            "release_id",
            "behavior_fingerprint",
        }
        if not isinstance(value, dict) or set(value) != expected_keys or value.get("schema_version") != 1:
            raise LeadRoleCandidateError("candidate_choice_invalid")
        scalar_keys = (
            "role_id",
            "candidate_id",
            "reason_code",
            "agent_id",
            "runtime_profile_name",
            "revision_ref",
            "behavior_fingerprint",
        )
        if any(not isinstance(value.get(key), str) for key in scalar_keys):
            raise LeadRoleCandidateError("candidate_choice_invalid")
        if any(value.get(key) is not None and not isinstance(value.get(key), str) for key in ("assignment_id", "release_id")):
            raise LeadRoleCandidateError("candidate_choice_invalid")
        return cls(
            role_id=value["role_id"],
            candidate_id=value["candidate_id"],
            reason_code=value["reason_code"],
            agent_id=value["agent_id"],
            runtime_profile_name=value["runtime_profile_name"],
            assignment_id=value["assignment_id"],
            revision_ref=value["revision_ref"],
            release_id=value["release_id"],
            behavior_fingerprint=value["behavior_fingerprint"],
        )


@dataclass(frozen=True, slots=True)
class LeadRoleCandidateRoster:
    by_role: Mapping[LeadRoleId, tuple[LeadRoleCandidate, ...]]

    @classmethod
    def build(
        cls,
        values: Mapping[LeadRoleId, tuple[LeadRoleCandidate, ...]],
    ) -> LeadRoleCandidateRoster:
        if any(role not in LEAD_ROLE_IDS for role in values):
            raise LeadRoleCandidateError("candidate_roster_invalid")
        normalized: dict[LeadRoleId, tuple[LeadRoleCandidate, ...]] = {role: tuple(values.get(role, ())) for role in LEAD_ROLE_IDS}
        seen: set[str] = set()
        for role, candidates in normalized.items():
            preferred_count = 0
            seen_identities: set[tuple[str, str | None, str, str | None, str]] = set()
            for candidate in candidates:
                identity = (
                    candidate.agent_id,
                    candidate.assignment_id,
                    candidate.revision_ref,
                    candidate.release_id,
                    candidate.behavior_fingerprint,
                )
                if (
                    candidate.role_id != role
                    or candidate.candidate_id in seen
                    or identity in seen_identities
                    or _CANDIDATE_ID.fullmatch(candidate.candidate_id) is None
                    or not candidate.agent_id
                    or len(candidate.agent_id) > 200
                    or not candidate.runtime_profile_name
                    or len(candidate.runtime_profile_name) > 200
                    or (candidate.assignment_id is not None and not candidate.assignment_id.startswith("asa_"))
                    or not candidate.revision_ref.startswith("arv_")
                    or (candidate.release_id is not None and not candidate.release_id.startswith("arl_"))
                    or _SHA256_REF.fullmatch(candidate.behavior_fingerprint) is None
                    or not candidate.display_name
                    or len(candidate.display_name) > 160
                    or len(candidate.description) > 500
                    or (candidate.is_preferred and candidate.assignment_id is None)
                ):
                    raise LeadRoleCandidateError("candidate_roster_invalid")
                seen.add(candidate.candidate_id)
                seen_identities.add(identity)
                preferred_count += int(candidate.is_preferred)
            if preferred_count > 1:
                raise LeadRoleCandidateError("candidate_roster_invalid")
        return cls(by_role=MappingProxyType(normalized))


def new_turn_candidate(
    *,
    role_id: LeadRoleId,
    agent_id: str,
    runtime_profile_name: str,
    assignment_id: str | None,
    revision_ref: str,
    release_id: str | None,
    behavior_fingerprint: str,
    is_primary: bool,
    is_preferred: bool,
    display_name: str,
    description: str,
    candidate_id: str | None = None,
) -> LeadRoleCandidate:
    return LeadRoleCandidate(
        candidate_id=candidate_id or f"lrc_{uuid4().hex}",
        role_id=role_id,
        agent_id=agent_id,
        runtime_profile_name=runtime_profile_name,
        assignment_id=assignment_id,
        revision_ref=revision_ref,
        release_id=release_id,
        behavior_fingerprint=behavior_fingerprint,
        is_primary=is_primary,
        is_preferred=is_preferred,
        display_name=display_name,
        description=description,
    )


def _candidate_id_for_scope(
    scope: str | None,
    *,
    role_id: LeadRoleId,
    agent_id: str,
    assignment_id: str | None,
    revision_ref: str,
    release_id: str | None,
    behavior_fingerprint: str,
) -> str | None:
    if scope is None:
        return None
    material = json.dumps(
        [
            scope,
            role_id,
            agent_id,
            assignment_id,
            revision_ref,
            release_id,
            behavior_fingerprint,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"lrc_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


async def build_runtime_role_candidate_roster(
    peers: Iterable[Any],
    *,
    subagent_runner: Any,
    parent_ctx: Any,
    team_subagent_store: Any = None,
    stable_scope: str | None = None,
) -> LeadRoleCandidateRoster:
    """Build the one actor-scoped exact roster shared by Lead and planner.

    ``stable_scope`` is used by request/confirm REST flows whose preview and
    confirmation happen in different requests.  It keeps only the opaque
    handle stable; admission still rechecks every exact identity field.
    """
    if not agent_behavior_m3_enabled():
        return LeadRoleCandidateRoster.build({})

    peer_values = list(peers)
    values: dict[LeadRoleId, list[LeadRoleCandidate]] = {role: [] for role in BEHAVIOR_CUTOVER_LEAD_ROLES}
    visible_by_agent_id = {str(getattr(peer, "agent_id", "") or ""): peer for peer in peer_values if getattr(peer, "agent_id", None)}
    team_id = str(getattr(parent_ctx, "team_id", "") or "")
    user_id = str(getattr(parent_ctx, "user_id", "") or "")
    store = team_subagent_store or getattr(parent_ctx, "team_subagent_store", None)
    db_path = str(getattr(store, "db_path", "") or "")
    if team_id and user_id and db_path:
        try:
            from bowhead_core.agent_role_preference import AgentRolePreferenceStore

            team_roster = await AgentRolePreferenceStore(db_path).lead_candidate_roster(
                team_id=team_id,
                user_id=user_id,
            )
            for role in BEHAVIOR_CUTOVER_LEAD_ROLES:
                for candidate in team_roster.by_role[role]:
                    peer = visible_by_agent_id.get(candidate.agent_id)
                    if peer is None or peer.name != candidate.runtime_profile_name:
                        continue
                    if role == "image" and "image_generate" not in peer.tool_names:
                        continue
                    values[role].append(
                        new_turn_candidate(
                            role_id=role,
                            agent_id=candidate.agent_id,
                            runtime_profile_name=candidate.runtime_profile_name,
                            assignment_id=candidate.assignment_id,
                            revision_ref=candidate.revision_ref,
                            release_id=candidate.release_id,
                            behavior_fingerprint=candidate.behavior_fingerprint,
                            is_primary=candidate.is_primary,
                            is_preferred=candidate.is_preferred,
                            display_name=candidate.display_name,
                            description=candidate.description,
                            candidate_id=(
                                _candidate_id_for_scope(
                                    stable_scope,
                                    role_id=role,
                                    agent_id=candidate.agent_id,
                                    assignment_id=candidate.assignment_id,
                                    revision_ref=candidate.revision_ref,
                                    release_id=candidate.release_id,
                                    behavior_fingerprint=candidate.behavior_fingerprint,
                                )
                                or candidate.candidate_id
                            ),
                        )
                    )
        except Exception:
            logger.warning(
                "agent_role_candidate.team_roster_unavailable team=%s user=%s",
                team_id,
                user_id,
                exc_info=True,
            )

    if subagent_runner is not None and team_id:
        for peer in peer_values:
            if peer.source != "system":
                continue
            try:
                projected = await subagent_runner.project_system_behavior_candidate(
                    profile_name=peer.name,
                    parent_ctx=parent_ctx,
                )
            except Exception:
                logger.warning(
                    "agent_role_candidate.system_candidate_unavailable team=%s profile=%s",
                    team_id,
                    peer.name,
                    exc_info=True,
                )
                continue
            if projected is None or projected.behavior_descriptor.verification_state != "reviewed":
                continue
            descriptor = projected.behavior_descriptor
            for role in BEHAVIOR_CUTOVER_LEAD_ROLES:
                if role not in descriptor.eligible_lead_role_ids:
                    continue
                if role == "image" and "image_generate" not in peer.tool_names:
                    continue
                values[role].append(
                    new_turn_candidate(
                        role_id=role,
                        agent_id=projected.agent_id,
                        runtime_profile_name=peer.name,
                        assignment_id=None,
                        revision_ref=projected.revision_ref,
                        release_id=None,
                        behavior_fingerprint=descriptor.behavior_fingerprint,
                        is_primary=(role == "image" and descriptor.primary_capability_ref == "cap_visualize@1"),
                        is_preferred=False,
                        display_name=str(projected.definition.get("display_name") or peer.name or "Agent")[:160],
                        description=(peer.description or "")[:500],
                        candidate_id=_candidate_id_for_scope(
                            stable_scope,
                            role_id=role,
                            agent_id=projected.agent_id,
                            assignment_id=None,
                            revision_ref=projected.revision_ref,
                            release_id=None,
                            behavior_fingerprint=descriptor.behavior_fingerprint,
                        ),
                    )
                )

    return LeadRoleCandidateRoster.build(
        {role: tuple(candidates) for role, candidates in values.items()},
    )


def validate_structured_choice(
    roster: LeadRoleCandidateRoster,
    choice: LeadRoleChoice,
) -> LeadRoleCandidate:
    """Return the exact candidate or fail without an order/name fallback."""
    if not agent_behavior_m3_enabled():
        raise LeadRoleCandidateError("behavior_reader_disabled")
    if _REASON_CODE.fullmatch(choice.reason_code) is None:
        raise LeadRoleCandidateError("candidate_choice_invalid")
    candidates = roster.by_role.get(choice.role_id, ())
    matches = tuple(item for item in candidates if item.candidate_id == choice.candidate_id)
    if len(matches) != 1:
        raise LeadRoleCandidateError("candidate_choice_invalid")
    selected = matches[0]
    preferred = tuple(item for item in candidates if item.is_preferred)
    if preferred and preferred != (selected,):
        raise LeadRoleCandidateError("candidate_choice_invalid")
    return selected


__all__ = [
    "BEHAVIOR_CUTOVER_LEAD_ROLES",
    "LeadRoleCandidate",
    "LeadRoleCandidateError",
    "LeadRoleCandidateRoster",
    "LeadRoleChoice",
    "ExactAgentRoleChoice",
    "build_runtime_role_candidate_roster",
    "new_turn_candidate",
    "validate_structured_choice",
]
