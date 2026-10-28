"""Owner-managed immutable Agent Eval Policy revisions."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_store import AgentEvalStoreError, verify_eval_suite_revision_conn

EvalPlanTier = Literal["critical", "standard", "diagnostic"]
EvalExecutionScope = Literal["paired", "candidate_only"]

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{1,199}$")
_MAX_POLICY_BYTES = 256 * 1024
_MAX_REASON_CODES = 64
_SAFE_EFFECT_MODES = {"fake_only", "no_effects", "read_only_fixture", "sandbox"}
PLATFORM_ATTEMPTS = 3
_TIERS = frozenset({"critical", "standard", "diagnostic"})
_VERDICT_RULES = frozenset({"threshold", "majority"})
# Standard cases (and diagnostic ones, which follow standard) are decided by the majority of their
# attempts. Critical cases keep the threshold rule: the platform contract passes only on every attempt
# (pass^k), and any disagreement on it stays without a verdict.
DEFAULT_VERDICT_RULES: Mapping[str, str] = {"critical": "threshold", "standard": "majority"}


class AgentEvalPolicyError(RuntimeError):
    """Stable policy failure for an API or orchestration adapter."""

    def __init__(self, code: str, *, details: Mapping[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        # Safe values the caller needs to retry correctly, such as the current policy revision.
        self.details = dict(details or {})


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AgentEvalPolicySuite:
    suite_revision_id: str
    required: bool
    tier: EvalPlanTier
    execution_scope: EvalExecutionScope
    attempts: int

    def __post_init__(self) -> None:
        if _ID_RE.fullmatch(self.suite_revision_id) is None:
            raise ValueError("Agent Eval Policy suite revision is invalid")
        if self.tier not in {"critical", "standard", "diagnostic"}:
            raise ValueError("Agent Eval Policy tier is invalid")
        if self.execution_scope not in {"paired", "candidate_only"}:
            raise ValueError("Agent Eval Policy execution scope is invalid")
        if not isinstance(self.required, bool):
            raise ValueError("Agent Eval Policy required flag is invalid")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or not 1 <= self.attempts <= 5:
            raise ValueError("Agent Eval Policy attempts must be between 1 and 5")


@dataclass(frozen=True, slots=True)
class AgentEvalPolicyDraft:
    owner_suites: tuple[AgentEvalPolicySuite, ...]
    pass_thresholds: Mapping[str, float]
    minimum_deltas: Mapping[str, float]
    flaky_limits: Mapping[str, float]
    cost_and_latency_limits: Mapping[str, int]
    allowed_effect_modes: tuple[str, ...]
    override_reason_codes: tuple[str, ...]
    reviewer_evidence_policy: Mapping[str, bool]
    # None only for a policy written before verdict rules existed: its document has no rules and every
    # tier keeps the threshold rule. New policies, and a policy the Owner's next test upgrades, carry them.
    verdict_rules: Mapping[str, str] | None

    @classmethod
    def default(
        cls,
        *,
        owner_suites: tuple[AgentEvalPolicySuite, ...] = (),
    ) -> AgentEvalPolicyDraft:
        return cls(
            owner_suites=owner_suites,
            pass_thresholds={"critical": 1.0, "standard": 0.8},
            minimum_deltas={"pass_rate": 0.05},
            flaky_limits={"critical": 0.0, "standard": 0.2},
            cost_and_latency_limits={
                "max_attempt_cost_microusd": 5_000_000,
                "max_attempt_duration_ms": 120_000,
                "max_comparison_cost_microusd": 100_000_000,
                "max_comparison_duration_ms": 1_800_000,
                "max_concurrency": 4,
                "max_tool_calls": 100,
            },
            allowed_effect_modes=("fake_only", "no_effects", "read_only_fixture", "sandbox"),
            override_reason_codes=(
                "compatibility_unknown",
                "non_critical_regression",
                "owner_quality_skipped",
                "provider_identity_attributed_only",
            ),
            reviewer_evidence_policy={
                "include_aggregate_metrics": True,
                "include_case_summaries": True,
                "include_raw_outputs": False,
            },
            verdict_rules=DEFAULT_VERDICT_RULES,
        )


@dataclass(frozen=True, slots=True)
class AgentEvalPolicyRevision:
    id: str
    team_id: str
    agent_id: str
    sequence: int
    policy: dict[str, Any]
    policy_hash: str
    created_by: str
    created_at: str


class AgentEvalPolicyStore:
    """Validate exact suite identities and append canonical policy documents."""

    def __init__(self, db_path: str | Path, *, clock: _Clock | None = None) -> None:
        self.db_path = str(db_path)
        self._clock = clock or _SystemClock()

    async def default_preview(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        """Build the current default without creating a policy revision."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await self.default_preview_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)

    async def default_preview_conn(self, db: aiosqlite.Connection, *, team_id: str, agent_id: str, actor_user_id: str) -> dict[str, Any]:
        """Compile the default inside a caller-owned read or prepare snapshot."""
        await _require_owner(
            db,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        platform = await _platform_suite(db, team_id=team_id, agent_id=agent_id)
        if platform is None:
            raise AgentEvalPolicyError("agent_eval_platform_suite_missing")
        draft = await _default_draft_conn(db, team_id=team_id, agent_id=agent_id)
        policy = await _compile_policy(
            db,
            team_id=team_id,
            agent_id=agent_id,
            draft=draft,
            platform=platform,
        )
        return policy

    async def resolve_for_prepare_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_revision_id: str | None,
        created_at: str,
    ) -> AgentEvalPolicyRevision:
        """Resolve an exact current Policy or atomically create the explicit default."""
        db.row_factory = aiosqlite.Row
        await _require_owner(
            db,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        current = await _current_row(db, team_id=team_id, agent_id=agent_id)
        if current is not None:
            if str(current["id"]) != expected_revision_id:
                raise AgentEvalPolicyError("agent_eval_policy_version_conflict", details={"current_policy_revision_id": str(current["id"])})
            return _revision_from_row(current)
        if expected_revision_id is not None:
            raise AgentEvalPolicyError("agent_eval_policy_version_conflict")
        draft = await _default_draft_conn(db, team_id=team_id, agent_id=agent_id)
        return await self.create_revision_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            expected_revision_id=None,
            draft=draft,
            created_at=created_at,
        )

    async def create_revision(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_revision_id: str | None,
        draft: AgentEvalPolicyDraft,
    ) -> AgentEvalPolicyRevision:
        timestamp = self._clock.now().astimezone(UTC).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                active_candidate = await _active_candidate(db, team_id=team_id, agent_id=agent_id)
                if active_candidate is not None:
                    code = (
                        "agent_eval_plan_frozen_for_review"
                        if str(active_candidate["state"]) in {"pending", "approved"}
                        else "agent_eval_policy_plan_rotation_required"
                    )
                    raise AgentEvalPolicyError(code)
                revision = await self.create_revision_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                    expected_revision_id=expected_revision_id,
                    draft=draft,
                    created_at=timestamp,
                )
                await db.commit()
                return revision
            except BaseException:
                await db.rollback()
                raise

    async def create_revision_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_revision_id: str | None,
        draft: AgentEvalPolicyDraft,
        created_at: str,
    ) -> AgentEvalPolicyRevision:
        return await _create_revision_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            expected_revision_id=expected_revision_id,
            draft=draft,
            created_at=created_at,
        )

    async def current(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> AgentEvalPolicyRevision | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_owner(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
            row = await _current_row(db, team_id=team_id, agent_id=agent_id)
        return _revision_from_row(row) if row is not None else None

    async def get_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        revision_id: str,
    ) -> AgentEvalPolicyRevision:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM agent_eval_policy_revision
            WHERE team_id=? AND agent_id=? AND id=?
            """,
            (team_id, agent_id, revision_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentEvalPolicyError("agent_eval_policy_not_found")
        return _revision_from_row(row)


async def _compile_policy(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    draft: AgentEvalPolicyDraft,
    platform: aiosqlite.Row,
    tolerate_expired_fixtures: bool = False,
) -> dict[str, Any]:
    _validate_settings(draft)

    async def verify(suite_revision_id: str) -> None:
        try:
            await verify_eval_suite_revision_conn(
                db,
                suite_revision_id=suite_revision_id,
                allowed_effect_modes=draft.allowed_effect_modes,
            )
        except AgentEvalStoreError as exc:
            # Expiry is checked last, so everything else about the revision is valid. A rotation
            # still pins it; preparing a plan from it then names the expired fixture.
            if tolerate_expired_fixtures and exc.code == "agent_eval_fixture_expired":
                return
            raise AgentEvalPolicyError(exc.code) from exc

    await verify(str(platform["revision_id"]))
    seen: set[str] = set()
    owner_items: list[dict[str, Any]] = []
    for selected in draft.owner_suites:
        if selected.suite_revision_id in seen:
            raise ValueError("Agent Eval Policy contains a duplicate suite revision")
        seen.add(selected.suite_revision_id)
        async with db.execute(
            """
            SELECT revision.id,suite.suite_kind,suite.state,suite.team_id,suite.agent_id,
                   suite.agent_source_kind,suite.platform_managed
            FROM agent_eval_suite_revision revision
            JOIN agent_eval_suite suite ON suite.id=revision.suite_id
            WHERE revision.id=?
            """,
            (selected.suite_revision_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if (
            row is None
            or str(row["team_id"]) != team_id
            or str(row["agent_id"]) != agent_id
            or str(row["agent_source_kind"]) != "team"
            or str(row["suite_kind"]) != "owner_quality"
            or str(row["state"]) != "active"
            or int(row["platform_managed"]) != 0
        ):
            raise AgentEvalPolicyError("agent_eval_policy_suite_scope_mismatch")
        await verify(selected.suite_revision_id)
        owner_items.append(_suite_item(selected, suite_kind="owner_quality"))

    suite_items = [
        {
            # Paired, so a platform failure can be told apart from one Live already has.
            "attempts": PLATFORM_ATTEMPTS,
            "execution_scope": "paired",
            "required": True,
            "suite_kind": "platform_contract",
            "suite_revision_id": str(platform["revision_id"]),
            "tier": "critical",
        },
        *owner_items,
    ]
    policy = {
        "allowed_effect_modes": sorted(draft.allowed_effect_modes),
        "cost_and_latency_limits": _integer_mapping(draft.cost_and_latency_limits),
        "flaky_limits": _ratio_mapping(draft.flaky_limits),
        "minimum_deltas": _ratio_mapping(draft.minimum_deltas),
        "override_reason_codes": sorted(draft.override_reason_codes),
        "pass_thresholds": _ratio_mapping(draft.pass_thresholds),
        "reviewer_evidence_policy": _boolean_mapping(draft.reviewer_evidence_policy),
        "schema_version": 1,
        "suite_items": suite_items,
        **({"verdict_rules": _verdict_rule_mapping(draft.verdict_rules)} if draft.verdict_rules is not None else {}),
    }
    _json_document(policy)
    return policy


async def _create_revision_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    expected_revision_id: str | None,
    draft: AgentEvalPolicyDraft,
    created_at: str,
    tolerate_expired_fixtures: bool = False,
) -> AgentEvalPolicyRevision:
    db.row_factory = aiosqlite.Row
    await _require_owner(
        db,
        team_id=team_id,
        agent_id=agent_id,
        actor_user_id=actor_user_id,
    )
    current = await _current_row(db, team_id=team_id, agent_id=agent_id)
    current_id = str(current["id"]) if current is not None else None
    if current_id != expected_revision_id:
        raise AgentEvalPolicyError(
            "agent_eval_policy_version_conflict",
            details={"current_policy_revision_id": current_id} if current_id else None,
        )
    platform = await _platform_suite(db, team_id=team_id, agent_id=agent_id)
    if platform is None:
        raise AgentEvalPolicyError("agent_eval_platform_suite_missing")
    policy = await _compile_policy(
        db,
        team_id=team_id,
        agent_id=agent_id,
        draft=draft,
        platform=platform,
        tolerate_expired_fixtures=tolerate_expired_fixtures,
    )
    policy_json = _canonical_json(policy)
    if len(policy_json.encode()) > _MAX_POLICY_BYTES:
        raise ValueError("Agent Eval Policy exceeds 256 KiB")
    policy_hash = _hash_text(policy_json)
    if current is not None and str(current["policy_hash"]) == policy_hash:
        return _revision_from_row(current)
    sequence = int(current["sequence"]) + 1 if current is not None else 1
    revision_id = f"aepr_{uuid4().hex}"
    await db.execute(
        """
        INSERT INTO agent_eval_policy_revision(
          id,team_id,agent_id,sequence,policy_json,policy_hash,created_by,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            revision_id,
            team_id,
            agent_id,
            sequence,
            policy_json,
            policy_hash,
            actor_user_id,
            created_at,
        ),
    )
    async with db.execute(
        "SELECT * FROM agent_eval_policy_revision WHERE id=?",
        (revision_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentEvalPolicyError("agent_eval_policy_create_failed")
    return _revision_from_row(row)


async def rotate_policy_onto_current_suites_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    created_at: str,
) -> AgentEvalPolicyRevision | None:
    """Append a policy revision when the current one no longer pins the Agent's current suites.

    The policy follows the platform contract and every active Owner suite at its newest
    revision (highest sequence). Owner entries keep their required/tier/scope/attempts; an
    active Owner suite the policy doesn't list yet joins with the defaults a first policy
    uses; entries whose suite is no longer active are dropped. Thresholds, limits, override
    reasons and the reviewer evidence policy carry over unchanged, and earlier revisions stay
    as they are, so every plan remains traceable to the exact suite revisions it ran.

    The revision is attributed to the Agent's Owner, who owns the policy. Returns None when
    nothing had to change, there is no policy yet (the first preparation creates it from the
    current suites), or the Owner can't hold one; the next rotation point catches up.
    """
    db.row_factory = aiosqlite.Row
    current = await _current_row(db, team_id=team_id, agent_id=agent_id)
    if current is None:
        return None
    if await _platform_suite(db, team_id=team_id, agent_id=agent_id) is None:
        # Preparation refuses a policy without a platform contract on its own.
        return None
    policy = json.loads(str(current["policy_json"]))
    items = policy.get("suite_items") if isinstance(policy, dict) else None
    if not isinstance(items, list):
        raise AgentEvalPolicyError("agent_eval_policy_malformed")
    async with db.execute(
        "SELECT COALESCE(owner_user_id,created_by) FROM team_subagent WHERE team_id=? AND id=?",
        (team_id, agent_id),
    ) as cursor:
        owner = await cursor.fetchone()
    if owner is None:
        return None
    latest_by_suite = await _active_owner_suite_revisions(db, team_id=team_id, agent_id=agent_id)
    suite_of_revision: dict[str, str] = {}
    pinned = [item for item in items if isinstance(item, dict) and item.get("suite_kind") == "owner_quality"]
    if pinned:
        placeholders = ",".join("?" for _ in pinned)
        async with db.execute(
            f"SELECT id,suite_id FROM agent_eval_suite_revision WHERE id IN ({placeholders})",
            tuple(str(item["suite_revision_id"]) for item in pinned),
        ) as cursor:
            suite_of_revision = {str(row["id"]): str(row["suite_id"]) for row in await cursor.fetchall()}
    owner_suites: list[AgentEvalPolicySuite] = []
    followed: set[str] = set()
    for item in pinned:
        suite_id = suite_of_revision.get(str(item["suite_revision_id"]))
        if suite_id is None or suite_id not in latest_by_suite or suite_id in followed:
            continue
        followed.add(suite_id)
        owner_suites.append(
            AgentEvalPolicySuite(
                suite_revision_id=latest_by_suite[suite_id],
                required=bool(item["required"]),
                tier=item["tier"],
                execution_scope=item["execution_scope"],
                attempts=int(item["attempts"]),
            )
        )
    owner_suites.extend(_default_owner_suite(revision_id) for suite_id, revision_id in latest_by_suite.items() if suite_id not in followed)
    draft = AgentEvalPolicyDraft(
        owner_suites=tuple(owner_suites),
        pass_thresholds=dict(policy.get("pass_thresholds") or {}),
        minimum_deltas=dict(policy.get("minimum_deltas") or {}),
        flaky_limits=dict(policy.get("flaky_limits") or {}),
        cost_and_latency_limits=dict(policy.get("cost_and_latency_limits") or {}),
        allowed_effect_modes=tuple(policy.get("allowed_effect_modes") or ()),
        override_reason_codes=tuple(policy.get("override_reason_codes") or ()),
        reviewer_evidence_policy=dict(policy.get("reviewer_evidence_policy") or {}),
        # Carried as is: following new suites never changes how cases are decided on its own.
        verdict_rules=_carried_verdict_rules(policy),
    )
    try:
        revision = await _create_revision_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=str(owner[0]),
            expected_revision_id=str(current["id"]),
            draft=draft,
            created_at=created_at,
            tolerate_expired_fixtures=True,
        )
    except AgentEvalPolicyError as exc:
        if exc.code == "agent_eval_policy_forbidden":
            # An inactive Owner can't hold a policy; preparation will refuse on its own.
            return None
        raise
    return None if revision.id == str(current["id"]) else revision


