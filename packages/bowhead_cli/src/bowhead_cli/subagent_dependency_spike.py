"""Isolated Stage 0A spike for Subagent dependency invalidation."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

DependencyKind = Literal["native_tool", "team_skill", "team_tool"]
DependencyStatus = Literal["active", "deleted", "disabled"]

_TEAM_ID = "team_dependency_spike"
_CONTROL_TEAM_ID = "team_dependency_control"
_NOW = "2026-07-28T12:00:00Z"
_DEFAULT_AGENT_COUNT = 5000
_DEFAULT_QUEUE_LIMIT = 64
_DEPENDENCY_STATUSES = frozenset({"active", "deleted", "disabled"})


@dataclass(frozen=True)
class DependencyRef:
    kind: DependencyKind
    dependency_id: str
    version: str


@dataclass(frozen=True)
class DependencyInvalidation:
    applied: bool
    catalog_revision: int
    matched_agents: int
    newly_stale_agents: int
    queue_depth: int
    deferred_stale_agents: int
    reason: str | None


@dataclass(frozen=True)
class RuntimeDependencyDecision:
    allowed: bool
    denial_reason: str | None


@dataclass(frozen=True)
class SubagentDependencySpikeReport:
    schema_version: int
    scope: str
    methodology: dict[str, str]
    environment: dict[str, str]
    dataset: dict[str, int]
    targeted_invalidation: dict[str, Any]
    global_rule_invalidation: dict[str, Any]
    reconciler: dict[str, Any]
    backpressure: dict[str, Any]
    exact_ref_replacement: dict[str, Any]
    query_plan: list[str]
    checks: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def initialize_dependency_spike_fixture(
    connection: sqlite3.Connection,
    *,
    agent_count: int = _DEFAULT_AGENT_COUNT,
) -> None:
    """Create a deterministic synthetic dependency graph."""
    if agent_count < 30:
        raise ValueError("agent_count must be at least 30")

    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE team_subagent (
          id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          status TEXT NOT NULL,
          audience_mode TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE team_subagent_validation (
          subagent_id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          referenced_dependency_digest TEXT NOT NULL,
          global_rule_digest TEXT NOT NULL,
          validator_version INTEGER NOT NULL,
          status TEXT NOT NULL,
          eligible INTEGER NOT NULL,
          checked_at TEXT NOT NULL,
          FOREIGN KEY(subagent_id) REFERENCES team_subagent(id) ON DELETE CASCADE
        );

        CREATE TABLE team_subagent_dependency_state (
          team_id TEXT PRIMARY KEY,
          catalog_revision INTEGER NOT NULL DEFAULT 1,
          catalog_digest TEXT NOT NULL,
          global_rule_digest TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE team_subagent_validator_state (
          team_id TEXT PRIMARY KEY,
          validator_version INTEGER NOT NULL
        );

        CREATE TABLE dependency_catalog_source (
          team_id TEXT NOT NULL,
          dependency_kind TEXT NOT NULL,
          dependency_id TEXT NOT NULL,
          dependency_version TEXT NOT NULL,
          status TEXT NOT NULL,
          PRIMARY KEY(team_id, dependency_kind, dependency_id)
        );

        CREATE TABLE team_subagent_dependency_catalog (
          team_id TEXT NOT NULL,
          dependency_kind TEXT NOT NULL,
          dependency_id TEXT NOT NULL,
          dependency_version TEXT NOT NULL,
          status TEXT NOT NULL,
          PRIMARY KEY(team_id, dependency_kind, dependency_id)
        );

        CREATE TABLE team_subagent_global_rule (
          team_id TEXT NOT NULL,
          target_kind TEXT NOT NULL,
          target_id TEXT NOT NULL,
          denied INTEGER NOT NULL,
          rule_version TEXT NOT NULL,
          PRIMARY KEY(team_id, target_kind, target_id)
        );

        CREATE TABLE team_subagent_dependency_ref (
          subagent_id TEXT NOT NULL,
          team_id TEXT NOT NULL,
          dependency_kind TEXT NOT NULL,
          dependency_id TEXT NOT NULL,
          dependency_version TEXT NOT NULL,
          PRIMARY KEY(subagent_id, dependency_kind, dependency_id),
          FOREIGN KEY(subagent_id) REFERENCES team_subagent(id) ON DELETE CASCADE
        );

        CREATE INDEX idx_team_subagent_dependency_reverse
          ON team_subagent_dependency_ref(
            team_id,
            dependency_kind,
            dependency_id,
            subagent_id
          );

        CREATE TABLE team_subagent_validation_stale (
          subagent_id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          reason TEXT NOT NULL,
          invalidated_revision INTEGER NOT NULL,
          priority INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(subagent_id) REFERENCES team_subagent(id) ON DELETE CASCADE
        );

        CREATE INDEX idx_team_subagent_validation_stale_priority
          ON team_subagent_validation_stale(
            team_id,
            priority,
            created_at,
            subagent_id
          );

        CREATE TABLE team_subagent_validation_queue (
          subagent_id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL,
          reason TEXT NOT NULL,
          invalidated_revision INTEGER NOT NULL,
          priority INTEGER NOT NULL,
          enqueued_at TEXT NOT NULL,
          FOREIGN KEY(subagent_id) REFERENCES team_subagent(id) ON DELETE CASCADE
        );

        CREATE INDEX idx_team_subagent_validation_queue_priority
          ON team_subagent_validation_queue(
            team_id,
            priority,
            enqueued_at,
            subagent_id
          );

        CREATE TABLE subagent_dependency_spike_metrics (
          team_id TEXT PRIMARY KEY,
          hook_bumps INTEGER NOT NULL DEFAULT 0,
          reconciler_repairs INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    dependencies: list[tuple[str, str, str, str, str]] = [
        (_TEAM_ID, "team_tool", "tool.shared", "v1", "active"),
        (_TEAM_ID, "team_tool", "tool.stable", "v1", "active"),
        (_TEAM_ID, "team_tool", "tool.unused", "v1", "active"),
        (_TEAM_ID, "team_tool", "tool.swap_old", "v1", "active"),
        (_TEAM_ID, "team_tool", "tool.swap_new", "v1", "active"),
        (_TEAM_ID, "native_tool", "native.sql", "v1", "active"),
        (_TEAM_ID, "team_skill", "skill.missed", "v1", "active"),
        (_CONTROL_TEAM_ID, "team_tool", "tool.shared", "v1", "active"),
    ]
    connection.executemany(
        """
        INSERT INTO dependency_catalog_source (
          team_id,
          dependency_kind,
          dependency_id,
          dependency_version,
          status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        dependencies,
    )
    connection.executemany(
        """
        INSERT INTO team_subagent_dependency_catalog (
          team_id,
          dependency_kind,
          dependency_id,
          dependency_version,
          status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        dependencies,
    )

    agents: list[tuple[str, str, str, str, str]] = []
    refs_by_agent: dict[str, list[DependencyRef]] = {}
    dependency_refs: list[tuple[str, str, str, str, str]] = []
    for index in range(agent_count):
        subagent_id = _agent_id(index)
        if index < 3:
            status = "enabled"
            audience_mode = ("teamwide", "custom", "private")[index]
            base_ref = DependencyRef("team_tool", "tool.shared", "v1")
        else:
            status = "disabled" if index >= 20 and index % 10 == 0 else "enabled"
            audience_mode = ("teamwide", "custom", "private")[index % 3]
            base_ref = DependencyRef("team_tool", "tool.stable", "v1")
        refs = [base_ref]
        if index < 20:
            refs.append(DependencyRef("native_tool", "native.sql", "v1"))
        if index in (10, 11):
            refs.append(DependencyRef("team_skill", "skill.missed", "v1"))
        if index == 25:
            refs.append(DependencyRef("team_tool", "tool.swap_old", "v1"))
        agents.append((subagent_id, _TEAM_ID, status, audience_mode, _NOW))
        refs_by_agent[subagent_id] = refs
        dependency_refs.extend(
            (
                subagent_id,
                _TEAM_ID,
                ref.kind,
                ref.dependency_id,
                ref.version,
            )
            for ref in refs
        )

    control_subagent_id = "sa_dependency_control"
    control_ref = DependencyRef("team_tool", "tool.shared", "v1")
    agents.append(
        (
            control_subagent_id,
            _CONTROL_TEAM_ID,
            "enabled",
            "teamwide",
            _NOW,
        )
    )
    refs_by_agent[control_subagent_id] = [control_ref]
    dependency_refs.append(
        (
            control_subagent_id,
            _CONTROL_TEAM_ID,
            control_ref.kind,
            control_ref.dependency_id,
            control_ref.version,
        )
    )
    connection.executemany(
        """
        INSERT INTO team_subagent (
          id,
          team_id,
          status,
          audience_mode,
          updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        agents,
    )
    connection.executemany(
        """
        INSERT INTO team_subagent_dependency_ref (
          subagent_id,
          team_id,
          dependency_kind,
          dependency_id,
          dependency_version
        ) VALUES (?, ?, ?, ?, ?)
        """,
        dependency_refs,
    )

    for team_id in (_TEAM_ID, _CONTROL_TEAM_ID):
        catalog_digest = _catalog_digest(
            connection,
            table="dependency_catalog_source",
            team_id=team_id,
        )
        global_rule_digest = _global_rule_digest(connection, team_id=team_id)
        connection.execute(
            """
            INSERT INTO team_subagent_dependency_state (
              team_id,
              catalog_revision,
              catalog_digest,
              global_rule_digest,
              updated_at
            ) VALUES (?, 1, ?, ?, ?)
            """,
            (team_id, catalog_digest, global_rule_digest, _NOW),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_validator_state (
              team_id,
              validator_version
            ) VALUES (?, 1)
            """,
            (team_id,),
        )
        connection.execute(
            """
            INSERT INTO subagent_dependency_spike_metrics (
              team_id,
              hook_bumps,
              reconciler_repairs
            ) VALUES (?, 0, 0)
            """,
            (team_id,),
        )

    validation_rows = []
    for subagent_id, team_id, *_ in agents:
        validation_rows.append(
            (
                subagent_id,
                team_id,
                _dependency_digest(refs_by_agent[subagent_id]),
                _global_rule_digest(connection, team_id=team_id),
                1,
                "valid",
                1,
                _NOW,
            )
        )
    connection.executemany(
        """
        INSERT INTO team_subagent_validation (
          subagent_id,
          team_id,
          referenced_dependency_digest,
          global_rule_digest,
          validator_version,
          status,
          eligible,
          checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        validation_rows,
    )
    connection.commit()


def apply_dependency_change_hook(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    dependency_kind: DependencyKind,
    dependency_id: str,
    dependency_version: str,
    status: DependencyStatus,
    queue_limit: int = _DEFAULT_QUEUE_LIMIT,
) -> DependencyInvalidation:
    """Apply a catalog hook and invalidate only reverse-index matches."""
    _validate_dependency_mutation(
        dependency_version=dependency_version,
        status=status,
        queue_limit=queue_limit,
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute(
            """
            SELECT dependency_version, status
            FROM dependency_catalog_source
            WHERE team_id = ?
              AND dependency_kind = ?
              AND dependency_id = ?
            """,
            (team_id, dependency_kind, dependency_id),
        ).fetchone()
        if current == (dependency_version, status):
            result = _no_change_result(connection, team_id=team_id)
            connection.commit()
            return result

        values = (
            team_id,
            dependency_kind,
            dependency_id,
            dependency_version,
            status,
        )
        for table in (
            "dependency_catalog_source",
            "team_subagent_dependency_catalog",
        ):
            connection.execute(
                f"""
                INSERT INTO {table} (
                  team_id,
                  dependency_kind,
                  dependency_id,
                  dependency_version,
                  status
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(team_id, dependency_kind, dependency_id)
                DO UPDATE SET
                  dependency_version = excluded.dependency_version,
                  status = excluded.status
                """,
                values,
            )

        revision = _bump_catalog_revision(
            connection,
            team_id=team_id,
            catalog_digest=_catalog_digest(
                connection,
                table="dependency_catalog_source",
                team_id=team_id,
            ),
        )
        reason = _dependency_reason(status)
        affected = _reverse_index_rows(
            connection,
            team_id=team_id,
            dependency_kind=dependency_kind,
            dependency_id=dependency_id,
        )
        newly_stale = _mark_stale(
            connection,
            rows=affected,
            reason=reason,
            revision=revision,
        )
        queue_depth, deferred = _refill_validation_queue(
            connection,
            team_id=team_id,
            queue_limit=queue_limit,
        )
        connection.execute(
            """
            UPDATE subagent_dependency_spike_metrics
            SET hook_bumps = hook_bumps + 1
            WHERE team_id = ?
            """,
            (team_id,),
        )
        connection.commit()
        return DependencyInvalidation(
            applied=True,
            catalog_revision=revision,
            matched_agents=len(affected),
            newly_stale_agents=newly_stale,
            queue_depth=queue_depth,
            deferred_stale_agents=deferred,
            reason=reason,
        )
    except Exception:
        connection.rollback()
        raise


def apply_global_deny_hook(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    target_kind: DependencyKind,
    target_id: str,
    denied: bool,
    rule_version: str,
    queue_limit: int = _DEFAULT_QUEUE_LIMIT,
) -> DependencyInvalidation:
    """Invalidate only refs in the changed global-rule scope."""
    if not rule_version:
        raise ValueError("rule_version must not be empty")
    _validate_queue_limit(queue_limit)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute(
            """
            SELECT denied, rule_version
            FROM team_subagent_global_rule
            WHERE team_id = ?
              AND target_kind = ?
              AND target_id = ?
            """,
            (team_id, target_kind, target_id),
        ).fetchone()
        expected = (int(denied), rule_version)
        if current == expected:
            result = _no_change_result(connection, team_id=team_id)
            connection.commit()
            return result

        connection.execute(
            """
            INSERT INTO team_subagent_global_rule (
              team_id,
              target_kind,
              target_id,
              denied,
              rule_version
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(team_id, target_kind, target_id)
            DO UPDATE SET
              denied = excluded.denied,
              rule_version = excluded.rule_version
            """,
            (team_id, target_kind, target_id, int(denied), rule_version),
        )
        revision = _bump_catalog_revision(
            connection,
            team_id=team_id,
            global_rule_digest=_global_rule_digest(
                connection,
                team_id=team_id,
            ),
        )
        reason = "security_deny" if denied else "global_rule_changed"
        affected = _reverse_index_rows(
            connection,
            team_id=team_id,
            dependency_kind=target_kind,
            dependency_id=target_id,
        )
        newly_stale = _mark_stale(
            connection,
            rows=affected,
            reason=reason,
            revision=revision,
        )
        queue_depth, deferred = _refill_validation_queue(
            connection,
            team_id=team_id,
            queue_limit=queue_limit,
        )
        connection.execute(
            """
            UPDATE subagent_dependency_spike_metrics
            SET hook_bumps = hook_bumps + 1
            WHERE team_id = ?
            """,
            (team_id,),
        )
        connection.commit()
        return DependencyInvalidation(
            applied=True,
            catalog_revision=revision,
            matched_agents=len(affected),
            newly_stale_agents=newly_stale,
            queue_depth=queue_depth,
            deferred_stale_agents=deferred,
            reason=reason,
        )
    except Exception:
        connection.rollback()
        raise


def mutate_dependency_source_without_hook(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    dependency_kind: DependencyKind,
    dependency_id: str,
    dependency_version: str,
    status: DependencyStatus,
) -> None:
    """Simulate a missed producer hook in the isolated source catalog."""
    _validate_dependency_mutation(
        dependency_version=dependency_version,
        status=status,
        queue_limit=1,
    )
    connection.execute(
        """
        INSERT INTO dependency_catalog_source (
          team_id,
          dependency_kind,
          dependency_id,
          dependency_version,
          status
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(team_id, dependency_kind, dependency_id)
        DO UPDATE SET
          dependency_version = excluded.dependency_version,
          status = excluded.status
        """,
        (
            team_id,
            dependency_kind,
            dependency_id,
            dependency_version,
            status,
        ),
    )
    connection.commit()


def reconcile_dependency_catalog(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    queue_limit: int = _DEFAULT_QUEUE_LIMIT,
) -> DependencyInvalidation:
    """Repair missed dependency hooks from the authoritative source digest."""
    _validate_queue_limit(queue_limit)
    connection.execute("BEGIN IMMEDIATE")
    try:
        source_digest = _catalog_digest(
            connection,
            table="dependency_catalog_source",
            team_id=team_id,
        )
        state = connection.execute(
            """
            SELECT catalog_revision, catalog_digest
            FROM team_subagent_dependency_state
            WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()
        if state is None:
            raise ValueError("team_subagent_dependency_state_missing")
        if state[1] == source_digest:
            result = _no_change_result(connection, team_id=team_id)
            connection.commit()
            return result

        changed = connection.execute(
            """
            SELECT
              source.dependency_kind,
              source.dependency_id,
              source.dependency_version,
              source.status
            FROM dependency_catalog_source AS source
            LEFT JOIN team_subagent_dependency_catalog AS projected
              ON projected.team_id = source.team_id
             AND projected.dependency_kind = source.dependency_kind
             AND projected.dependency_id = source.dependency_id
            WHERE source.team_id = ?
              AND (
                projected.dependency_id IS NULL
                OR projected.dependency_version <> source.dependency_version
                OR projected.status <> source.status
              )
            UNION ALL
            SELECT
              projected.dependency_kind,
              projected.dependency_id,
              projected.dependency_version,
              'deleted'
            FROM team_subagent_dependency_catalog AS projected
            LEFT JOIN dependency_catalog_source AS source
              ON source.team_id = projected.team_id
             AND source.dependency_kind = projected.dependency_kind
             AND source.dependency_id = projected.dependency_id
            WHERE projected.team_id = ?
              AND source.dependency_id IS NULL
              AND projected.status <> 'deleted'
            ORDER BY 1, 2
            """,
            (team_id, team_id),
        ).fetchall()
        if not changed:
            raise ValueError("catalog_digest_drift_without_changed_dependency")

        revision = int(state[0]) + 1
        affected_by_id: dict[str, tuple[str, str, str]] = {}
        for dependency_kind, dependency_id, dependency_version, status in changed:
            connection.execute(
                """
                INSERT INTO team_subagent_dependency_catalog (
                  team_id,
                  dependency_kind,
                  dependency_id,
                  dependency_version,
                  status
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(team_id, dependency_kind, dependency_id)
                DO UPDATE SET
                  dependency_version = excluded.dependency_version,
                  status = excluded.status
                """,
                (
                    team_id,
                    dependency_kind,
                    dependency_id,
                    dependency_version,
                    status,
                ),
            )
            reason = _dependency_reason(status)
            for subagent_id, agent_status, audience_mode in _reverse_index_rows(
                connection,
                team_id=team_id,
                dependency_kind=dependency_kind,
                dependency_id=dependency_id,
            ):
                affected_by_id[subagent_id] = (
                    agent_status,
                    audience_mode,
                    reason,
                )

        connection.execute(
            """
            UPDATE team_subagent_dependency_state
            SET catalog_revision = ?,
                catalog_digest = ?,
                updated_at = ?
            WHERE team_id = ?
            """,
            (revision, source_digest, _NOW, team_id),
        )
        stale_rows = [(subagent_id, status, audience_mode) for subagent_id, (status, audience_mode, _) in affected_by_id.items()]
        reasons = {details[2] for details in affected_by_id.values()}
        reason = next(iter(reasons)) if len(reasons) == 1 else "multiple_dependencies_changed"
        newly_stale = _mark_stale(
            connection,
            rows=stale_rows,
            reason=reason,
            revision=revision,
        )
        queue_depth, deferred = _refill_validation_queue(
            connection,
            team_id=team_id,
            queue_limit=queue_limit,
        )
        connection.execute(
            """
            UPDATE subagent_dependency_spike_metrics
            SET reconciler_repairs = reconciler_repairs + 1
            WHERE team_id = ?
            """,
            (team_id,),
        )
        connection.commit()
        return DependencyInvalidation(
            applied=True,
            catalog_revision=revision,
            matched_agents=len(affected_by_id),
            newly_stale_agents=newly_stale,
            queue_depth=queue_depth,
            deferred_stale_agents=deferred,
            reason=reason,
        )
    except Exception:
        connection.rollback()
        raise


def replace_validated_dependency_refs(
    connection: sqlite3.Connection,
    *,
    subagent_id: str,
    refs: Sequence[DependencyRef],
) -> str:
    """Replace one Agent's refs and validation snapshot in one transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        agent = connection.execute(
            "SELECT team_id FROM team_subagent WHERE id = ?",
            (subagent_id,),
        ).fetchone()
        if agent is None:
            raise ValueError("subagent_not_found")
        team_id = str(agent[0])
        unique_keys = {(ref.kind, ref.dependency_id) for ref in refs}
        if len(unique_keys) != len(refs):
            raise ValueError("duplicate_dependency_ref")
        for ref in refs:
            catalog_row = connection.execute(
                """
                SELECT dependency_version, status
                FROM team_subagent_dependency_catalog
                WHERE team_id = ?
                  AND dependency_kind = ?
                  AND dependency_id = ?
                """,
                (team_id, ref.kind, ref.dependency_id),
            ).fetchone()
            if catalog_row != (ref.version, "active"):
                raise ValueError("dependency_snapshot_not_current")

        connection.execute(
            "DELETE FROM team_subagent_dependency_ref WHERE subagent_id = ?",
            (subagent_id,),
        )
        connection.executemany(
            """
            INSERT INTO team_subagent_dependency_ref (
              subagent_id,
              team_id,
              dependency_kind,
              dependency_id,
              dependency_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    subagent_id,
                    team_id,
                    ref.kind,
                    ref.dependency_id,
                    ref.version,
                )
                for ref in refs
            ],
        )
        digest = _dependency_digest(refs)
        state = connection.execute(
            """
            SELECT global_rule_digest
            FROM team_subagent_dependency_state
            WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()
        validator = connection.execute(
            """
            SELECT validator_version
            FROM team_subagent_validator_state
            WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()
        if state is None or validator is None:
            raise ValueError("dependency_state_missing")
        connection.execute(
            """
            UPDATE team_subagent_validation
            SET referenced_dependency_digest = ?,
                global_rule_digest = ?,
                validator_version = ?,
                status = 'valid',
                eligible = 1,
                checked_at = ?
            WHERE subagent_id = ?
            """,
            (digest, state[0], validator[0], _NOW, subagent_id),
        )
        connection.execute(
            "DELETE FROM team_subagent_validation_stale WHERE subagent_id = ?",
            (subagent_id,),
        )
        connection.execute(
            "DELETE FROM team_subagent_validation_queue WHERE subagent_id = ?",
            (subagent_id,),
        )
        connection.commit()
        return digest
    except Exception:
        connection.rollback()
        raise


def resolve_runtime_dependency_gate(
    connection: sqlite3.Connection,
    *,
    subagent_id: str,
) -> RuntimeDependencyDecision:
    """Resolve current hard dependency state without trusting validation cache."""
    agent = connection.execute(
        "SELECT team_id FROM team_subagent WHERE id = ?",
        (subagent_id,),
    ).fetchone()
    if agent is None:
        return RuntimeDependencyDecision(False, "subagent_not_found")
    rows = connection.execute(
        """
        SELECT
          ref.dependency_kind,
          ref.dependency_id,
          catalog.status,
          COALESCE(rule.denied, 0)
        FROM team_subagent_dependency_ref AS ref
        LEFT JOIN team_subagent_dependency_catalog AS catalog
          ON catalog.team_id = ref.team_id
         AND catalog.dependency_kind = ref.dependency_kind
         AND catalog.dependency_id = ref.dependency_id
        LEFT JOIN team_subagent_global_rule AS rule
          ON rule.team_id = ref.team_id
         AND rule.target_kind = ref.dependency_kind
         AND rule.target_id = ref.dependency_id
        WHERE ref.subagent_id = ?
        ORDER BY ref.dependency_kind, ref.dependency_id
        """,
        (subagent_id,),
    ).fetchall()
    for _, _, status, denied in rows:
        if status is None:
            return RuntimeDependencyDecision(False, "dependency_missing")
        if status == "disabled":
            return RuntimeDependencyDecision(False, "dependency_disabled")
        if status == "deleted":
            return RuntimeDependencyDecision(False, "dependency_deleted")
        if bool(denied):
            return RuntimeDependencyDecision(False, "blocked_by_security_policy")
    return RuntimeDependencyDecision(True, None)


def invalidate_validator_version(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    validator_version: int,
    queue_limit: int = _DEFAULT_QUEUE_LIMIT,
) -> DependencyInvalidation:
    """Mark a full validator-scope change while bounding the work queue."""
    if validator_version < 1:
        raise ValueError("validator_version must be positive")
    _validate_queue_limit(queue_limit)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute(
            """
            SELECT validator_version
            FROM team_subagent_validator_state
            WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()
        if current is None:
            raise ValueError("team_subagent_validator_state_missing")
        if validator_version < int(current[0]):
            raise ValueError("validator_version_regression")
        if validator_version == int(current[0]):
            result = _no_change_result(connection, team_id=team_id)
            connection.commit()
            return result

        connection.execute(
            """
            UPDATE team_subagent_validator_state
            SET validator_version = ?
            WHERE team_id = ?
            """,
            (validator_version, team_id),
        )
        revision = _bump_catalog_revision(connection, team_id=team_id)
        affected = connection.execute(
            """
            SELECT id, status, audience_mode
            FROM team_subagent
            WHERE team_id = ?
            ORDER BY id
            """,
            (team_id,),
        ).fetchall()
        newly_stale = _mark_stale(
            connection,
            rows=affected,
            reason="validator_version_changed",
            revision=revision,
        )
        queue_depth, deferred = _refill_validation_queue(
            connection,
            team_id=team_id,
            queue_limit=queue_limit,
        )
        connection.execute(
            """
            UPDATE subagent_dependency_spike_metrics
            SET hook_bumps = hook_bumps + 1
            WHERE team_id = ?
            """,
            (team_id,),
        )
        connection.commit()
        return DependencyInvalidation(
            applied=True,
            catalog_revision=revision,
            matched_agents=len(affected),
            newly_stale_agents=newly_stale,
            queue_depth=queue_depth,
            deferred_stale_agents=deferred,
            reason="validator_version_changed",
        )
    except Exception:
        connection.rollback()
        raise


def process_validation_batch(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    batch_size: int,
    queue_limit: int = _DEFAULT_QUEUE_LIMIT,
) -> dict[str, Any]:
    """Simulate successful bounded worker processing and refill."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    _validate_queue_limit(queue_limit)
    connection.execute("BEGIN IMMEDIATE")
    try:
        batch = connection.execute(
            """
            SELECT subagent_id, priority
            FROM team_subagent_validation_queue
            WHERE team_id = ?
            ORDER BY priority, enqueued_at, subagent_id
            LIMIT ?
            """,
            (team_id, batch_size),
        ).fetchall()
        validator_version = connection.execute(
            """
            SELECT validator_version
            FROM team_subagent_validator_state
            WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()
        if validator_version is None:
            raise ValueError("team_subagent_validator_state_missing")
        for subagent_id, _ in batch:
            connection.execute(
                """
                UPDATE team_subagent_validation
                SET validator_version = ?,
                    checked_at = ?
                WHERE subagent_id = ?
                """,
                (validator_version[0], _NOW, subagent_id),
            )
            connection.execute(
                "DELETE FROM team_subagent_validation_stale WHERE subagent_id = ?",
                (subagent_id,),
            )
            connection.execute(
                "DELETE FROM team_subagent_validation_queue WHERE subagent_id = ?",
                (subagent_id,),
            )
        queue_depth, deferred = _refill_validation_queue(
            connection,
            team_id=team_id,
            queue_limit=queue_limit,
        )
        connection.commit()
        return {
            "processed": len(batch),
            "processed_priorities": [int(row[1]) for row in batch],
            "queue_depth": queue_depth,
            "deferred_stale_agents": deferred,
        }
    except Exception:
        connection.rollback()
        raise


def run_subagent_dependency_spike(
    *,
    agent_count: int = _DEFAULT_AGENT_COUNT,
    queue_limit: int = _DEFAULT_QUEUE_LIMIT,
) -> SubagentDependencySpikeReport:
    """Run the deterministic reverse-index and reconciler contract matrix."""
    _validate_queue_limit(queue_limit)

    targeting_connection = _fixture_connection(agent_count=agent_count)
    try:
        initial_ref_count = _count(
            targeting_connection,
            "SELECT COUNT(*) FROM team_subagent_dependency_ref WHERE team_id = ?",
            (_TEAM_ID,),
        )
        initial_fresh = _fresh_count(targeting_connection, team_id=_TEAM_ID)
        unreferenced = apply_dependency_change_hook(
            targeting_connection,
            team_id=_TEAM_ID,
            dependency_kind="team_tool",
            dependency_id="tool.unused",
            dependency_version="v2",
            status="active",
            queue_limit=queue_limit,
        )
        fresh_after_unreferenced = _fresh_count(
            targeting_connection,
            team_id=_TEAM_ID,
        )
        referenced = apply_dependency_change_hook(
            targeting_connection,
            team_id=_TEAM_ID,
            dependency_kind="team_tool",
            dependency_id="tool.shared",
            dependency_version="v2",
            status="disabled",
            queue_limit=queue_limit,
        )
        affected_gate = resolve_runtime_dependency_gate(
            targeting_connection,
            subagent_id=_agent_id(1),
        )
        unrelated_gate = resolve_runtime_dependency_gate(
            targeting_connection,
            subagent_id=_agent_id(100),
        )
        control_gate = resolve_runtime_dependency_gate(
            targeting_connection,
            subagent_id="sa_dependency_control",
        )
        query_plan = _reverse_query_plan(targeting_connection)
    finally:
        targeting_connection.close()

    global_connection = _fixture_connection(agent_count=agent_count)
    try:
        global_rule = apply_global_deny_hook(
            global_connection,
            team_id=_TEAM_ID,
            target_kind="native_tool",
            target_id="native.sql",
            denied=True,
            rule_version="deny-v2",
            queue_limit=queue_limit,
        )
        denied_gate = resolve_runtime_dependency_gate(
            global_connection,
            subagent_id=_agent_id(5),
        )
        non_native_gate = resolve_runtime_dependency_gate(
            global_connection,
            subagent_id=_agent_id(100),
        )
    finally:
        global_connection.close()

    reconcile_connection = _fixture_connection(agent_count=agent_count)
    try:
        pre_reconcile_gate = resolve_runtime_dependency_gate(
            reconcile_connection,
            subagent_id=_agent_id(10),
        )
        mutate_dependency_source_without_hook(
            reconcile_connection,
            team_id=_TEAM_ID,
            dependency_kind="team_skill",
            dependency_id="skill.missed",
            dependency_version="v2",
            status="disabled",
        )
        repaired = reconcile_dependency_catalog(
            reconcile_connection,
            team_id=_TEAM_ID,
            queue_limit=queue_limit,
        )
        post_reconcile_gate = resolve_runtime_dependency_gate(
            reconcile_connection,
            subagent_id=_agent_id(10),
        )
        repeated_reconcile = reconcile_dependency_catalog(
            reconcile_connection,
            team_id=_TEAM_ID,
            queue_limit=queue_limit,
        )
        repair_metric = _count(
            reconcile_connection,
            """
            SELECT reconciler_repairs
            FROM subagent_dependency_spike_metrics
            WHERE team_id = ?
            """,
            (_TEAM_ID,),
        )
    finally:
        reconcile_connection.close()

    backpressure_connection = _fixture_connection(agent_count=agent_count)
    try:
        global_invalidation = invalidate_validator_version(
            backpressure_connection,
            team_id=_TEAM_ID,
            validator_version=2,
            queue_limit=queue_limit,
        )
        initial_priorities = _priority_counts(
            backpressure_connection,
            team_id=_TEAM_ID,
        )
        batch = process_validation_batch(
            backpressure_connection,
            team_id=_TEAM_ID,
            batch_size=16,
            queue_limit=queue_limit,
        )
        remaining_stale = _stale_count(
            backpressure_connection,
            team_id=_TEAM_ID,
        )
    finally:
        backpressure_connection.close()

    replacement_connection = _fixture_connection(agent_count=agent_count)
    try:
        replacement_id = _agent_id(25)
        before_digest = _validation_digest(
            replacement_connection,
            subagent_id=replacement_id,
        )
        after_digest = replace_validated_dependency_refs(
            replacement_connection,
            subagent_id=replacement_id,
            refs=(
                DependencyRef("team_tool", "tool.stable", "v1"),
                DependencyRef("team_tool", "tool.swap_new", "v1"),
            ),
        )
        old_change = apply_dependency_change_hook(
            replacement_connection,
            team_id=_TEAM_ID,
            dependency_kind="team_tool",
            dependency_id="tool.swap_old",
            dependency_version="v2",
            status="disabled",
            queue_limit=queue_limit,
        )
        new_change = apply_dependency_change_hook(
            replacement_connection,
            team_id=_TEAM_ID,
            dependency_kind="team_tool",
            dependency_id="tool.swap_new",
            dependency_version="v2",
            status="disabled",
            queue_limit=queue_limit,
        )
    finally:
        replacement_connection.close()

    targeted_invalidation = {
        "unreferenced_change": asdict(unreferenced),
        "referenced_disable": asdict(referenced),
        "initial_fresh_agents": initial_fresh,
        "fresh_after_unreferenced_change": fresh_after_unreferenced,
        "affected_runtime_gate": asdict(affected_gate),
        "unrelated_runtime_gate": asdict(unrelated_gate),
        "control_team_runtime_gate": asdict(control_gate),
    }
    global_rule_invalidation = {
        "native_deny": asdict(global_rule),
        "matching_runtime_gate": asdict(denied_gate),
        "non_matching_runtime_gate": asdict(non_native_gate),
    }
    reconciler = {
        "pre_reconcile_runtime_gate": asdict(pre_reconcile_gate),
        "repair": asdict(repaired),
        "post_reconcile_runtime_gate": asdict(post_reconcile_gate),
        "repeat": asdict(repeated_reconcile),
        "repair_metric": repair_metric,
    }
    backpressure = {
        "global_invalidation": asdict(global_invalidation),
        "initial_queue_priorities": initial_priorities,
        "processed_batch": batch,
        "remaining_stale_agents": remaining_stale,
        "queue_limit": queue_limit,
    }
    exact_ref_replacement = {
        "digest_changed": before_digest != after_digest,
        "old_dependency_change": asdict(old_change),
        "new_dependency_change": asdict(new_change),
    }
    reverse_plan_indexed = any("idx_team_subagent_dependency_reverse" in step for step in query_plan)
    checks = {
        "unreferenced_dependency_preserves_freshness": (
            unreferenced.matched_agents == 0 and initial_fresh == agent_count and fresh_after_unreferenced == agent_count
        ),
        "referenced_disable_targets_exactly_three": (referenced.matched_agents == 3 and referenced.newly_stale_agents == 3),
        "runtime_hard_gate_precedes_revalidation": (
            not affected_gate.allowed and affected_gate.denial_reason == "dependency_disabled" and unrelated_gate.allowed
        ),
        "team_boundary_is_preserved": control_gate.allowed,
        "global_deny_targets_relevant_native_refs": (
            global_rule.matched_agents == 20
            and not denied_gate.allowed
            and denied_gate.denial_reason == "blocked_by_security_policy"
            and non_native_gate.allowed
        ),
        "reconciler_repairs_missed_hook": (
            pre_reconcile_gate.allowed
            and repaired.applied
            and repaired.matched_agents == 2
            and not post_reconcile_gate.allowed
            and repair_metric == 1
        ),
        "reconciler_is_idempotent": (not repeated_reconcile.applied and repeated_reconcile.matched_agents == 0 and repair_metric == 1),
        "queue_depth_is_bounded": (
            global_invalidation.matched_agents == agent_count
            and global_invalidation.queue_depth == queue_limit
            and batch["queue_depth"] == queue_limit
        ),
        "queue_prioritizes_enabled_teamwide": (
            bool(batch["processed_priorities"])
            and batch["processed_priorities"] == sorted(batch["processed_priorities"])
            and batch["processed_priorities"][0] == 10
        ),
        "queue_refills_without_losing_stale_work": (
            batch["processed"] == 16 and remaining_stale == agent_count - 16 and batch["deferred_stale_agents"] == agent_count - 16 - queue_limit
        ),
        "validated_refs_replace_exactly": (before_digest != after_digest and old_change.matched_agents == 0 and new_change.matched_agents == 1),
        "reverse_lookup_uses_index": reverse_plan_indexed,
        "production_mutation_is_none": True,
    }
    return SubagentDependencySpikeReport(
        schema_version=1,
        scope="synthetic_isolated_dependency_reverse_index",
        methodology={
            "catalog_source": "synthetic authoritative source and isolated projection",
            "freshness": "referenced dependency digest plus applicable global rule digest",
            "hard_gate": "current dependency status and security deny are resolved at runtime",
            "production_activation": "none",
            "production_mutation": "none",
            "queue": "bounded queue backed by a separate stale ledger and deterministic refill",
            "reconciler": "digest drift repairs missed hooks; identical digest is a no-op",
        },
        environment={
            "architecture": platform.machine(),
            "platform": platform.system(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        dataset={
            "agents": agent_count,
            "control_team_agents": 1,
            "dependency_refs": initial_ref_count,
            "native_ref_agents": 20,
            "shared_tool_ref_agents": 3,
            "missed_skill_ref_agents": 2,
            "queue_limit": queue_limit,
        },
        targeted_invalidation=targeted_invalidation,
        global_rule_invalidation=global_rule_invalidation,
        reconciler=reconciler,
        backpressure=backpressure,
        exact_ref_replacement=exact_ref_replacement,
        query_plan=query_plan,
        checks=checks,
    )


def _fixture_connection(*, agent_count: int) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    initialize_dependency_spike_fixture(
        connection,
        agent_count=agent_count,
    )
    return connection


def _validate_dependency_mutation(
    *,
    dependency_version: str,
    status: str,
    queue_limit: int,
) -> None:
    if not dependency_version:
        raise ValueError("dependency_version must not be empty")
    if status not in _DEPENDENCY_STATUSES:
        raise ValueError("invalid_dependency_status")
    _validate_queue_limit(queue_limit)


def _validate_queue_limit(queue_limit: int) -> None:
    if queue_limit < 1:
        raise ValueError("queue_limit must be positive")


def _catalog_digest(
    connection: sqlite3.Connection,
    *,
    table: str,
    team_id: str,
) -> str:
    if table not in {
        "dependency_catalog_source",
        "team_subagent_dependency_catalog",
    }:
        raise ValueError("unsupported_catalog_table")
    rows = connection.execute(
        f"""
        SELECT
          dependency_kind,
          dependency_id,
          dependency_version,
          status
        FROM {table}
        WHERE team_id = ?
        ORDER BY dependency_kind, dependency_id
        """,
        (team_id,),
    ).fetchall()
    return _stable_digest(rows)


def _global_rule_digest(
    connection: sqlite3.Connection,
    *,
    team_id: str,
) -> str:
    rows = connection.execute(
        """
        SELECT target_kind, target_id, denied, rule_version
        FROM team_subagent_global_rule
        WHERE team_id = ?
        ORDER BY target_kind, target_id
        """,
        (team_id,),
    ).fetchall()
    return _stable_digest(rows)


def _dependency_digest(refs: Sequence[DependencyRef]) -> str:
    rows = sorted((ref.kind, ref.dependency_id, ref.version) for ref in refs)
    return _stable_digest(rows)


def _stable_digest(rows: Iterable[Sequence[Any]]) -> str:
    payload = json.dumps(
        [list(row) for row in rows],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def _bump_catalog_revision(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    catalog_digest: str | None = None,
    global_rule_digest: str | None = None,
) -> int:
    state = connection.execute(
        """
        SELECT catalog_revision, catalog_digest, global_rule_digest
        FROM team_subagent_dependency_state
        WHERE team_id = ?
        """,
        (team_id,),
    ).fetchone()
    if state is None:
        raise ValueError("team_subagent_dependency_state_missing")
    revision = int(state[0]) + 1
    connection.execute(
        """
        UPDATE team_subagent_dependency_state
        SET catalog_revision = ?,
            catalog_digest = ?,
            global_rule_digest = ?,
            updated_at = ?
        WHERE team_id = ?
        """,
        (
            revision,
            catalog_digest if catalog_digest is not None else state[1],
            global_rule_digest if global_rule_digest is not None else state[2],
            _NOW,
            team_id,
        ),
    )
    return revision


def _reverse_index_rows(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    dependency_kind: str,
    dependency_id: str,
) -> list[tuple[str, str, str]]:
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT agent.id, agent.status, agent.audience_mode
            FROM team_subagent_dependency_ref AS ref
            JOIN team_subagent AS agent
              ON agent.id = ref.subagent_id
             AND agent.team_id = ref.team_id
            WHERE ref.team_id = ?
              AND ref.dependency_kind = ?
              AND ref.dependency_id = ?
            ORDER BY ref.subagent_id
            """,
            (team_id, dependency_kind, dependency_id),
        ).fetchall()
    ]


def _mark_stale(
    connection: sqlite3.Connection,
    *,
    rows: Sequence[tuple[str, str, str]],
    reason: str,
    revision: int,
) -> int:
    if not rows:
        return 0
    subagent_ids = [row[0] for row in rows]
    placeholders = ",".join("?" for _ in subagent_ids)
    already_stale = _count(
        connection,
        f"""
        SELECT COUNT(*)
        FROM team_subagent_validation_stale
        WHERE subagent_id IN ({placeholders})
        """,
        tuple(subagent_ids),
    )
    connection.executemany(
        """
        INSERT INTO team_subagent_validation_stale (
          subagent_id,
          team_id,
          reason,
          invalidated_revision,
          priority,
          created_at
        )
        SELECT ?, team_id, ?, ?, ?, ?
        FROM team_subagent
        WHERE id = ?
        ON CONFLICT(subagent_id)
        DO UPDATE SET
          reason = excluded.reason,
          invalidated_revision = MAX(
            team_subagent_validation_stale.invalidated_revision,
            excluded.invalidated_revision
          ),
          priority = MIN(
            team_subagent_validation_stale.priority,
            excluded.priority
          )
        """,
        [
            (
                subagent_id,
                reason,
                revision,
                _validation_priority(
                    status=status,
                    audience_mode=audience_mode,
                    reason=reason,
                ),
                _NOW,
                subagent_id,
            )
            for subagent_id, status, audience_mode in rows
        ],
    )
    connection.execute(
        f"""
        UPDATE team_subagent_validation_queue
        SET reason = (
              SELECT stale.reason
              FROM team_subagent_validation_stale AS stale
              WHERE stale.subagent_id =
                team_subagent_validation_queue.subagent_id
            ),
            invalidated_revision = (
              SELECT stale.invalidated_revision
              FROM team_subagent_validation_stale AS stale
              WHERE stale.subagent_id =
                team_subagent_validation_queue.subagent_id
            ),
            priority = MIN(
              priority,
              (
                SELECT stale.priority
                FROM team_subagent_validation_stale AS stale
                WHERE stale.subagent_id =
                  team_subagent_validation_queue.subagent_id
              )
            )
        WHERE subagent_id IN ({placeholders})
        """,
        tuple(subagent_ids),
    )
    return len(rows) - already_stale


def _validation_priority(
    *,
    status: str,
    audience_mode: str,
    reason: str,
) -> int:
    if status != "enabled":
        return 40
    if reason in {"dependency_deleted", "dependency_disabled", "security_deny"}:
        return 0
    if audience_mode == "teamwide":
        return 10
    if audience_mode == "custom":
        return 20
    return 30


def _refill_validation_queue(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    queue_limit: int,
) -> tuple[int, int]:
    depth = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM team_subagent_validation_queue
        WHERE team_id = ?
        """,
        (team_id,),
    )
    available = max(0, queue_limit - depth)
    if available:
        rows = connection.execute(
            """
            SELECT
              stale.subagent_id,
              stale.team_id,
              stale.reason,
              stale.invalidated_revision,
              stale.priority,
              stale.created_at
            FROM team_subagent_validation_stale AS stale
            LEFT JOIN team_subagent_validation_queue AS queued
              ON queued.subagent_id = stale.subagent_id
            WHERE stale.team_id = ?
              AND queued.subagent_id IS NULL
            ORDER BY
              stale.priority,
              stale.created_at,
              stale.subagent_id
            LIMIT ?
            """,
            (team_id, available),
        ).fetchall()
        connection.executemany(
            """
            INSERT INTO team_subagent_validation_queue (
              subagent_id,
              team_id,
              reason,
              invalidated_revision,
              priority,
              enqueued_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    queue_depth = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM team_subagent_validation_queue
        WHERE team_id = ?
        """,
        (team_id,),
    )
    deferred = _stale_count(connection, team_id=team_id) - queue_depth
    return queue_depth, deferred


def _no_change_result(
    connection: sqlite3.Connection,
    *,
    team_id: str,
) -> DependencyInvalidation:
    revision = _count(
        connection,
        """
        SELECT catalog_revision
        FROM team_subagent_dependency_state
        WHERE team_id = ?
        """,
        (team_id,),
    )
    queue_depth = _count(
        connection,
        """
        SELECT COUNT(*)
        FROM team_subagent_validation_queue
        WHERE team_id = ?
        """,
        (team_id,),
    )
    return DependencyInvalidation(
        applied=False,
        catalog_revision=revision,
        matched_agents=0,
        newly_stale_agents=0,
        queue_depth=queue_depth,
        deferred_stale_agents=(_stale_count(connection, team_id=team_id) - queue_depth),
        reason=None,
    )


def _dependency_reason(status: str) -> str:
    if status == "disabled":
        return "dependency_disabled"
    if status == "deleted":
        return "dependency_deleted"
    return "dependency_changed"


def _fresh_count(
    connection: sqlite3.Connection,
    *,
    team_id: str,
) -> int:
    return _count(
        connection,
        """
        SELECT COUNT(*)
        FROM team_subagent_validation AS validation
        LEFT JOIN team_subagent_validation_stale AS stale
          ON stale.subagent_id = validation.subagent_id
        WHERE validation.team_id = ?
          AND validation.eligible = 1
          AND stale.subagent_id IS NULL
        """,
        (team_id,),
    )


def _stale_count(
    connection: sqlite3.Connection,
    *,
    team_id: str,
) -> int:
    return _count(
        connection,
        """
        SELECT COUNT(*)
        FROM team_subagent_validation_stale
        WHERE team_id = ?
        """,
        (team_id,),
    )


def _priority_counts(
    connection: sqlite3.Connection,
    *,
    team_id: str,
) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT priority, COUNT(*)
        FROM team_subagent_validation_queue
        WHERE team_id = ?
        GROUP BY priority
        ORDER BY priority
        """,
        (team_id,),
    ).fetchall()
    return {str(priority): int(count) for priority, count in rows}


def _validation_digest(
    connection: sqlite3.Connection,
    *,
    subagent_id: str,
) -> str:
    row = connection.execute(
        """
        SELECT referenced_dependency_digest
        FROM team_subagent_validation
        WHERE subagent_id = ?
        """,
        (subagent_id,),
    ).fetchone()
    if row is None:
        raise ValueError("subagent_validation_missing")
    return str(row[0])


def _reverse_query_plan(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT ref.subagent_id
        FROM team_subagent_dependency_ref AS ref
        WHERE ref.team_id = ?
          AND ref.dependency_kind = ?
          AND ref.dependency_id = ?
        ORDER BY ref.subagent_id
        """,
        (_TEAM_ID, "team_tool", "tool.shared"),
    ).fetchall()
    return [str(row[3]) for row in rows]


def _count(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> int:
    row = connection.execute(sql, params).fetchone()
    if row is None:
        raise ValueError("count_query_returned_no_row")
    return int(row[0])


def _agent_id(index: int) -> str:
    return f"sa_dependency_{index:05d}"
