"""Read-only audit for the authenticated team model boundary.

The audit never consults deployment routing configuration and never invokes a
provider.  It opens SQLite in read-only/query-only mode, decrypts each team's
own saved config, validates the same schema used by the Gateway resolver, and
constructs the in-memory Router to catch deterministic build failures.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bowhead_core.llm_config_crypto import decrypt_llm_config
from bowhead_core.team_llm_config import (
    TeamLlmConfigError,
    build_router_from_v2_config,
    is_v2_config,
    migrate_legacy_config_to_v2,
    validate_v2_config,
)

AuditResult = Literal["team_safe_success", "team_unavailable", "resolver_error"]
AuditReason = Literal[
    "resolved",
    "config_missing",
    "config_invalid",
    "config_decrypt_failed",
    "router_build_failed",
]


@dataclass(frozen=True)
class TeamModelBoundaryAuditRow:
    """Secret-free result for one active team."""

    team_id: str
    config_version: int
    result: AuditResult
    reason: AuditReason
    catalog_resolvable: bool
    router_resolvable: bool
    provider_count: int
    model_count: int


@dataclass(frozen=True)
class TeamModelBoundaryAuditReport:
    """Stable aggregate plus actionable, secret-free team rows."""

    schema_version: int
    generated_at: str
    read_only: bool
    writes_performed: int
    provider_calls_performed: int
    teams_checked: int
    results: dict[str, int]
    reasons: dict[str, int]
    teams: tuple[TeamModelBoundaryAuditRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def audit_team_model_boundary(
    db_path: Path,
    *,
    team_id: str | None = None,
    now: datetime | None = None,
) -> TeamModelBoundaryAuditReport:
    """Audit active team-owned model configs without writes or provider calls."""
    if team_id is not None and not team_id.strip():
        raise ValueError("team_id must be non-empty when provided")
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        _require_team_columns(connection)
        sql = "SELECT id, llm_config_encrypted, llm_config_version FROM teams WHERE status='active'"
        params: tuple[str, ...] = ()
        if team_id is not None:
            sql += " AND id=?"
            params = (team_id,)
        sql += " ORDER BY id"
        source_rows = connection.execute(sql, params).fetchall()
        if team_id is not None and not source_rows:
            raise ValueError(f"active team not found: {team_id}")
        rows = tuple(_audit_row(row) for row in source_rows)
    finally:
        connection.close()

    return TeamModelBoundaryAuditReport(
        schema_version=1,
        generated_at=checked_at.isoformat(),
        read_only=True,
        writes_performed=0,
        provider_calls_performed=0,
        teams_checked=len(rows),
        results=dict(sorted(Counter(row.result for row in rows).items())),
        reasons=dict(sorted(Counter(row.reason for row in rows).items())),
        teams=rows,
    )


def _audit_row(row: sqlite3.Row) -> TeamModelBoundaryAuditRow:
    team_id = str(row["id"])
    version = int(row["llm_config_version"] or 0)
    stored = str(row["llm_config_encrypted"] or "")
    if not stored:
        return _unavailable(team_id, version, "config_missing")
    try:
        raw_config = decrypt_llm_config(stored)
    except (TypeError, ValueError):
        return _error(team_id, version, "config_decrypt_failed")
    if not raw_config:
        return _unavailable(team_id, version, "config_missing")
    try:
        config = raw_config if is_v2_config(raw_config) else migrate_legacy_config_to_v2(raw_config)
        validate_v2_config(config, allow_legacy_duplicate_fallbacks=True)
    except (TeamLlmConfigError, TypeError, ValueError):
        return _unavailable(team_id, version, "config_invalid")

    try:
        build_router_from_v2_config(config)
    except Exception:  # noqa: BLE001 - report a finite reason, never raw adapter details
        return _error(team_id, version, "router_build_failed")

    providers = config.get("providers") or {}
    model_count = sum(len(spec.get("models") or []) for spec in providers.values() if isinstance(spec, dict))
    return TeamModelBoundaryAuditRow(
        team_id=team_id,
        config_version=version,
        result="team_safe_success",
        reason="resolved",
        catalog_resolvable=True,
        router_resolvable=True,
        provider_count=len(providers),
        model_count=model_count,
    )


def _unavailable(
    team_id: str,
    version: int,
    reason: Literal["config_missing", "config_invalid"],
) -> TeamModelBoundaryAuditRow:
    return TeamModelBoundaryAuditRow(
        team_id=team_id,
        config_version=version,
        result="team_unavailable",
        reason=reason,
        catalog_resolvable=False,
        router_resolvable=False,
        provider_count=0,
        model_count=0,
    )


def _error(
    team_id: str,
    version: int,
    reason: Literal["config_decrypt_failed", "router_build_failed"],
) -> TeamModelBoundaryAuditRow:
    return TeamModelBoundaryAuditRow(
        team_id=team_id,
        config_version=version,
        result="resolver_error",
        reason=reason,
        catalog_resolvable=False,
        router_resolvable=False,
        provider_count=0,
        model_count=0,
    )


def _require_team_columns(connection: sqlite3.Connection) -> None:
    found = {str(row["name"]) for row in connection.execute("PRAGMA table_info(teams)").fetchall()}
    required = {
        "id",
        "status",
        "llm_config_encrypted",
        "llm_config_version",
    }
    missing = required - found
    if missing:
        raise ValueError(f"teams schema missing columns: {', '.join(sorted(missing))}")
