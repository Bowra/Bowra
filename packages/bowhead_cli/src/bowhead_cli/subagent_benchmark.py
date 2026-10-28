"""Deterministic Stage 0A catalog-read benchmark for the planned Subagent schema."""

from __future__ import annotations

import math
import platform
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter_ns
from typing import Any

_ALLOWED_AGENT_COUNTS = frozenset({1, 50, 500, 5000})
_ALLOWED_MEMBER_COUNTS = frozenset({1, 100, 1000})
_BENCHMARK_SEED = 20260727
_PAGE_SIZE = 50
_PAGE_50_OFFSET = 49 * _PAGE_SIZE


@dataclass(frozen=True)
class BenchmarkWorkload:
    name: str
    sql: str
    params: tuple[Any, ...]


@dataclass(frozen=True)
class SubagentBenchmarkReport:
    schema_version: int
    generated_at: str
    scope: str
    methodology: dict[str, str]
    environment: dict[str, Any]
    dataset: dict[str, Any]
    workloads: list[dict[str, Any]]
    checks: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_subagent_catalog_benchmark(
    *,
    agent_count: int,
    member_count: int,
    iterations: int = 20,
) -> SubagentBenchmarkReport:
    """Generate an isolated fixture and benchmark planned catalog reads."""
    _validate_dimensions(agent_count=agent_count, member_count=member_count)
    if not 5 <= iterations <= 200:
        raise ValueError("iterations must be between 5 and 200")

    with tempfile.TemporaryDirectory(prefix="bowhead-subagent-benchmark-") as temp_dir:
        db_path = Path(temp_dir) / "benchmark.db"
        dataset = generate_subagent_benchmark_fixture(
            db_path,
            agent_count=agent_count,
            member_count=member_count,
        )
        workloads, page_50_available = _catalog_workloads(db_path)
        results = [
            _benchmark_workload(
                db_path,
                workload=workload,
                iterations=iterations,
            )
            for workload in workloads
        ]
        cursor_unique = _check_first_two_pages_unique(db_path)
        environment = _environment(db_path)

    return SubagentBenchmarkReport(
        schema_version=1,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        scope="synthetic_isolated_catalog_read",
        methodology={
            "fixture": "deterministic temporary SQLite database; no production data",
            "new_connection": "opens a query-only connection for each measured query",
            "warm_connection": "reuses one query-only connection after one warm-up query",
            "production_slo": "not established by this synthetic benchmark",
        },
        environment=environment,
        dataset=dataset,
        workloads=results,
        checks={
            "all_query_plans_indexed": all(result["plan_ok"] for result in results),
            "first_two_pages_unique": cursor_unique,
            "page_50_available": page_50_available,
            "page_50_requirement_met": agent_count != 5000 or page_50_available,
            "fixture_counts_match": dataset["agents"] == agent_count and dataset["members"] == member_count,
        },
    )


def generate_subagent_benchmark_fixture(
    db_path: Path,
    *,
    agent_count: int,
    member_count: int,
) -> dict[str, Any]:
    """Create one deterministic synthetic fixture; never reads production data."""
    _validate_dimensions(agent_count=agent_count, member_count=member_count)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_SCHEMA)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        stats = _seed_fixture(
            connection,
            agent_count=agent_count,
            member_count=member_count,
        )
        connection.commit()
    finally:
        connection.close()
    return stats