async def adopt_default_verdict_rules_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    created_at: str,
) -> AgentEvalPolicyRevision | None:
    """Append the current policy with the default verdict rules when it was written before them.

    The new revision copies the current document exactly and adds only `verdict_rules`, so suites,
    thresholds and limits stay as the Owner had them. Earlier revisions, the plans compiled from them and
    their snapshots are never rewritten. Called from the Owner's own test start, which then recompiles
    the plan it runs; returns None when the policy already has verdict rules or there is none yet.
    """
    db.row_factory = aiosqlite.Row
    current = await _current_row(db, team_id=team_id, agent_id=agent_id)
    if current is None:
        return None
    revision = _revision_from_row(current)
    if "verdict_rules" in revision.policy:
        return None
    policy = {**revision.policy, "verdict_rules": _verdict_rule_mapping(DEFAULT_VERDICT_RULES)}
    policy_json = _canonical_json(policy)
    if len(policy_json.encode()) > _MAX_POLICY_BYTES:
        raise ValueError("Agent Eval Policy exceeds 256 KiB")
    revision_id = f"aepr_{uuid4().hex}"
    await db.execute(
        """
        INSERT INTO agent_eval_policy_revision(
          id,team_id,agent_id,sequence,policy_json,policy_hash,created_by,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (revision_id, team_id, agent_id, revision.sequence + 1, policy_json, _hash_text(policy_json), actor_user_id, created_at),
    )
    async with db.execute("SELECT * FROM agent_eval_policy_revision WHERE id=?", (revision_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentEvalPolicyError("agent_eval_policy_create_failed")
    return _revision_from_row(row)


def _carried_verdict_rules(policy: Mapping[str, Any]) -> dict[str, str] | None:
    if "verdict_rules" not in policy:
        return None
    rules = policy["verdict_rules"]
    if not isinstance(rules, dict):
        raise AgentEvalPolicyError("agent_eval_policy_malformed")
    return dict(rules)


async def _active_owner_suite_revisions(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
) -> dict[str, str]:
    """Each active Owner suite's newest revision, in suite creation order."""
    async with db.execute(
        """
        SELECT suite.id AS suite_id,revision.id AS revision_id
        FROM agent_eval_suite suite
        JOIN agent_eval_suite_revision revision ON revision.id=(
          SELECT latest.id FROM agent_eval_suite_revision latest
          WHERE latest.suite_id=suite.id ORDER BY latest.sequence DESC LIMIT 1
        )
        WHERE suite.team_id=? AND suite.agent_id=?
          AND suite.agent_source_kind='team' AND suite.suite_kind='owner_quality'
          AND suite.state='active' AND suite.platform_managed=0
        ORDER BY suite.created_at,suite.id
        """,
        (team_id, agent_id),
    ) as cursor:
        return {str(row["suite_id"]): str(row["revision_id"]) for row in await cursor.fetchall()}


def _default_owner_suite(suite_revision_id: str) -> AgentEvalPolicySuite:
    return AgentEvalPolicySuite(
        suite_revision_id=suite_revision_id,
        required=True,
        tier="standard",
        execution_scope="paired",
        attempts=3,
    )


async def _default_draft_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
) -> AgentEvalPolicyDraft:
    latest_by_suite = await _active_owner_suite_revisions(db, team_id=team_id, agent_id=agent_id)
    return AgentEvalPolicyDraft.default(
        owner_suites=tuple(_default_owner_suite(revision_id) for revision_id in latest_by_suite.values()),
    )


