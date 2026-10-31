"""CustomSubagentStore — user-defined subagent profile CRUD (#90 Step 1).

Project-level only (per user); workspace 级 defer 到 v2. 见 docs/custom-subagent-design.md.

⚠️ LEGACY（2026-07-03 收编，docs/skills-tools-ssot.md §5-G2/G11）：本系统是 team_subagents
（CCA P3，team-scoped + visibility）的前身，grain 错误（per-user 表）且从未有过前端入口，
但路由+Lead 广告+派发接线全活——构成无人认领的可执行暗面。现整体挂
`BOWHEAD_USER_CUSTOM_SUBAGENTS`（默认 OFF）：OFF 时 gateway 不建 store、不挂路由，
Lead 广告与 resolve 因 store=None 自然熄灭。个人 subagent 的产品路径 =
team_subagents 的 `visibility=creator_only`。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core.subagent_prompt_limits import (
    MAX_PROMPT_CHARS_PERSONAL,
    MAX_PROMPT_TOKENS,
    system_prompt_token_overflow,
)


def user_custom_subagents_enabled() -> bool:
    """Legacy per-user custom subagents 总开关（默认 OFF，见模块 docstring）."""
    return os.environ.get("BOWHEAD_USER_CUSTOM_SUBAGENTS", "0") == "1"


SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_subagents (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    name                TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    description         TEXT NOT NULL,
    system_prompt       TEXT NOT NULL,
    allowed_tools       TEXT NOT NULL DEFAULT '[]',
    auto_read_skills    TEXT NOT NULL DEFAULT '[]',
    max_iterations      INTEGER NOT NULL DEFAULT 10,
    status              TEXT NOT NULL DEFAULT 'active',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_custom_subagents_user
    ON custom_subagents(user_id, status);
"""

# Reserved built-in names; custom agents must not conflict.
# subagent-unification: + consultant (system business-consultant peer; prevent a
# user custom subagent from shadowing the system name — review ROUTE-7).
RESERVED_NAMES = {
    "compute",
    "modeler",
    "analyst",
    "echo",
    "lead",
    "consultant",
    "debate",  # retired system capability; do not allow a custom row to revive it
}

# Destructive-tool denylist: filter even tools explicitly listed in users' allowed_tools.
# subagent-unification: fix separator bug; registered names use underscores, while old dotted prefixes
# (bi.update_) matched nothing via startswith, letting everything through.
#   - bi_update_ / bi_delete_ / bi_create_: formerly matched bi_update_measures /
#     bi_delete_measures / bi_create_measures / bi_update_dimensions; all four were removed on
#     2026-07-21 (datarc has no write API). **Deliberately retain** these prefixes: unlike skill_delete_'s
#     dead prefix, these express BI write semantics; future implemented write tools
#     must be blocked by default. Retention costs nothing (user allowed_tools entries are merely filtered).
#   - canvas_delete_: formerly matched canvas_delete_section, retired with Canvas Writer (implementation removed 2026-09-30).
#     **Deliberately retained** for the same reason as bi_*: future canvas deletion tools must be blocked by default.
#   - skill_delete_: grep found no actual tool; remove the dead prefix (tech-design §2.2).
# NB: filter_destructive is defense-in-depth #2; the real guard is registry deny-by-default plus
# intersection with the allowlist.
DESTRUCTIVE_TOOL_PREFIXES = (
    "bi_update_",
    "bi_delete_",
    "bi_create_",
    "canvas_delete_",
)

