"""内置子代理的团队级覆盖（delta）存储。

内置子代理管理 sprint（docs/sprints/builtin-subagent-admin/tech-design.md §2.2）。

内置定义（base）永远只跟代码走：``manifests/system/*.yaml`` → ``AgentRegistry`` →
进程级 ``PROFILES``。本 store 只持有「某团队改了什么」（delta）：

- 无行 = 完全使用代码默认值
- 删行 = 恢复默认
- 列为 ``NULL`` = 该字段不覆盖（``skills`` 的「显式覆盖为空」是 ``'[]'``，不是 ``NULL``）

表由 alembic ``0009_builtin_subagent_override`` 创建，``0035_scheduled_agent_binding``
追加职责字段；本 store **不做建表 DDL**
（docs/design/数据库迁移.md §开发者工作流第 3 条）。

所有读写以 ``team_id`` 为键，跨团队天然隔离（无 client 传入的 scope key → 无 IDOR）。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from bowhead_core._db import open_shared_ctx

BUILTIN_SUBAGENT_OVERRIDE_RESOURCE_KIND = "builtin_subagent_override"

_TABLE = "team_builtin_subagent_override"

_COLUMNS = (
    "team_id",
    "name",
    "enabled",
    "display_name",
    "description",
    "model",
    "skills_json",
    "prompt_append",
    "max_iterations",
    "primary_capability",
    "row_version",
    "updated_by",
    "created_at",
    "updated_at",
)


class BuiltinSubagentOverrideVersionConflictError(RuntimeError):
    """``expected_version`` 与库中 ``row_version`` 不符（route 映射 409）。"""

    def __init__(self, name: str, current_version: int | None) -> None:
        super().__init__(f"builtin subagent override {name!r} version conflict (current={current_version})")
        self.name = name
        self.current_version = current_version


class BuiltinSubagentDisplayNameConflictError(ValueError):
    """A system override would create a user-visible team name collision."""


@dataclass(frozen=True)
class BuiltinSubagentOverride:
    """一个内置子代理在某团队的 delta。``None`` = 该字段不覆盖。"""

    team_id: str
    name: str
    enabled: bool = True
    display_name: str | None = None
    description: str | None = None
    model: str | None = None
    skills: list[str] | None = None
    prompt_append: str | None = None
    max_iterations: int | None = None
    primary_capability: str | None = None
    row_version: int = 1
    updated_by: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def overridden_fields(self) -> list[str]:
        """被显式覆盖的字段名（供 UI 打「已改动」badge）。

        ``enabled`` 没有「不覆盖」态（无行即 enabled），所以只有 ``False`` 才算改动。
        """
        fields: list[str] = []
        if not self.enabled:
            fields.append("enabled")
        for attr in (
            "display_name",
            "description",
            "model",
            "skills",
            "prompt_append",
            "max_iterations",
            "primary_capability",
        ):
            if getattr(self, attr) is not None:
                fields.append(attr)
        return fields

    @property
    def is_default(self) -> bool:
        """True = 这行与「没有这行」等价（可安全删除）。"""
        return not self.overridden_fields


def _row_to_override(row: aiosqlite.Row) -> BuiltinSubagentOverride:
    raw_skills = row["skills_json"]
    skills: list[str] | None = None
    if raw_skills is not None:
        try:
            parsed = json.loads(raw_skills)
        except (TypeError, ValueError):
            parsed = []
        skills = [str(s) for s in parsed] if isinstance(parsed, list) else []
    return BuiltinSubagentOverride(
        team_id=row["team_id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        display_name=row["display_name"],
        description=row["description"],
        model=row["model"],
        skills=skills,
        prompt_append=row["prompt_append"],
        max_iterations=row["max_iterations"],
        primary_capability=row["primary_capability"],
        row_version=int(row["row_version"]),
        updated_by=row["updated_by"],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


class BuiltinSubagentOverrideStore:
    """``team_builtin_subagent_override`` 的 CRUD。

    写路径用 ``BEGIN IMMEDIATE``，把「版本比对 + UPSERT + audit」放在同一事务里 —— 镜像
    ``TeamSubagentStore`` 的做法（audit 与业务写要么一起成功要么一起回滚，避免「改了但没留痕」
    或「留了痕但没改」）。
    """

    def __init__(
        self,
        db_path: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("builtin_subagent_override_clock_must_return_aware_datetime")
        return value.astimezone(UTC).isoformat()

    # ── Reads ──

    async def list_for_team(self, team_id: str) -> dict[str, BuiltinSubagentOverride]:
        """本团队的全部 delta，按内置 name 索引。turn 启动预取用（≤ 内置个数行）。"""
        if not team_id:
            return {}
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE} WHERE team_id=?",  # noqa: S608 -- Fixed internal table/column names
                (team_id,),
            ) as cur:
                rows = await cur.fetchall()
        return {str(r["name"]): _row_to_override(r) for r in rows}

    async def get(self, team_id: str, name: str) -> BuiltinSubagentOverride | None:
        if not team_id or not name:
            return None
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE} WHERE team_id=? AND name=?",  # noqa: S608 -- Fixed internal table/column names
                (team_id, name),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_override(row) if row else None

    # ── Writes ──

    async def upsert(
        self,
        override: BuiltinSubagentOverride,
        *,
        expected_version: int | None,
        actor_user_id: str,
        audit_action: str,
    ) -> BuiltinSubagentOverride:
        """全量写入一个 delta（不是 PATCH —— 调用方送来的就是目标终态）。

        ``expected_version``：``None`` = 首次创建（库中必须无行，有行则冲突）；有值 = 必须与
        库中 ``row_version`` 相等。不符抛
        :class:`BuiltinSubagentOverrideVersionConflictError`。
        """
        now = self._now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    f"SELECT row_version, created_at, display_name FROM {_TABLE} WHERE team_id=? AND name=?",  # noqa: S608 -- Fixed internal table name
                    (override.team_id, override.name),
                ) as cur:
                    existing = await cur.fetchone()
                current_version = int(existing["row_version"]) if existing else None
                if expected_version != current_version:
                    raise BuiltinSubagentOverrideVersionConflictError(
                        override.name,
                        current_version,
                    )
                next_version = (current_version or 0) + 1
                created_at = str(existing["created_at"]) if existing else now
                if await self._table_exists_conn(db, "agent_name_claim"):
                    await self._move_display_name_claim_conn(
                        db,
                        team_id=override.team_id,
                        name=override.name,
                        old_display_name=(str(existing["display_name"]) if existing and existing["display_name"] else None),
                        new_display_name=(override.display_name if override.display_name and override.display_name.strip() else None),
                        timestamp=now,
                    )
                await db.execute(
                    f"""
                    INSERT INTO {_TABLE}
                      (team_id, name, enabled, display_name, description, model,
                      skills_json, prompt_append, max_iterations,
                       primary_capability, row_version, updated_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(team_id, name) DO UPDATE SET
                      enabled=excluded.enabled,
                      display_name=excluded.display_name,
                      description=excluded.description,
                      model=excluded.model,
                      skills_json=excluded.skills_json,
                      prompt_append=excluded.prompt_append,
                      max_iterations=excluded.max_iterations,
                      primary_capability=excluded.primary_capability,
                      row_version=excluded.row_version,
                      updated_by=excluded.updated_by,
                      updated_at=excluded.updated_at
                    """,  # noqa: S608 -- Fixed internal table name
                    (
                        override.team_id,
                        override.name,
                        1 if override.enabled else 0,
                        override.display_name,
                        override.description,
                        override.model,
                        (None if override.skills is None else json.dumps(override.skills, ensure_ascii=False)),
                        override.prompt_append,
                        override.max_iterations,
                        override.primary_capability,
                        next_version,
                        actor_user_id,
                        created_at,
                        now,
                    ),
                )
                await self._write_audit_conn(
                    db,
                    actor_user_id=actor_user_id,
                    audit_action=audit_action,
                    team_id=override.team_id,
                    name=override.name,
                    payload={"overridden_fields": override.overridden_fields},
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return replace(
            override,
            row_version=next_version,
            updated_by=actor_user_id,
            created_at=created_at,
            updated_at=now,
        )

    async def delete(
        self,
        team_id: str,
        name: str,
        *,
        actor_user_id: str,
        audit_action: str,
    ) -> bool:
        """恢复默认 = 删行。返回 False 表示本来就没有 delta（幂等）。"""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    f"SELECT display_name FROM {_TABLE} WHERE team_id=? AND name=?",  # noqa: S608 -- Fixed internal table name
                    (team_id, name),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing and existing["display_name"] and await self._table_exists_conn(db, "agent_name_claim"):
                    await self._move_display_name_claim_conn(
                        db,
                        team_id=team_id,
                        name=name,
                        old_display_name=str(existing["display_name"]),
                        new_display_name=None,
                        timestamp=self._now(),
                    )
                cur = await db.execute(
                    f"DELETE FROM {_TABLE} WHERE team_id=? AND name=?",  # noqa: S608 -- Fixed internal table name
                    (team_id, name),
                )
                removed = cur.rowcount > 0
                if removed:
                    await self._write_audit_conn(
                        db,
                        actor_user_id=actor_user_id,
                        audit_action=audit_action,
                        team_id=team_id,
                        name=name,
                        payload=None,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return removed

    async def _move_display_name_claim_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        name: str,
        old_display_name: str | None,
        new_display_name: str | None,
        timestamp: str,
    ) -> None:
        old_key = _effective_name_key(old_display_name) if old_display_name else None
        new_key = _effective_name_key(new_display_name) if new_display_name else None
        if old_key == new_key:
            return
        if new_key is not None:
            if new_key in await self._other_system_name_keys_conn(db, team_id=team_id, current_name=name):
                raise BuiltinSubagentDisplayNameConflictError("display_name_unavailable")
            async with db.execute(
                "SELECT name,display_name FROM team_subagent WHERE team_id=?",
                (team_id,),
            ) as cursor:
                team_rows = await cursor.fetchall()
            if any(_effective_name_key(str(row["display_name"] or row["name"])) == new_key for row in team_rows):
                raise BuiltinSubagentDisplayNameConflictError("display_name_unavailable")
            async with db.execute(
                "SELECT holder_kind FROM agent_name_claim WHERE team_id=? AND normalized_key=?",
                (team_id, new_key),
            ) as cursor:
                claims = await cursor.fetchall()
            if any(str(claim["holder_kind"]) != "system" for claim in claims):
                raise BuiltinSubagentDisplayNameConflictError("display_name_unavailable")
            await db.execute(
                """
                INSERT INTO agent_name_claim(
                  team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
                ) VALUES (?,'*',?,'system',?,NULL,?)
                ON CONFLICT(team_id,locale,normalized_key) DO NOTHING
                """,
                (team_id, new_key, f"system:{name}", timestamp),
            )
        if old_key is None:
            return
        if old_key in await self._other_system_name_keys_conn(db, team_id=team_id, current_name=name):
            return
        async with db.execute(
            "SELECT holder_kind FROM agent_name_claim WHERE team_id=? AND locale='*' AND normalized_key=?",
            (team_id, old_key),
        ) as cursor:
            old_claim = await cursor.fetchone()
        if old_claim is not None and str(old_claim["holder_kind"]) == "system":
            async with db.execute(
                "SELECT id,name,display_name FROM team_subagent WHERE team_id=? ORDER BY id",
                (team_id,),
            ) as cursor:
                matching_agents = [
                    str(row["id"]) for row in await cursor.fetchall() if _effective_name_key(str(row["display_name"] or row["name"])) == old_key
                ]
            if len(matching_agents) == 1:
                await db.execute(
                    """
                    UPDATE agent_name_claim
                    SET holder_kind='agent',holder_id=?,agent_id=?,claimed_at=?
                    WHERE team_id=? AND locale='*' AND normalized_key=?
                    """,
                    (matching_agents[0], matching_agents[0], timestamp, team_id, old_key),
                )
            elif matching_agents:
                group_id = f"legacy:{old_key[:20]}"
                await db.execute(
                    """
                    UPDATE agent_name_claim
                    SET holder_kind='legacy_conflict',holder_id=?,agent_id=NULL,claimed_at=?
                    WHERE team_id=? AND locale='*' AND normalized_key=?
                    """,
                    (group_id, timestamp, team_id, old_key),
                )
                await db.executemany(
                    """
                    INSERT INTO agent_name_conflict_group(team_id,normalized_key,agent_id,detected_at)
                    VALUES (?,?,?,?)
                    ON CONFLICT(team_id,normalized_key,agent_id) DO UPDATE SET resolved_at=NULL
                    """,
                    [(team_id, old_key, agent_id, timestamp) for agent_id in matching_agents],
                )
            else:
                await db.execute(
                    "DELETE FROM agent_name_claim WHERE team_id=? AND locale='*' AND normalized_key=?",
                    (team_id, old_key),
                )

    async def _other_system_name_keys_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        current_name: str,
    ) -> set[str]:
        from bowhead_core.system_agent_marketplace import (
            SYSTEM_FUTURE_RESERVED_DISPLAY_NAMES,
            system_agent_reserved_display_names,
        )

        try:
            from bowhead_core.agents.registry import AgentRegistry

            names = list(AgentRegistry.bootstrap().list_all())
        except Exception:
            names = []
        async with db.execute(
            f"SELECT name,display_name FROM {_TABLE} WHERE team_id=? AND name<>?",  # noqa: S608 -- fixed internal table
            (team_id, current_name),
        ) as cursor:
            overrides = {str(row["name"]): str(row["display_name"]) for row in await cursor.fetchall() if str(row["display_name"] or "").strip()}
        reserved = set(SYSTEM_FUTURE_RESERVED_DISPLAY_NAMES)
        for system_name in names:
            if system_name == current_name:
                continue
            reserved.update(system_agent_reserved_display_names(system_name))
            if override_name := overrides.get(system_name):
                reserved.add(override_name)
        return {_effective_name_key(value) for value in reserved}

    async def _table_exists_conn(self, db: aiosqlite.Connection, table_name: str) -> bool:
        async with db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
            (table_name,),
        ) as cursor:
            return await cursor.fetchone() is not None

    # ── Audit (same transaction as business write) ──

    async def _write_audit_conn(
        self,
        db: aiosqlite.Connection,
        *,
        actor_user_id: str | None,
        audit_action: str,
        team_id: str,
        name: str,
        payload: dict[str, Any] | None,
    ) -> None:
        if not actor_user_id:
            return
        from bowhead_core.user_store import UserStore

        await UserStore(self.db_path).write_admin_audit_log(
            actor_user_id=actor_user_id,
            team_id=team_id,
            action=audit_action,
            target_kind=BUILTIN_SUBAGENT_OVERRIDE_RESOURCE_KIND,
            target_id=name,
            target_team_id=team_id,
            scope="team",
            payload={"name": name, **(payload or {})},
            conn=db,
        )


def _effective_name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized):
        raise BuiltinSubagentDisplayNameConflictError("display_name_unavailable")
    collapsed = " ".join(normalized.strip().split()).casefold()
    if not collapsed:
        raise BuiltinSubagentDisplayNameConflictError("display_name_unavailable")
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


__all__ = [
    "BUILTIN_SUBAGENT_OVERRIDE_RESOURCE_KIND",
    "BuiltinSubagentOverride",
    "BuiltinSubagentOverrideStore",
    "BuiltinSubagentOverrideVersionConflictError",
]