def _validate_settings(draft: AgentEvalPolicyDraft) -> None:
    if not draft.allowed_effect_modes or set(draft.allowed_effect_modes) - _SAFE_EFFECT_MODES:
        raise ValueError("Agent Eval Policy effect mode is invalid")
    if len(set(draft.allowed_effect_modes)) != len(draft.allowed_effect_modes):
        raise ValueError("Agent Eval Policy effect modes must be unique")
    if (
        len(draft.override_reason_codes) > _MAX_REASON_CODES
        or len(set(draft.override_reason_codes)) != len(draft.override_reason_codes)
        or any(_ID_RE.fullmatch(code) is None for code in draft.override_reason_codes)
    ):
        raise ValueError("Agent Eval Policy override reason codes are invalid")
    limits = _integer_mapping(draft.cost_and_latency_limits)
    required_limits = {
        "max_attempt_cost_microusd",
        "max_attempt_duration_ms",
        "max_comparison_cost_microusd",
        "max_comparison_duration_ms",
        "max_concurrency",
        "max_tool_calls",
    }
    if set(limits) != required_limits or any(value < 0 for value in limits.values()):
        raise ValueError("Agent Eval Policy cost and latency limits are invalid")
    if not 1 <= limits["max_concurrency"] <= 32:
        raise ValueError("Agent Eval Policy concurrency is invalid")
    if limits["max_attempt_duration_ms"] > limits["max_comparison_duration_ms"]:
        raise ValueError("Agent Eval Policy attempt duration exceeds comparison duration")
    if limits["max_attempt_cost_microusd"] > limits["max_comparison_cost_microusd"]:
        raise ValueError("Agent Eval Policy attempt cost exceeds comparison cost")