# report-engine Phase-2 WP-F: report-WRITE tools must NEVER be grantable to
# user-created / team custom subagents. The re-review (security P0) showed
# canvas_add_section/compose_final/patch/reorder are analyst-bound and NOT
# destructive-prefixed, so a user could author an team agent that writes/overwrites
# arbitrary report content — and the only runtime block was the peer-name guard
# Phase-2 removes. They share no prefix, so fence by exact name (used by BOTH
# grant routes: filter_destructive below + the team route's _NATIVE_ALLOWLIST).
# `write_report` is the default writer-loop surface and is fenced here.
# Holding these is a `report-writer` trust-class privilege, never a CCA grant.
# WP-E (2026-06-17): canvas_add_section/canvas_compose_final are RETIRED tools
# (handlers deleted), but their names are KEPT in this fence defensively — so a
# future re-introduction of a tool by those names can never become a CCA grant.
# 2026-09-09 (user decision, tech-design: docs/sprints/custom-agent-report-pen-2026-09-09/):
# **Remove `write_report` from this fence**. The original rationale was that Phase-2 removed the sole runtime peer-name guard;
# its current write path now has server-stamped owner (`_resolve_writer_id` = spawn id, not forgeable by LLM),
# revision CAS, bake, and kind/superseded checks. `_write_guard` explicitly describes its security model as
# per-section owner enforcement. Team agents cannot take system peer names (creation rejects them),
# so name-based trust-class lookup cannot impersonate them. The four remaining tools **lack an owner model**, relying on
# `_write_guard`'s trust-class gate (CCA cannot resolve trust class ⇒ fail-closed), untouched in this change.
# Rollback: restore "write_report" to this set (authoring immediately hides it; filtering strips existing refs).
REPORT_WRITE_TOOLS = frozenset(
    {
        "canvas_add_section",  # retired (WP-E) — kept fenced defensively
        "canvas_compose_final",  # retired (WP-E) — kept fenced defensively
        "canvas_patch_section",  # retired with Canvas Writer (2026-09-30) — kept fenced defensively
        "canvas_reorder_sections",  # retired with Canvas Writer (2026-09-30) — kept fenced defensively
        # canvas-update-turn batch B2-2: can hide any prior-turn chart without an owner model ⇒ same tier as the two above.
        "remove_chart",
        "edit_chart",
    }
)

# Allowed name pattern
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

# Personal custom-subagent character precheck. Numbers and the language-independent gate live in subagent_prompt_limits;
# aliases here preserve historical imports (gateway routes / team_subagent_validation).
MAX_PROMPT_LEN = MAX_PROMPT_CHARS_PERSONAL
MAX_ITERATIONS_CAP = 50


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass
class CustomSubagent:
    id: str
    user_id: str
    name: str
    display_name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    auto_read_skills: list[str] = field(default_factory=list)
    max_iterations: int = 10
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


