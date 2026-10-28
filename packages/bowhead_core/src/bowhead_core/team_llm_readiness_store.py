"""Persistence helpers for V2 enrollment, baselines and risk continuity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.audit_actions import AuditAction
from bowhead_core.team_llm_evidence import Capability, canonical_json
from bowhead_core.team_llm_readiness import RequiredAssignment

if TYPE_CHECKING:
    from pathlib import Path

ROLLOUT_GENERATION = "model-discovery-v2-r1"


@dataclass(frozen=True, slots=True)
class V2Enrollment:
    team_id: str
    config_version: int
    semantic_hash: str
    assignments: tuple[dict[str, Any], ...]
    enrolled_at: str


def safe_assignment_snapshot(
    assignments: tuple[RequiredAssignment, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "role_key": row.role_key,
            "provider_id": row.provider_id,
            "model_id": row.model_id,
            "capability": row.capability,
            "config_signature": row.config_signature,
            "testability_contract_version": (row.testability_contract_version),
        }
        for row in assignments
    ]


def assignment_semantic_hash(
    assignments: tuple[RequiredAssignment, ...],
) -> str:
    rows = safe_assignment_snapshot(assignments)
    rows.sort(
        key=lambda row: (
            row["role_key"],
            row["provider_id"],
            row["model_id"],
            row["capability"],
            row["config_signature"],
        )
    )
    return hashlib.sha256(canonical_json(rows).encode()).hexdigest()


def acceptance_key_from_row(
    row: aiosqlite.Row,
) -> tuple[str, str, Capability, str, str, str, str]:
    """必须与 `RequiredAssignment.acceptance_key_for` 逐位对齐（含 risk_code）。

    同一判据的两份实现是这套代码反复出问题的形状；两边少一位就是「认领了却仍然拦」
    或「认领了 A 却顺带放行 B」，且两者都静默。**公开导出**：apply 服务此前内联拼了
    第三份 6 元组（漏 risk_code），造成 check 面 risk_accepted / apply 面重算
    required ⇒ 语义哈希必不等 ⇒ 确定性 409 死循环（116 实测，08-24 客户报的
    「加模型后应用 409」的第二根因——当时只修了 evidence_id 那一半）。
    """
    return (
        str(row["provider_id"]),
        str(row["model_id"]),
        str(row["capability"]),  # type: ignore[return-value]
        str(row["role_key"]),
        str(row["config_signature"]),
        str(row["testability_contract_version"]),
        str(row["risk_code"]),
    )


class TeamLlmReadinessStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def ensure_enrollment(
        self,
        *,
        team_id: str,
        config_version: int,
        assignments: tuple[RequiredAssignment, ...],
        source: str,
        actor_user_id: str,
        now: datetime | None = None,
    ) -> tuple[V2Enrollment, bool]:
        timestamp = now or datetime.now(UTC)
        snapshot = safe_assignment_snapshot(assignments)
        semantic_hash = assignment_semantic_hash(assignments)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO team_llm_v2_enrollments(
                    team_id, enrolled_config_version,
                    enrolled_semantic_hash, assignments_json,
                    rollout_generation, enrollment_source, enrolled_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    team_id,
                    config_version,
                    semantic_hash,
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    ROLLOUT_GENERATION,
                    source,
                    timestamp.isoformat().replace("+00:00", "Z"),
                ),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    """
                    INSERT INTO admin_audit_log(
                        id, actor_user_id, team_id, action,
                        target_kind, target_id, scope,
                        payload_json, ts
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"aud_{uuid4().hex}",
                        actor_user_id,
                        team_id,
                        AuditAction.LLM_V2_ENROLLED,
                        "team_llm_config",
                        team_id,
                        "team",
                        json.dumps(
                            {
                                "config_version": config_version,
                                "semantic_hash": semantic_hash,
                                "rollout_generation": (ROLLOUT_GENERATION),
                            },
                            separators=(",", ":"),
                        ),
                        timestamp.isoformat().replace(
                            "+00:00",
                            "Z",
                        ),
                    ),
                )
            row = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_v2_enrollments
                    WHERE team_id = ?
                    """,
                    (team_id,),
                )
            ).fetchone()
            await db.commit()
        assert row is not None
        return (
            V2Enrollment(
                team_id=team_id,
                config_version=int(row["enrolled_config_version"]),
                semantic_hash=str(row["enrolled_semantic_hash"]),
                assignments=tuple(json.loads(str(row["assignments_json"]))),
                enrolled_at=str(row["enrolled_at"]),
            ),
            cursor.rowcount == 1,
        )

    async def active_acceptance_keys(
        self,
        *,
        team_id: str,
    ) -> frozenset[tuple[str, str, Capability, str, str, str, str]]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT acceptance.provider_id, acceptance.model_id,
                           acceptance.capability, acceptance.role_key,
                           acceptance.config_signature,
                           acceptance.testability_contract_version,
                           acceptance.risk_code
                    FROM team_llm_risk_acceptances AS acceptance
                    LEFT JOIN team_llm_risk_acceptance_retirements AS retired
                      ON retired.risk_acceptance_id = acceptance.id
                    WHERE acceptance.team_id = ?
                      AND retired.risk_acceptance_id IS NULL
                    """,
                    (team_id,),
                )
            ).fetchall()
        return frozenset(acceptance_key_from_row(row) for row in rows)

    async def active_grandfather_scopes(
        self,
        *,
        team_id: str,
    ) -> frozenset[tuple[str, str, Capability, str]]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            baseline = await (
                await db.execute(
                    """
                    SELECT assignments_json
                    FROM team_llm_v2_migration_baselines
                    WHERE team_id = ?
                    """,
                    (team_id,),
                )
            ).fetchone()
            invalidation = await (
                await db.execute(
                    """
                    SELECT team_id
                    FROM team_llm_v2_migration_baseline_invalidations
                    WHERE team_id = ?
                    """,
                    (team_id,),
                )
            ).fetchone()
        if baseline is None or invalidation is not None:
            return frozenset()
        rows = json.loads(str(baseline["assignments_json"]))
        return frozenset(
            (
                str(row["provider_id"]),
                str(row["model_id"]),
                str(row["capability"]),  # type: ignore[misc]
                str(row["config_signature"]),
            )
            for row in rows
        )

    async def has_migration_baseline(self, *, team_id: str) -> bool:
        async with open_shared_ctx(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT 1
                    FROM team_llm_v2_migration_baselines
                    WHERE team_id = ?
                    """,
                    (team_id,),
                )
            ).fetchone()
        return row is not None