def _seed_fixture(
    connection: sqlite3.Connection,
    *,
    agent_count: int,
    member_count: int,
) -> dict[str, Any]:
    team_id = "team_benchmark"
    actor_id = "usr_0000"
    base_time = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    connection.execute("INSERT INTO teams(id) VALUES (?)", (team_id,))

    users: list[tuple[str, str]] = []
    memberships: list[tuple[str, str, str]] = []
    for index in range(member_count):
        user_id = f"usr_{index:04d}"
        status = "inactive" if index > 0 and index % 20 == 0 else "active"
        users.append((user_id, status))
        memberships.append((team_id, user_id, status))
    connection.executemany("INSERT INTO users(id,status) VALUES (?,?)", users)
    connection.executemany(
        "INSERT INTO team_members(team_id,user_id,status) VALUES (?,?,?)",
        memberships,
    )

    groups = [(f"grp_{index:04d}", team_id, "active") for index in range(201)]
    connection.executemany("INSERT INTO groups(id,team_id,status) VALUES (?,?,?)", groups)
    connection.execute(
        "INSERT INTO group_memberships(team_id,group_id,principal_type,principal_id,status) VALUES (?,?,?,?,?)",
        (team_id, "grp_0000", "user", actor_id, "active"),
    )

    subagents: list[tuple[Any, ...]] = []
    validations: list[tuple[str, str, str, str]] = []
    grants: list[tuple[Any, ...]] = []
    audience_counts: Counter[str] = Counter()
    validation_counts: Counter[str] = Counter()
    roster_size_counts: Counter[int] = Counter()
    roster_sizes = (1, 10, 50, 200)

    for index in range(agent_count):
        subagent_id = f"osa_bench_{index:06d}"
        owner_id = f"usr_{index % member_count:04d}"
        bucket = index % 20
        audience = "teamwide" if bucket < 13 else "custom" if bucket < 17 else "private"
        audience_counts[audience] += 1
        disabled = index % 20 == 19
        quarantined = index > 0 and index % 100 == 0
        status = "disabled" if disabled or quarantined else "enabled"
        lifecycle = "quarantined" if quarantined else "active"
        updated_at = (base_time - timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
        if index % 2:
            name = f"research-agent-{index:06d}"
            display_name = f"Research Agent {index}"
            description = "Synthetic English benchmark record"
        else:
            name = f"research-agent-cn-{index:06d}"
            display_name = f"研究助手 {index}"
            description = "合成中文基准记录"
        visibility = "team_wide" if audience == "teamwide" else "creator_only"
        subagents.append(
            (
                subagent_id,
                team_id,
                name,
                display_name,
                description,
                owner_id,
                audience,
                visibility,
                lifecycle,
                status,
                updated_at,
            ),
        )

        if index % 20 == 0:
            validation_counts["stale_or_pending"] += 1
        else:
            validation_status = "valid" if index % 20 < 14 else "warning" if index % 20 < 17 else "invalid"
            validation_counts[validation_status] += 1
            validations.append((subagent_id, team_id, validation_status, updated_at))

        if audience == "custom":
            roster_size = roster_sizes[(index // 20) % len(roster_sizes)]
            roster_size_counts[roster_size] += 1
            principals = _custom_principals(
                roster_size=roster_size,
                member_count=member_count,
                shared_with_actor=index % 2 == 0,
            )
            for grant_index, (principal_type, principal_id) in enumerate(principals):
                grants.append(
                    _grant(
                        grant_id=f"grant_allow_{index:06d}_{grant_index:03d}",
                        team_id=team_id,
                        subagent_id=subagent_id,
                        principal_type=principal_type,
                        principal_id=principal_id,
                        effect="allow",
                        created_at=updated_at,
                    ),
                )
        if audience != "private" and index > 0 and index % 37 == 0:
            grants.append(
                _grant(
                    grant_id=f"grant_deny_{index:06d}",
                    team_id=team_id,
                    subagent_id=subagent_id,
                    principal_type="user",
                    principal_id=actor_id,
                    effect="deny",
                    created_at=updated_at,
                ),
            )

    connection.executemany(
        "INSERT INTO team_subagent("
        "id,team_id,name,display_name,description,owner_user_id,audience_mode,"
        "visibility,lifecycle_state,status,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        subagents,
    )
    connection.executemany(
        "INSERT INTO team_subagent_validation(subagent_id,team_id,status,checked_at) VALUES (?,?,?,?)",
        validations,
    )
    connection.executemany(
        "INSERT INTO resource_grants("
        "id,team_id,resource_kind,resource_id,principal_type,principal_id,"
        "permission,effect,created_at,expires_at,revoked_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        grants,
    )
    return {
        "profile": "synthetic_provisional_not_production",
        "seed": _BENCHMARK_SEED,
        "agents": agent_count,
        "members": member_count,
        "groups": len(groups),
        "grants": len(grants),
        "audience": {
            "private": audience_counts["private"],
            "custom": audience_counts["custom"],
            "teamwide": audience_counts["teamwide"],
        },
        "custom_rosters": {
            "profile_sizes": list(roster_sizes),
            "actual_counts": {str(size): roster_size_counts[size] for size in roster_sizes if roster_size_counts[size]},
        },
        "validation": {
            "valid": validation_counts["valid"],
            "warning": validation_counts["warning"],
            "invalid": validation_counts["invalid"],
            "stale_or_pending": validation_counts["stale_or_pending"],
        },
    }


def _validate_dimensions(*, agent_count: int, member_count: int) -> None:
    if agent_count not in _ALLOWED_AGENT_COUNTS:
        raise ValueError(f"agents must be one of {sorted(_ALLOWED_AGENT_COUNTS)}")
    if member_count not in _ALLOWED_MEMBER_COUNTS:
        raise ValueError(f"members must be one of {sorted(_ALLOWED_MEMBER_COUNTS)}")


def _custom_principals(
    *,
    roster_size: int,
    member_count: int,
    shared_with_actor: bool,
) -> list[tuple[str, str]]:
    principals: list[tuple[str, str]] = []
    user_start = 0 if shared_with_actor else 1
    for index in range(user_start, min(member_count, roster_size + user_start)):
        principals.append(("user", f"usr_{index:04d}"))
        if len(principals) == roster_size:
            return principals
    group_start = 0 if shared_with_actor and not principals else 1
    for index in range(group_start, 201):
        principals.append(("group", f"grp_{index:04d}"))
        if len(principals) == roster_size:
            return principals
    raise ValueError("synthetic principal pool is smaller than requested roster")


def _grant(
    *,
    grant_id: str,
    team_id: str,
    subagent_id: str,
    principal_type: str,
    principal_id: str,
    effect: str,
    created_at: str,
) -> tuple[Any, ...]:
    return (
        grant_id,
        team_id,
        "team_subagent",
        subagent_id,
        principal_type,
        principal_id,
        "use",
        effect,
        created_at,
        None,
        None,
    )


def _catalog_workloads(db_path: Path) -> tuple[list[BenchmarkWorkload], bool]:
    team_id = "team_benchmark"
    actor_id = "usr_0000"
    first_id = "osa_bench_000000"
    workloads = [
        BenchmarkWorkload(
            name="mine_first_page",
            sql=_MINE_SQL,
            params=(team_id, actor_id, _PAGE_SIZE),
        ),
        BenchmarkWorkload(
            name="team_directory_first_page",
            sql=_TEAM_DIRECTORY_SQL,
            params=(actor_id, actor_id, team_id, _PAGE_SIZE),
        ),
        BenchmarkWorkload(
            name="shared_with_me",
            sql=_SHARED_WITH_ME_SQL,
            params=(
                actor_id,
                actor_id,
                actor_id,
                actor_id,
                team_id,
                actor_id,
                _PAGE_SIZE,
            ),
        ),
        BenchmarkWorkload(
            name="catalog_search",
            sql=_SEARCH_SQL,
            params=(actor_id, actor_id, team_id, "%研究%", "%研究%", _PAGE_SIZE),
        ),
        BenchmarkWorkload(
            name="catalog_counts",
            sql=_COUNTS_SQL,
            params=(team_id,),
        ),
        BenchmarkWorkload(
            name="consumer_detail",
            sql=_DETAIL_SQL,
            params=(team_id, first_id),
        ),
    ]
    cursor = _page_50_cursor(db_path)
    if cursor is not None:
        workloads.append(
            BenchmarkWorkload(
                name="team_directory_page_50",
                sql=_TEAM_DIRECTORY_KEYSET_SQL,
                params=(actor_id, actor_id, team_id, cursor[0], cursor[0], cursor[1], _PAGE_SIZE),
            ),
        )
    return workloads, cursor is not None


def _page_50_cursor(db_path: Path) -> tuple[str, str] | None:
    with _read_connection(db_path) as connection:
        row = connection.execute(
            _TEAM_DIRECTORY_CURSOR_SQL,
            ("usr_0000", "usr_0000", "team_benchmark", _PAGE_50_OFFSET),
        ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def _benchmark_workload(
    db_path: Path,
    *,
    workload: BenchmarkWorkload,
    iterations: int,
) -> dict[str, Any]:
    connection_samples: list[float] = []
    row_count = 0
    for _ in range(iterations):
        with _read_connection(db_path) as connection:
            elapsed, row_count = _timed_query(connection, workload)
            connection_samples.append(elapsed)

    with _read_connection(db_path) as connection:
        _timed_query(connection, workload)
        warm_samples = [_timed_query(connection, workload)[0] for _ in range(iterations)]
        plan = [
            str(row[3])
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {workload.sql}",  # noqa: S608
                workload.params,
            )
        ]
    plan_issues = _plan_issues(plan)
    return {
        "name": workload.name,
        "row_count": row_count,
        "new_connection_ms": _latency_summary(connection_samples),
        "warm_connection_ms": _latency_summary(warm_samples),
        "query_plan": plan,
        "plan_ok": not plan_issues,
        "plan_issues": plan_issues,
    }


def _timed_query(
    connection: sqlite3.Connection,
    workload: BenchmarkWorkload,
) -> tuple[float, int]:
    started = perf_counter_ns()
    rows = connection.execute(workload.sql, workload.params).fetchall()
    elapsed_ms = (perf_counter_ns() - started) / 1_000_000
    return elapsed_ms, len(rows)


def _latency_summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "min": round(ordered[0], 4),
        "p50": round(_percentile(ordered, 0.50), 4),
        "p95": round(_percentile(ordered, 0.95), 4),
        "max": round(ordered[-1], 4),
    }


def _percentile(ordered: list[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _plan_issues(plan: list[str]) -> list[str]:
    issues: list[str] = []
    for detail in plan:
        normalized = detail.upper()
        scans_subagents = "SCAN SA" in normalized or "SCAN TEAM_SUBAGENT" in normalized
        if scans_subagents and "USING INDEX" not in normalized:
            issues.append("unbounded_team_subagent_scan")
        if any(f"SCAN {alias}" in normalized for alias in ("GRANT_ALLOW", "GRANT_DENY")) and "USING INDEX" not in normalized:
            issues.append("unbounded_resource_grants_scan")
    return sorted(set(issues))


def _check_first_two_pages_unique(db_path: Path) -> bool:
    with _read_connection(db_path) as connection:
        first = connection.execute(
            _TEAM_DIRECTORY_SQL,
            ("usr_0000", "usr_0000", "team_benchmark", _PAGE_SIZE),
        ).fetchall()
        if not first:
            return True
        last_updated_at, last_id = str(first[-1][5]), str(first[-1][0])
        second = connection.execute(
            _TEAM_DIRECTORY_KEYSET_SQL,
            (
                "usr_0000",
                "usr_0000",
                "team_benchmark",
                last_updated_at,
                last_updated_at,
                last_id,
                _PAGE_SIZE,
            ),
        ).fetchall()
    return not ({str(row[0]) for row in first} & {str(row[0]) for row in second})


def _environment(db_path: Path) -> dict[str, Any]:
    with _read_connection(db_path) as connection:
        pragmas = {
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
            "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
            "cache_size": int(connection.execute("PRAGMA cache_size").fetchone()[0]),
        }
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "byteorder": sys.byteorder,
        "pragmas": pragmas,
        "cache_note": "new_connection is not an OS page-cache cold benchmark",
    }


@contextmanager
def _read_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


_SCHEMA = """
CREATE TABLE teams (
  id TEXT PRIMARY KEY
);
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL
);
CREATE TABLE team_members (
  team_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(team_id,user_id)
);
CREATE INDEX idx_team_members_user_status
  ON team_members(user_id,status,team_id);
CREATE TABLE groups (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE group_memberships (
  team_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  principal_type TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(group_id,principal_type,principal_id)
);
CREATE INDEX idx_group_memberships_principal
  ON group_memberships(team_id,principal_type,principal_id,status,group_id);
CREATE TABLE team_subagent (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  name TEXT NOT NULL,
  display_name TEXT NOT NULL,
  description TEXT NOT NULL,
  owner_user_id TEXT,
  audience_mode TEXT NOT NULL,
  visibility TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_team_subagent_team_owner
  ON team_subagent(team_id,owner_user_id,lifecycle_state,status);
CREATE INDEX idx_team_subagent_team_updated
  ON team_subagent(team_id,updated_at DESC,id DESC);
CREATE INDEX idx_team_subagent_team_audience
  ON team_subagent(team_id,audience_mode,lifecycle_state,status);
CREATE TABLE resource_grants (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  principal_type TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  permission TEXT NOT NULL,
  effect TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT
);
CREATE INDEX idx_resource_grants_lookup
  ON resource_grants(team_id,resource_kind,resource_id,revoked_at);
CREATE INDEX idx_resource_grants_principal
  ON resource_grants(team_id,principal_type,principal_id,revoked_at);
CREATE TABLE team_subagent_validation (
  subagent_id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  status TEXT NOT NULL,
  checked_at TEXT NOT NULL
);
CREATE INDEX idx_team_subagent_validation_team_status
  ON team_subagent_validation(team_id,status,checked_at);
"""

_ACTIVE_OWNER_JOIN = """
JOIN users owner ON owner.id=sa.owner_user_id AND owner.status='active'
JOIN team_members owner_tm
  ON owner_tm.team_id=sa.team_id
 AND owner_tm.user_id=sa.owner_user_id
 AND owner_tm.status='active'
"""

_DENY_SQL = """
NOT EXISTS (
  SELECT 1
  FROM resource_grants grant_deny
  WHERE grant_deny.team_id=sa.team_id
    AND grant_deny.resource_kind='team_subagent'
    AND grant_deny.resource_id=sa.id
    AND grant_deny.permission='use'
    AND grant_deny.effect='deny'
    AND grant_deny.revoked_at IS NULL
    AND grant_deny.expires_at IS NULL
    AND (
      (grant_deny.principal_type='user' AND grant_deny.principal_id=?)
      OR (
        grant_deny.principal_type='group'
        AND EXISTS (
          SELECT 1 FROM group_memberships deny_gm
          WHERE deny_gm.team_id=sa.team_id
            AND deny_gm.group_id=grant_deny.principal_id
            AND deny_gm.principal_type='user'
            AND deny_gm.principal_id=?
            AND deny_gm.status='active'
        )
      )
    )
)
"""

_MINE_SQL = f"""
SELECT sa.id,sa.display_name,sa.description,sa.audience_mode,sa.status,sa.updated_at
FROM team_subagent sa
{_ACTIVE_OWNER_JOIN}
WHERE sa.team_id=?
  AND sa.owner_user_id=?
  AND sa.lifecycle_state='active'
ORDER BY sa.updated_at DESC,sa.id DESC
LIMIT ?
"""

_TEAM_DIRECTORY_BASE = f"""
FROM team_subagent sa
{_ACTIVE_OWNER_JOIN}
WHERE {_DENY_SQL}
  AND sa.team_id=?
  AND sa.audience_mode='teamwide'
  AND sa.lifecycle_state='active'
  AND sa.status='enabled'
"""

_TEAM_DIRECTORY_SQL = f"""
SELECT sa.id,sa.display_name,sa.description,sa.audience_mode,sa.status,sa.updated_at
{_TEAM_DIRECTORY_BASE}
ORDER BY sa.updated_at DESC,sa.id DESC
LIMIT ?
"""

_TEAM_DIRECTORY_CURSOR_SQL = f"""
SELECT sa.updated_at,sa.id
{_TEAM_DIRECTORY_BASE}
ORDER BY sa.updated_at DESC,sa.id DESC
LIMIT 1 OFFSET ?
"""

_TEAM_DIRECTORY_KEYSET_SQL = f"""
SELECT sa.id,sa.display_name,sa.description,sa.audience_mode,sa.status,sa.updated_at
{_TEAM_DIRECTORY_BASE}
  AND (sa.updated_at < ? OR (sa.updated_at = ? AND sa.id < ?))
ORDER BY sa.updated_at DESC,sa.id DESC
LIMIT ?
"""

_SHARED_WITH_ME_SQL = f"""
SELECT sa.id,sa.display_name,sa.description,sa.audience_mode,sa.status,sa.updated_at
FROM team_subagent sa
{_ACTIVE_OWNER_JOIN}
WHERE EXISTS (
  SELECT 1
  FROM resource_grants grant_allow
  WHERE grant_allow.team_id=sa.team_id
    AND grant_allow.resource_kind='team_subagent'
    AND grant_allow.resource_id=sa.id
    AND grant_allow.permission='use'
    AND grant_allow.effect='allow'
    AND grant_allow.revoked_at IS NULL
    AND grant_allow.expires_at IS NULL
    AND (
      (grant_allow.principal_type='user' AND grant_allow.principal_id=?)
      OR (
        grant_allow.principal_type='group'
        AND EXISTS (
          SELECT 1 FROM group_memberships allow_gm
          WHERE allow_gm.team_id=sa.team_id
            AND allow_gm.group_id=grant_allow.principal_id
            AND allow_gm.principal_type='user'
            AND allow_gm.principal_id=?
            AND allow_gm.status='active'
        )
      )
    )
)
  AND {_DENY_SQL}
  AND sa.team_id=?
  AND sa.owner_user_id<>?
  AND sa.audience_mode='custom'
  AND sa.lifecycle_state='active'
  AND sa.status='enabled'
ORDER BY sa.updated_at DESC,sa.id DESC
LIMIT ?
"""

_SEARCH_SQL = f"""
SELECT sa.id,sa.display_name,sa.description,sa.audience_mode,sa.status,sa.updated_at
{_TEAM_DIRECTORY_BASE}
  AND (sa.display_name LIKE ? OR sa.description LIKE ?)
ORDER BY sa.updated_at DESC,sa.id DESC
LIMIT ?
"""

_COUNTS_SQL = """
SELECT audience_mode,COUNT(*)
FROM team_subagent
WHERE team_id=? AND lifecycle_state='active' AND status='enabled'
GROUP BY audience_mode
"""

_DETAIL_SQL = f"""
SELECT sa.id,sa.display_name,sa.description,sa.audience_mode,sa.status,sa.updated_at,
       validation.status
FROM team_subagent sa
{_ACTIVE_OWNER_JOIN}
LEFT JOIN team_subagent_validation validation
  ON validation.team_id=sa.team_id AND validation.subagent_id=sa.id
WHERE sa.team_id=? AND sa.id=? AND sa.lifecycle_state='active'
"""