def _row_to_agent(row: aiosqlite.Row) -> CustomSubagent:
    try:
        allowed = json.loads(row["allowed_tools"] or "[]")
    except (json.JSONDecodeError, TypeError):
        allowed = []
    try:
        skills = json.loads(row["auto_read_skills"] or "[]")
    except (json.JSONDecodeError, TypeError):
        skills = []
    return CustomSubagent(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        display_name=row["display_name"],
        description=row["description"],
        system_prompt=row["system_prompt"],
        allowed_tools=allowed,
        auto_read_skills=skills,
        max_iterations=row["max_iterations"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def filter_destructive(tools: list[str]) -> list[str]:
    """Destructive 工具过滤器：即使用户 allow 了也要在调用点剔除.
    WP-F: also strips REPORT_WRITE_TOOLS (exact name) — report-write privilege is
    never a CCA grant."""
    from bowhead_core.resource_disable import team_agent_opened_tools

    opened = team_agent_opened_tools()
    return [t for t in tools if not any(t.startswith(p) for p in DESTRUCTIVE_TOOL_PREFIXES) and (t in opened or t not in REPORT_WRITE_TOOLS)]


class CustomSubagentStore:
    def __init__(self, db_path: str):
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    def _validate(
        self,
        *,
        name: str,
        display_name: str,
        description: str,
        system_prompt: str,
        max_iterations: int,
    ) -> None:
        if not _NAME_RE.match(name):
            raise ValueError(
                f"name must be lowercase alphanumeric + _/- (2-32 chars), got {name!r}",
            )
        if name in RESERVED_NAMES:
            raise ValueError(f"name {name!r} is reserved by built-in subagents")
        if not display_name.strip() or len(display_name) > 40:
            raise ValueError("display_name required (1-40 chars)")
        if not description.strip() or len(description) > 200:
            raise ValueError("description required (1-200 chars)")
        if len(system_prompt) > MAX_PROMPT_LEN:
            raise ValueError(f"system_prompt too long (max {MAX_PROMPT_LEN} chars)")
        overflow = system_prompt_token_overflow(system_prompt)
        if overflow is not None:
            raise ValueError(f"system_prompt too long ({overflow} tokens, max {MAX_PROMPT_TOKENS})")
        if not (1 <= max_iterations <= MAX_ITERATIONS_CAP):
            raise ValueError(f"max_iterations must be in [1, {MAX_ITERATIONS_CAP}]")

    async def create(
        self,
        *,
        user_id: str,
        name: str,
        display_name: str,
        description: str,
        system_prompt: str,
        allowed_tools: list[str] | None = None,
        auto_read_skills: list[str] | None = None,
        max_iterations: int = 10,
    ) -> CustomSubagent:
        self._validate(
            name=name,
            display_name=display_name,
            description=description,
            system_prompt=system_prompt,
            max_iterations=max_iterations,
        )
        allowed = filter_destructive(allowed_tools or [])
        skills = auto_read_skills or []
        agent = CustomSubagent(
            id=f"cs_{uuid4().hex[:12]}",
            user_id=user_id,
            name=name,
            display_name=display_name,
            description=description,
            system_prompt=system_prompt,
            allowed_tools=allowed,
            auto_read_skills=skills,
            max_iterations=max_iterations,
            created_at=_now(),
            updated_at=_now(),
        )
        async with aiosqlite.connect(self._db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO custom_subagents(id, user_id, name, display_name, "
                    "description, system_prompt, allowed_tools, auto_read_skills, "
                    "max_iterations, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    (
                        agent.id,
                        agent.user_id,
                        agent.name,
                        agent.display_name,
                        agent.description,
                        agent.system_prompt,
                        json.dumps(allowed),
                        json.dumps(skills),
                        agent.max_iterations,
                        agent.created_at,
                        agent.updated_at,
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError as e:
                raise ValueError(
                    f"subagent name {name!r} already exists for this user",
                ) from e
        return agent

    async def get(self, agent_id: str) -> CustomSubagent | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM custom_subagents WHERE id = ?",
                (agent_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_agent(row) if row else None

    async def list_for_user(
        self,
        user_id: str,
        status: str = "active",
    ) -> list[CustomSubagent]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM custom_subagents WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
                (user_id, status),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_agent(r) for r in rows]

    async def patch(
        self,
        agent_id: str,
        **changes: Any,
    ) -> CustomSubagent | None:
        current = await self.get(agent_id)
        if current is None:
            return None
        merged = {
            "name": current.name,
            "display_name": current.display_name,
            "description": current.description,
            "system_prompt": current.system_prompt,
            "max_iterations": current.max_iterations,
        }
        for k, v in changes.items():
            if v is not None and k in merged:
                merged[k] = v
        self._validate(**merged)  # type: ignore[arg-type]

        sets: list[str] = []
        vals: list[Any] = []
        for field_name in ("display_name", "description", "system_prompt", "max_iterations", "status"):
            v = changes.get(field_name)
            if v is not None:
                sets.append(f"{field_name} = ?")
                vals.append(v)
        if "allowed_tools" in changes and changes["allowed_tools"] is not None:
            sets.append("allowed_tools = ?")
            vals.append(json.dumps(filter_destructive(changes["allowed_tools"])))
        if "auto_read_skills" in changes and changes["auto_read_skills"] is not None:
            sets.append("auto_read_skills = ?")
            vals.append(json.dumps(changes["auto_read_skills"]))
        if not sets:
            return current
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(agent_id)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                f"UPDATE custom_subagents SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            await db.commit()
        return await self.get(agent_id)

    async def delete(self, agent_id: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM custom_subagents WHERE id = ?",
                (agent_id,),
            )
            await db.commit()
            return cur.rowcount > 0