def _suite_item(selected: AgentEvalPolicySuite, *, suite_kind: str) -> dict[str, Any]:
    return {
        "attempts": selected.attempts,
        "execution_scope": selected.execution_scope,
        "required": selected.required,
        "suite_kind": suite_kind,
        "suite_revision_id": selected.suite_revision_id,
        "tier": selected.tier,
    }


async def _require_owner(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> None:
    async with db.execute(
        """
        SELECT 1
        FROM team_subagent agent
        JOIN teams team ON team.id=agent.team_id AND team.status='active'
        JOIN users actor ON actor.id=? AND actor.team_id=agent.team_id AND actor.status='active'
        JOIN team_members member
          ON member.team_id=agent.team_id AND member.user_id=actor.id AND member.status='active'
        WHERE agent.team_id=? AND agent.id=? AND agent.lifecycle_state='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        """,
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        if await cursor.fetchone() is None:
            raise AgentEvalPolicyError("agent_eval_policy_forbidden")


async def _platform_suite(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT suite.id AS suite_id,revision.id AS revision_id
        FROM agent_eval_suite suite
        JOIN agent_eval_suite_revision revision ON revision.id=(
          SELECT latest.id FROM agent_eval_suite_revision latest
          WHERE latest.suite_id=suite.id ORDER BY latest.sequence DESC LIMIT 1
        )
        WHERE suite.team_id=? AND suite.agent_id=? AND suite.agent_source_kind='team'
          AND suite.suite_kind='platform_contract' AND suite.platform_managed=1
          AND suite.state='active'
        ORDER BY suite.created_at,suite.id LIMIT 1
        """,
        (team_id, agent_id),
    ) as cursor:
        return await cursor.fetchone()


async def _current_row(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT * FROM agent_eval_policy_revision
        WHERE team_id=? AND agent_id=? ORDER BY sequence DESC LIMIT 1
        """,
        (team_id, agent_id),
    ) as cursor:
        return await cursor.fetchone()


async def _active_candidate(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT id,state FROM team_subagent_release_candidate
        WHERE team_id=? AND subagent_id=? AND state IN ('prepared','pending','approved')
        LIMIT 1
        """,
        (team_id, agent_id),
    ) as cursor:
        return await cursor.fetchone()


def _revision_from_row(row: Mapping[str, Any]) -> AgentEvalPolicyRevision:
    policy = _object(row["policy_json"])
    policy_json = _canonical_json(policy)
    if _hash_text(policy_json) != str(row["policy_hash"]):
        raise AgentEvalPolicyError("agent_eval_policy_hash_mismatch")
    return AgentEvalPolicyRevision(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        agent_id=str(row["agent_id"]),
        sequence=int(row["sequence"]),
        policy=policy,
        policy_hash=str(row["policy_hash"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _ratio_mapping(value: Mapping[str, float]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, item in sorted(value.items()):
        if _ID_RE.fullmatch(key) is None or isinstance(item, bool) or not isinstance(item, int | float):
            raise ValueError("Agent Eval Policy ratio mapping is invalid")
        number = float(item)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError("Agent Eval Policy ratio must be between 0 and 1")
        result[key] = number
    if not result:
        raise ValueError("Agent Eval Policy ratio mapping cannot be empty")
    return result


def _verdict_rule_mapping(value: Mapping[str, str]) -> dict[str, str]:
    result = dict(sorted(value.items()))
    if not result or set(result) - _TIERS or set(result.values()) - _VERDICT_RULES:
        raise ValueError("Agent Eval Policy verdict rules are invalid")
    return result


def _integer_mapping(value: Mapping[str, int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, item in sorted(value.items()):
        if _ID_RE.fullmatch(key) is None or isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("Agent Eval Policy integer mapping is invalid")
        result[key] = item
    return result


def _boolean_mapping(value: Mapping[str, bool]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for key, item in sorted(value.items()):
        if _ID_RE.fullmatch(key) is None or not isinstance(item, bool):
            raise ValueError("Agent Eval Policy boolean mapping is invalid")
        result[key] = item
    if not result:
        raise ValueError("Agent Eval Policy reviewer evidence policy cannot be empty")
    return result


def _json_document(value: object) -> None:
    try:
        encoded = _canonical_json(value).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent Eval Policy must be JSON serializable") from exc
    if len(encoded) > _MAX_POLICY_BYTES:
        raise ValueError("Agent Eval Policy exceeds 256 KiB")


def _object(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise AgentEvalPolicyError("agent_eval_policy_malformed") from exc
    if not isinstance(decoded, dict):
        raise AgentEvalPolicyError("agent_eval_policy_malformed")
    return decoded


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


__all__ = [
    "DEFAULT_VERDICT_RULES",
    "AgentEvalPolicyDraft",
    "AgentEvalPolicyError",
    "AgentEvalPolicyRevision",
    "AgentEvalPolicyStore",
    "AgentEvalPolicySuite",
]
