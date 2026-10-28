"""PlanStore — Planning Mode 计划状态机持久层 (M1).

见 docs/sprints/planning-mode/m1-prd-techdesign.md §5 数据模型.

设计要点(落实 tech-lead-review):
- 计划 = DB 状态机, 执行唯一真相源 (plans / plan_steps 两表).
- **租户隔离不靠 route 层**: get_plan / list 把 user_id+workspace_id 进 WHERE
  (review P1-K: 现有 store 普遍 id-only, 这里防 cross-workspace 泄漏).
- 抢锁/重启回收照 scheduled_store 的 SQLite-native 模式 (partial unique index +
  boot recover), **不用 Postgres 的 SELECT FOR UPDATE SKIP LOCKED** (substrate
  是 SQLite, review P0-1 亲验).
- 连接走 `_db.open_shared_ctx` (busy_timeout=5000 / WAL-friendly), 非裸 connect.

M1 范围: CRUD + step 状态回写 + level-stepped driver 支持 (resume_level) +
boot 孤儿回收. crash 幂等 / attempt / lease = M2 (本模块表结构未含, 见 PRD §5).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from ._db import open_shared_ctx
from .agent_role_candidates import ExactAgentRoleChoice


class PlanStatus(StrEnum):
    PROPOSED = "proposed"  # Card shown, awaiting edit/approval.
    AWAITING_AMEND = "awaiting_amend"  # M2: level-boundary amendment awaits reapproval (D5 deferred).
    RUNNING = "running"
    BLOCKED = "blocked"  # Blocked/failed steps await material/skip and per-step retry (nonterminal).
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"  # Dependencies unmet.
    READY = "ready"  # Dependencies done, dispatchable.
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # Unchecked by user / all upstream dead.
    BLOCKED = "blocked"  # Unmet precondition (no corpus / clarification), await user.
    # graph-mode batch 3: replaced by revision/replan, terminal with no outgoing transition (update_step_status
    # SQL guard); preserve output/token evidence (D3.5). Pointer in superseded_by column.
    SUPERSEDED = "superseded"


# Retired, undispatchable agents (canvas-writer removed in c4d50b03b). Their legacy plan steps
# are skipped at execution admission (plan_driver.run_plan); closing aggregation and gap notices exclude them
# too (lead_orchestrator), letting old plans complete without retired-agent-incomplete gaps,
# and without remerging their earlier summaries as another chapter.
RETIRED_PLAN_AGENTS: frozenset[str] = frozenset({"canvas-writer"})


def is_retired_plan_agent(agent_name: object) -> bool:
    return str(agent_name or "") in RETIRED_PLAN_AGENTS


class DeliverableKind(StrEnum):
    """D6: 终态交付物是 plan 属性, 不焊死 report.

    ⚠ 这里只放**交付形态**。文件格式 (docx/pdf/xlsx/pptx) **不进本枚举** ——
    格式是同一交付物的序列化, 已在 6 处枚举并由
    ``tests/test_export_format_registry_drift.py`` 钉住 (加进来就是第 7 处);
    格式属于 artifact 的属性 (docs/design/概念术语.md「artifact 是文件与交付物的
    统一真相源」)。一份 report 导成 docx 还是那份 report; report「导」成 deck
    则要重写 —— 形态换生产者, 格式只换序列化, 两个轴不同。

    delivery-surface sprint (2026-08-13): 新增 ANSWER = 交付物就是聊天里的答复,
    不产生文档。不复用 NONE (「什么都不交付」— 聊天答复是交付了的) 也不复用
    ACTIONS_SUMMARY (语义是行动项摘要且从未实现, 不给死值追加第二含义)。
    """

    REPORT = "report"
    ANSWER = "answer"  # Reply is deliverable (short chat answer, no document).
    DECK = "deck"
    ACTIONS_SUMMARY = "actions_summary"
    ARTIFACT = "artifact"
    NONE = "none"


class StepOrigin(StrEnum):
    INITIAL = "initial"  # Original-plan step.
    AMENDMENT = "amendment"  # M2: added at level-boundary amendment (D5 deferred).
    REVISION = "revision"  # Batch 3: local replan (revise_step) revision.
    REPLAN = "replan"  # Batch 3: full replan regenerates unfinished subgraph steps.


# Terminal plan statuses: sticky; update_plan_status rejects transitions out (CTO P2-10).
_TERMINAL_PLAN = (PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED)


SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    workspace_id        TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    title               TEXT NOT NULL,
    objective           TEXT NOT NULL DEFAULT '',
    deliverable_kind    TEXT NOT NULL DEFAULT 'report',
    status              TEXT NOT NULL DEFAULT 'proposed',
    spec_json           TEXT NOT NULL DEFAULT '',
    current_rev         INTEGER NOT NULL DEFAULT 1,
    replan_count        INTEGER NOT NULL DEFAULT 0,
    canvas_id           TEXT,
    execution_started_at TEXT,
    execution_turn_id   TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_owner
    ON plans(user_id, workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_plans_session
    ON plans(session_id);

CREATE TABLE IF NOT EXISTS plan_steps (
    id                  TEXT PRIMARY KEY,
    plan_id             TEXT NOT NULL,
    seq                 INTEGER NOT NULL,
    level               INTEGER NOT NULL DEFAULT 0,
    agent_name          TEXT NOT NULL,
    objective           TEXT NOT NULL DEFAULT '',
    deliverable         TEXT NOT NULL DEFAULT '',
    depends_on          TEXT NOT NULL DEFAULT '[]',
    needs_corpus        INTEGER NOT NULL DEFAULT 0,
    needs_web           INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending',
    origin              TEXT NOT NULL DEFAULT 'initial',
    included            INTEGER NOT NULL DEFAULT 1,
    turn_id             TEXT,
    output_section_ids  TEXT NOT NULL DEFAULT '[]',
    result_digest       TEXT,
    payload_json        TEXT,
    tokens_input        INTEGER NOT NULL DEFAULT 0,
    tokens_output       INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    error_kind          TEXT,
    success_criteria    TEXT NOT NULL DEFAULT '[]',
    verify_json         TEXT,
    superseded_by       TEXT,
    input_manifest_json TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES plans(id)
);
CREATE INDEX IF NOT EXISTS idx_plan_steps_plan
    ON plan_steps(plan_id, level, seq);
"""


# New columns (payload_json/tokens_*/verify_json) use schema DEFAULT, omitted from shared add/replace INSERT.
_STEP_INSERT_SQL = (
    "INSERT INTO plan_steps(id, plan_id, seq, level, agent_name, "
    "objective, deliverable, success_criteria, depends_on, needs_corpus, needs_web, status, origin, "
    "included, turn_id, output_section_ids, result_digest, error, error_kind, "
    "created_at, updated_at) VALUES "
    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', NULL, NULL, NULL, ?, ?)"
)

_STEP_INSERT_WITH_AGENT_CHOICE_SQL = (
    "INSERT INTO plan_steps(id, plan_id, seq, level, agent_name, "
    "objective, deliverable, success_criteria, depends_on, needs_corpus, needs_web, status, origin, "
    "included, turn_id, output_section_ids, result_digest, error, error_kind, agent_choice_json, "
    "created_at, updated_at) VALUES "
    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', NULL, NULL, NULL, ?, ?, ?)"
)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


@dataclass
class Plan:
    id: str
    session_id: str
    workspace_id: str
    user_id: str
    title: str
    objective: str = ""
    deliverable_kind: str = DeliverableKind.REPORT
    status: str = PlanStatus.PROPOSED
    spec_json: str = ""
    current_rev: int = 1
    replan_count: int = 0  # Batch 3: revise/replan commit count (soft guard).
    canvas_id: str | None = None
    execution_started_at: str | None = None
    execution_turn_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class PlanStep:
    id: str
    plan_id: str
    seq: int
    agent_name: str
    objective: str = ""
    deliverable: str = ""
    # graph-mode batch 1: acceptance criteria (1-3 checkable items, second pass) and verification results.
    # deliverable=form, criteria=content; verifier prompt gets both (tech-design §1.2).
    success_criteria: tuple[str, ...] = field(default_factory=tuple)
    verify_json: str | None = None  # VerifyOutcome JSON, None=unverified.
    level: int = 0
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    needs_corpus: bool = False
    # planning-mode-audit WP-7/review-P1: public external-information step (web search, compute+web).
    # Persistence is critical: preflight (WP-2) identifies BI by agent_name; without it, pure-web
    # compute steps are wrongly blocked awaiting space selection (WP-2×WP-7 interaction, review P1).
    needs_web: bool = False
    status: str = StepStatus.PENDING
    origin: str = StepOrigin.INITIAL
    # A (editable): soft include/exclude; unchecked=dimmed, recheckable, skipped by driver filter.
    included: bool = True
    turn_id: str | None = None
    output_section_ids: tuple[str, ...] = field(default_factory=tuple)
    result_digest: str | None = None
    payload_json: str | None = None  # Full payload (report/retry downstream need fidelity, not summary).
    tokens_input: int = 0
    tokens_output: int = 0
    error: str | None = None
    error_kind: str | None = None
    # graph-mode batch 3: revision pointer (revise=successor "ps_*"; full replan=generation sentinel
    # "replan:rev{N}"; non-ps_ prefix never followed as step ID) and actual upstream receipts.
    superseded_by: str | None = None
    input_manifest_json: str | None = None
    # Agent Marketplace M3-13E: exact planner-selected Agent/revision, not display name;
    # never re-resolve latest by agent_name after approval; Plan execution uses it for admission recheck.
    agent_choice: ExactAgentRoleChoice | None = None
    created_at: str = ""
    updated_at: str = ""


def _row_to_plan(row: aiosqlite.Row) -> Plan:
    return Plan(
        id=row["id"],
        session_id=row["session_id"],
        workspace_id=row["workspace_id"],
        user_id=row["user_id"],
        title=row["title"],
        objective=row["objective"],
        deliverable_kind=row["deliverable_kind"],
        status=row["status"],
        spec_json=row["spec_json"],
        current_rev=row["current_rev"],
        replan_count=row["replan_count"],
        canvas_id=row["canvas_id"],
        execution_started_at=row["execution_started_at"],
        execution_turn_id=row["execution_turn_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _json_list(raw: str | None) -> tuple[str, ...]:
    try:
        val = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return ()
    return tuple(str(x) for x in val) if isinstance(val, list) else ()


def _row_to_step(row: aiosqlite.Row) -> PlanStep:
    try:
        choice_raw = row["agent_choice_json"]
    except IndexError:  # pre-0056 legacy schema
        choice_raw = None
    return PlanStep(
        id=row["id"],
        plan_id=row["plan_id"],
        seq=row["seq"],
        level=row["level"],
        agent_name=row["agent_name"],
        objective=row["objective"],
        deliverable=row["deliverable"],
        success_criteria=_json_list(row["success_criteria"]),
        verify_json=row["verify_json"],
        depends_on=_json_list(row["depends_on"]),
        needs_corpus=bool(row["needs_corpus"]),
        needs_web=bool(row["needs_web"]),
        status=row["status"],
        origin=row["origin"],
        included=bool(row["included"]),
        turn_id=row["turn_id"],
        output_section_ids=_json_list(row["output_section_ids"]),
        result_digest=row["result_digest"],
        payload_json=row["payload_json"],
        tokens_input=row["tokens_input"],
        tokens_output=row["tokens_output"],
        error=row["error"],
        error_kind=row["error_kind"],
        superseded_by=row["superseded_by"],
        input_manifest_json=row["input_manifest_json"],
        agent_choice=(ExactAgentRoleChoice.from_json(str(choice_raw)) if choice_raw is not None else None),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def new_step(
    *,
    plan_id: str,
    seq: int,
    agent_name: str,
    objective: str = "",
    deliverable: str = "",
    success_criteria: tuple[str, ...] = (),
    level: int = 0,
    depends_on: tuple[str, ...] = (),
    needs_corpus: bool = False,
    needs_web: bool = False,
    origin: str = StepOrigin.INITIAL,
    included: bool = True,
    agent_choice: ExactAgentRoleChoice | None = None,
) -> PlanStep:
    """构造一个待插入的 PlanStep (id 自动生成, 时间戳 insert 时填)."""
    return PlanStep(
        id=_new_id("ps"),
        plan_id=plan_id,
        seq=seq,
        level=level,
        agent_name=agent_name,
        objective=objective,
        deliverable=deliverable,
        success_criteria=tuple(success_criteria),
        depends_on=tuple(depends_on),
        needs_corpus=needs_corpus,
        needs_web=needs_web,
        origin=origin,
        included=included,
        agent_choice=agent_choice,
    )


class PlanStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self._db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
            # Add-column migration: executescript `CREATE TABLE IF NOT EXISTS` never changes existing tables,
            # so early M1 plan_steps DBs (dev / future prod) need A included column added, idempotently.
            async with db.execute("PRAGMA table_info(plan_steps)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            if "included" not in cols:
                await db.execute("ALTER TABLE plan_steps ADD COLUMN included INTEGER NOT NULL DEFAULT 1")
                await db.commit()
            # planning-mode-audit WP-7 review-P1: persist needs_web (preflight distinguishes pure-web
            # compute from BI compute); follow included idempotent add-column precedent.
            if "needs_web" not in cols:
                await db.execute("ALTER TABLE plan_steps ADD COLUMN needs_web INTEGER NOT NULL DEFAULT 0")
                await db.commit()
            # graph-mode batch 1: criteria + verification results. One of three guarded paths (SCHEMA/here/0011
            # revision all idempotent); maintain dual path while store tests bypass alembic (review B1).
            if "success_criteria" not in cols:
                await db.execute("ALTER TABLE plan_steps ADD COLUMN success_criteria TEXT NOT NULL DEFAULT '[]'")
                await db.commit()
            if "verify_json" not in cols:
                await db.execute("ALTER TABLE plan_steps ADD COLUMN verify_json TEXT")
                await db.commit()
            # graph-mode batch 3: revision pointer + upstream receipts (same three-way guards as 0016 revision).
            if "superseded_by" not in cols:
                await db.execute("ALTER TABLE plan_steps ADD COLUMN superseded_by TEXT")
                await db.commit()
            if "input_manifest_json" not in cols:
                await db.execute("ALTER TABLE plan_steps ADD COLUMN input_manifest_json TEXT")
                await db.commit()
            async with db.execute("PRAGMA table_info(plans)") as cur:
                plan_cols = {row[1] for row in await cur.fetchall()}
            if "execution_turn_id" not in plan_cols:
                await db.execute("ALTER TABLE plans ADD COLUMN execution_turn_id TEXT")
                await db.commit()
            if "replan_count" not in plan_cols:
                await db.execute("ALTER TABLE plans ADD COLUMN replan_count INTEGER NOT NULL DEFAULT 0")
                await db.commit()
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_execution_turn ON plans(execution_turn_id) WHERE execution_turn_id IS NOT NULL"
            )
            await db.commit()

    # ─── plans ─────────────────────────────────────────────
    async def create_plan(
        self,
        *,
        session_id: str,
        workspace_id: str,
        user_id: str,
        title: str,
        objective: str = "",
        deliverable_kind: str = DeliverableKind.REPORT,
        spec_json: str = "",
        canvas_id: str | None = None,
        initial_steps: Sequence[PlanStep] | None = None,
    ) -> Plan:
        """Publish a prepared proposal and its steps in one transaction.

        Initial steps have no Plan owner before publication. Keep their IDs,
        dependencies and exact Agent choices; assign the new owner to copies.
        Existing metadata-only callers may omit ``initial_steps``.
        """
        if deliverable_kind not in set(DeliverableKind):
            raise ValueError(f"invalid deliverable_kind: {deliverable_kind}")
        if initial_steps is not None and (not initial_steps or any(step.plan_id for step in initial_steps)):
            raise ValueError("initial steps must be nonempty and unbound")
        plan = Plan(
            id=_new_id("plan"),
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user_id,
            title=title,
            objective=objective,
            deliverable_kind=deliverable_kind,
            spec_json=spec_json,
            canvas_id=canvas_id,
            created_at=_now(),
            updated_at=_now(),
        )
        steps = [replace(step, plan_id=plan.id) for step in initial_steps or ()]
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "INSERT INTO plans(id, session_id, workspace_id, user_id, title, "
                "objective, deliverable_kind, status, spec_json, current_rev, "
                "canvas_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    plan.id,
                    plan.session_id,
                    plan.workspace_id,
                    plan.user_id,
                    plan.title,
                    plan.objective,
                    plan.deliverable_kind,
                    plan.status,
                    plan.spec_json,
                    plan.canvas_id,
                    plan.created_at,
                    plan.updated_at,
                ),
            )
            await self._insert_steps(db, steps, plan.created_at)
            from bowhead_core.chat_reference_plan import read_plan_association, read_requirements

            requirements = read_requirements(spec_json)
            if requirements is not None and await read_plan_association(db, turn_id=requirements.source_turn_id) is None:
                raise ValueError("reference_plan_source_changed")
            await db.commit()
        return plan

    async def get_plan(
        self,
        plan_id: str,
        *,
        user_id: str,
        workspace_id: str,
    ) -> Plan | None:
        """租户进 WHERE — 不同 workspace/user 拿不到别人的 plan (review P1-K)."""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM plans WHERE id = ? AND user_id = ? AND workspace_id = ?",
                (plan_id, user_id, workspace_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_plan(row) if row else None

    async def get_plan_with_steps(self, plan_id: str, *, user_id: str, workspace_id: str) -> tuple[Plan, list[PlanStep]] | None:
        """Read the displayed revision and its steps from one authorized snapshot."""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                async with db.execute(
                    "SELECT * FROM plans WHERE id=? AND user_id=? AND workspace_id=?",
                    (plan_id, user_id, workspace_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    return None
                plan = _row_to_plan(row)
                async with db.execute("SELECT * FROM plan_steps WHERE plan_id=? ORDER BY level, seq", (plan_id,)) as cursor:
                    steps = [_row_to_step(step) for step in await cursor.fetchall()]
                return plan, steps
            finally:
                await db.rollback()

    async def list_plans_for_user(
        self,
        *,
        user_id: str,
        workspace_id: str,
    ) -> list[Plan]:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM plans WHERE user_id = ? AND workspace_id = ? ORDER BY created_at DESC",
                (user_id, workspace_id),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_plan(r) for r in rows]

    async def update_plan_status(self, plan_id: str, status: str) -> bool:
        """终态 sticky (CTO P2-10): completed/failed/cancelled 不可再转出 —
        durable-cancel (D-D6) 依赖 cancelled 不可逆. 返回 False 若被拒/未命中."""
        if status not in set(PlanStatus):
            raise ValueError(f"invalid plan status: {status}")
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE id = ? AND status NOT IN (?, ?, ?)",
                (status, _now(), plan_id, *_TERMINAL_PLAN),
            )
            await db.commit()
            return cur.rowcount > 0

    async def demote_completed_to_blocked(self, plan_id: str) -> bool:
        """planning-mode-audit WP-5 review-P2: completed-but-report-empty 降级。

        终态 sticky(P2-10)的**唯一定向豁免**: `completed → blocked` —— 全步"成功"
        但报告零内容时, writer 步已被标 FAILED(report_empty), plan 必须回到可
        重试态(completed 卡没有重试按钮, 文案「可在计划卡重试」才不撒谎)。
        cancelled/failed 照旧不可转出(durable-cancel D-D6 依赖不变)。
        """
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (PlanStatus.BLOCKED, _now(), plan_id, PlanStatus.COMPLETED),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_execution_started_at(self, plan_id: str) -> None:
        """每次 run_plan(首跑或 re-approve 续跑)重置, 作 caps wallclock 锚。

        2026-07 语义修正: cap 的语义是"本执行 turn 的预算"(driver 在 loop 硬超时
        之下自停, CTO P2-6)。原 `IS NULL` 守卫(只设一次)让隔天补料重试的 plan
        因老锚点瞬间 wallclock 越界 → 终态 FAILED, 重试永不可能成功。
        """
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE plans SET execution_started_at = ?, updated_at = ? WHERE id = ?",
                (_now(), _now(), plan_id),
            )
            await db.commit()

    async def try_claim_running(self, plan_id: str, turn_id: str, *, expected_rev: int | None = None) -> bool:
        """原子认领执行权: PROPOSED/BLOCKED → RUNNING (CTO P1-5 防双跑).

        返回 True = 认领成功(本调用者去执行);False = 已 RUNNING(别人在跑)或
        终态(completed/failed/cancelled)→ 拒绝, 不重入。``turn_id`` 同时写入，
        既是跨 Gateway 的 claim token，也是 Stop / recovery 的 durable identity；
        不允许先写 RUNNING、稍后再 bind 的无主窗口。审批入口传入读取时的
        expected_rev，避免准备执行期间保存的新步骤被旧请求认领。
        """
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE plans SET status=?,execution_turn_id=?,updated_at=? WHERE id=? AND status IN (?,?) "
                "AND execution_turn_id IS NULL AND (? IS NULL OR current_rev=?)",
                (
                    PlanStatus.RUNNING,
                    turn_id,
                    _now(),
                    plan_id,
                    PlanStatus.PROPOSED,
                    PlanStatus.BLOCKED,
                    expected_rev,
                    expected_rev,
                ),
            )
            await db.commit()
            return cur.rowcount > 0

    async def bind_execution_turn(self, plan_id: str, turn_id: str) -> bool:
        """Renew/revalidate one exact execution identity before worker spawn."""
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE plans SET updated_at=? WHERE id=? AND status=? AND execution_turn_id=?",
                (_now(), plan_id, PlanStatus.RUNNING, turn_id),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def get_execution_claim_status(
        self,
        plan_id: str,
        turn_id: str,
    ) -> str | None:
        """Return status only while ``turn_id`` still owns this Plan claim.

        Dev execution heartbeat uses this after a RUNNING-only renewal misses.
        An exact token on a post-driver status means the same worker is merely
        finishing report / Turn terminal output; a cleared or replaced token
        means orphan recovery or a newer executor has fenced this worker out.
        """
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT status FROM plans WHERE id=? AND execution_turn_id=?",
                (plan_id, turn_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return str(row[0]) if row is not None else None

    async def heartbeat_execution_tail(self, plan_id: str, turn_id: str) -> bool:
        """Renew an exact-token dev tail after PlanDriver leaves RUNNING.

        The shared chat dispatch owns the worker lease.  This additional Plan
        exact-token fence keeps the report/terminal tail from overlapping an
        orphan retry after the 30-second freshness window. Cancellation is
        intentionally excluded and exact-token CAS fences a replaced owner.
        """
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE plans SET updated_at=? WHERE id=? AND execution_turn_id=? AND status IN (?,?,?,?)",
                (
                    _now(),
                    plan_id,
                    turn_id,
                    PlanStatus.AWAITING_AMEND,
                    PlanStatus.BLOCKED,
                    PlanStatus.COMPLETED,
                    PlanStatus.FAILED,
                ),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def release_execution_turn(self, plan_id: str, turn_id: str) -> bool:
        """Release the exact worker token after its complete Turn tail.

        ``BLOCKED`` becomes retryable only after this CAS.  RUNNING is excluded:
        a crashed worker must stay fenced until orphan recovery reconciles the
        Plan, rather than opening a second execution against unfinished work.
        """
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE plans SET execution_turn_id=NULL,updated_at=? WHERE id=? AND execution_turn_id=? AND status!=?",
                (_now(), plan_id, turn_id, PlanStatus.RUNNING),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def release_execution_turn_for_turn(self, turn_id: str) -> bool:
        """Release a dynamically authored Plan after the exact Turn tail.

        Auto planning learns its Plan id only after the Turn is admitted, so
        the immutable start request cannot carry it. ``execution_turn_id`` is
        unique and remains the durable ownership token for that path.
        """
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE plans SET execution_turn_id=NULL,updated_at=? WHERE execution_turn_id=? AND status!=?",
                (_now(), turn_id, PlanStatus.RUNNING),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def release_execution_claim(
        self,
        plan_id: str,
        turn_id: str,
        *,
        next_status: str = PlanStatus.BLOCKED,
    ) -> bool:
        """Release a pre-spawn Plan claim without stealing a newer executor.

        The CAS accepts only the Turn identity written by ``try_claim_running``,
        clears it, and makes the Plan retryable.  A delayed old request can
        never release a newer executor's claim.
        """
        if next_status not in (PlanStatus.PROPOSED, PlanStatus.BLOCKED):
            raise ValueError("invalid_plan_release_status")
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE plans SET status=?,execution_turn_id=NULL,updated_at=? WHERE id=? AND status=? AND execution_turn_id=?",
                (
                    next_status,
                    _now(),
                    plan_id,
                    PlanStatus.RUNNING,
                    turn_id,
                ),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def cancel_execution_for_turn(self, turn_id: str) -> bool:
        """Durably cancel a running Plan from any Gateway process."""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                from .session_store import (
                    _cancel_running_plan_for_turn_in_tx,
                )

                cancelled = await _cancel_running_plan_for_turn_in_tx(
                    db,
                    turn_id=turn_id,
                    updated_at=_now(),
                )
                await db.commit()
                return cancelled
            except BaseException:
                with contextlib.suppress(Exception):
                    await db.execute("ROLLBACK")
                raise

    # ─── plan_steps ────────────────────────────────────────
    @staticmethod
    def _step_rows(
        steps: list[PlanStep],
        ts: str,
        *,
        include_agent_choice: bool = False,
    ) -> list[tuple[Any, ...]]:
        return [
            (
                s.id,
                s.plan_id,
                s.seq,
                s.level,
                s.agent_name,
                s.objective,
                s.deliverable,
                json.dumps(list(s.success_criteria)),
                json.dumps(list(s.depends_on)),
                1 if s.needs_corpus else 0,
                1 if s.needs_web else 0,
                s.status,
                s.origin,
                1 if s.included else 0,
                *((s.agent_choice.canonical_json() if s.agent_choice is not None else None,) if include_agent_choice else ()),
                ts,
                ts,
            )
            for s in steps
        ]

    @classmethod
    async def _insert_steps(
        cls,
        db: aiosqlite.Connection,
        steps: list[PlanStep],
        ts: str,
    ) -> None:
        include_choice = any(step.agent_choice is not None for step in steps)
        if include_choice:
            async with db.execute("PRAGMA table_info(plan_steps)") as cursor:
                columns = {str(row[1]) for row in await cursor.fetchall()}
            if "agent_choice_json" not in columns:
                raise RuntimeError("plan_agent_choice_schema_unavailable")
            await db.executemany(
                _STEP_INSERT_WITH_AGENT_CHOICE_SQL,
                cls._step_rows(steps, ts, include_agent_choice=True),
            )
            return
        await db.executemany(_STEP_INSERT_SQL, cls._step_rows(steps, ts))

    async def add_steps(self, steps: list[PlanStep]) -> None:
        """批量插入步 (初始 reify). 调用方用 new_step() 构造."""
        if not steps:
            return
        ts = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            from bowhead_core.chat_reference_plan import read_requirements

            for plan_id in {step.plan_id for step in steps}:
                async with db.execute("SELECT spec_json FROM plans WHERE id=?", (plan_id,)) as cursor:
                    row = await cursor.fetchone()
                if row is not None and read_requirements(row[0]) is not None:
                    raise ValueError("reference_plan_requires_versioned_graph_edit")
            await self._insert_steps(db, steps, ts)
            await db.commit()

    async def replace_steps(self, plan_id: str, steps: list[PlanStep], *, expected_rev: int) -> bool:
        """仅替换仍可编辑的原版本；步骤和版本同事务提交，与执行认领互斥。"""
        if not steps or any(step.plan_id != plan_id for step in steps):
            raise ValueError("steps must belong to plan_id and must not be empty")
        ts = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE plans SET current_rev=current_rev+1, updated_at=? "
                    "WHERE id=? AND status=? AND execution_turn_id IS NULL AND current_rev=?",
                    (ts, plan_id, PlanStatus.PROPOSED, expected_rev),
                )
                if cur.rowcount == 0:
                    await db.execute("ROLLBACK")
                    return False
                await db.execute("DELETE FROM plan_steps WHERE plan_id = ?", (plan_id,))
                await self._insert_steps(db, steps, ts)
                from bowhead_core.chat_reference_plan import refresh_graph_in_connection

                await refresh_graph_in_connection(db, plan_id)
                await db.commit()
                return True
            except ValueError:
                await db.rollback()
                return False
            except BaseException:
                with contextlib.suppress(Exception):
                    await db.execute("ROLLBACK")
                raise

    # NB: D5 amendment loop (append_amendment_steps) deferred to M2; see m1-execution-tech-design v2.

    # ─── Batch 3 replan version protocol ───────────────────────────
    # Triple CAS (batch3-replan-design D-C, review R1-F1/R2-F2/R2-F3):
    #   1) plans row: status='blocked' ∧ execution_turn_id IS NULL (reject worker tail window,
    #      avoid try_claim_running race) ∧ current_rev=expected_rev (OCC, mutually exclusive endpoints);
    #   2) old step row: status IN (failed, blocked) (second double-click confirm rejected here);
    #   3) new insert + downstream reset in same transaction. Any CAS rowcount==0 → full ROLLBACK, False.

    async def skip_blocked_step(self, plan_id: str, step_id: str, *, expected_rev: int) -> bool:
        """Skip and advance the reviewed graph in the same idle-Plan transaction."""
        ts = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                changed = await db.execute(
                    "UPDATE plans SET current_rev=current_rev+1,updated_at=? "
                    "WHERE id=? AND status='blocked' AND execution_turn_id IS NULL AND current_rev=?",
                    (ts, plan_id, expected_rev),
                )
                if changed.rowcount != 1:
                    await db.rollback()
                    return False
                skipped = await db.execute(
                    "UPDATE plan_steps SET status='skipped',updated_at=? WHERE id=? AND plan_id=? AND status IN ('failed','blocked')",
                    (ts, step_id, plan_id),
                )
                if skipped.rowcount != 1:
                    await db.rollback()
                    return False
                from bowhead_core.chat_reference_plan import refresh_graph_in_connection

                await refresh_graph_in_connection(db, plan_id)
                await db.commit()
                return True
            except ValueError:
                await db.rollback()
                return False
            except BaseException:
                await db.rollback()
                raise

    async def supersede_step(
        self,
        *,
        plan_id: str,
        old_step_id: str,
        new_step: PlanStep,
        downstream_reset_ids: Sequence[str],
        expected_rev: int,
    ) -> bool:
        """局部 replan commit: 旧行标 SUPERSEDED(指针=新行 id)+新行插入+下游复位。"""
        if new_step.plan_id != plan_id:
            raise ValueError("new_step.plan_id 与 plan_id 不一致")
        ts = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE plans SET replan_count = replan_count + 1, current_rev = current_rev + 1, "
                    "updated_at = ? WHERE id = ? AND status = ? AND execution_turn_id IS NULL AND current_rev = ?",
                    (ts, plan_id, PlanStatus.BLOCKED, expected_rev),
                )
                if cur.rowcount == 0:
                    await db.execute("ROLLBACK")
                    return False
                cur = await db.execute(
                    "UPDATE plan_steps SET status = ?, superseded_by = ?, updated_at = ? WHERE id = ? AND plan_id = ? AND status IN (?, ?)",
                    (StepStatus.SUPERSEDED, new_step.id, ts, old_step_id, plan_id, StepStatus.FAILED, StepStatus.BLOCKED),
                )
                if cur.rowcount == 0:
                    await db.execute("ROLLBACK")
                    return False
                await self._insert_steps(db, [new_step], ts)
                if downstream_reset_ids:
                    ph = ", ".join("?" for _ in downstream_reset_ids)
                    # Defensive predicate: reset only BLOCKED rows still upstream_failed.
                    await db.execute(
                        f"UPDATE plan_steps SET status = ?, error = NULL, error_kind = NULL, updated_at = ? "  # noqa: S608 — placeholders only
                        f"WHERE plan_id = ? AND id IN ({ph}) AND status = ? AND error_kind = 'upstream_failed'",
                        (StepStatus.PENDING, ts, plan_id, *downstream_reset_ids, StepStatus.BLOCKED),
                    )
                from bowhead_core.chat_reference_plan import refresh_graph_in_connection

                await refresh_graph_in_connection(db, plan_id)
                await db.commit()
                return True
            except ValueError:
                await db.rollback()
                return False
            except BaseException:
                # ROLLBACK may itself raise (e.g. COMMIT failed and transaction inactive); never replace real cause
                # (implementation review P2-3); implicit rollback on finally closing connection protects data.
                with contextlib.suppress(Exception):
                    await db.execute("ROLLBACK")
                raise

    async def replan_steps(
        self,
        *,
        plan_id: str,
        superseded_ids: Sequence[str],
        new_steps: list[PlanStep],
        level_updates: dict[str, int],
        expected_rev: int,
    ) -> bool:
        """全量 replan commit: 旧非 done 行标 SUPERSEDED(世代哨兵 replan:rev{N},
        非 ps_ 前缀不被别名链跟进)+新子图插入+活行 level 回写。done 行不动(D3.5)。"""
        ts = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE plans SET replan_count = replan_count + 1, current_rev = current_rev + 1, "
                    "updated_at = ? WHERE id = ? AND status = ? AND execution_turn_id IS NULL AND current_rev = ?",
                    (ts, plan_id, PlanStatus.BLOCKED, expected_rev),
                )
                if cur.rowcount == 0:
                    await db.execute("ROLLBACK")
                    return False
                sentinel = f"replan:rev{expected_rev + 1}"
                for sid in superseded_ids:
                    # Also reject skipped: skipped steps cannot be replaced again by replan request;
                    # allowing skip race between confirm read and transaction converts user-intent row to
                    # generation sentinel → manifest bucket excluded becomes degraded (R2-F9 contradiction).
                    cur = await db.execute(
                        "UPDATE plan_steps SET status = ?, superseded_by = ?, updated_at = ? WHERE id = ? AND plan_id = ? AND status NOT IN (?, ?, ?)",
                        (StepStatus.SUPERSEDED, sentinel, ts, sid, plan_id, StepStatus.DONE, StepStatus.SKIPPED, StepStatus.SUPERSEDED),
                    )
                    if cur.rowcount == 0:
                        await db.execute("ROLLBACK")
                        return False
                if new_steps:
                    await self._insert_steps(db, new_steps, ts)
                for sid, lvl in level_updates.items():
                    await db.execute(
                        "UPDATE plan_steps SET level = ?, updated_at = ? WHERE id = ? AND plan_id = ? AND status != ?",
                        (lvl, ts, sid, plan_id, StepStatus.SUPERSEDED),
                    )
                from bowhead_core.chat_reference_plan import refresh_graph_in_connection

                await refresh_graph_in_connection(db, plan_id)
                await db.commit()
                return True
            except ValueError:
                await db.rollback()
                return False
            except BaseException:
                # ROLLBACK may itself raise (e.g. COMMIT failed and transaction inactive); never replace real cause
                # (implementation review P2-3); implicit rollback on finally closing connection protects data.
                with contextlib.suppress(Exception):
                    await db.execute("ROLLBACK")
                raise

    async def list_steps(self, plan_id: str) -> list[PlanStep]:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY level, seq",
                (plan_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_step(r) for r in rows]

    async def update_step_status(
        self,
        step_id: str,
        *,
        status: str,
        turn_id: str | None = None,
        output_section_ids: tuple[str, ...] | None = None,
        result_digest: str | None = None,
        payload_json: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        error: str | None = None,
        error_kind: str | None = None,
        verify_json: str | None = None,
        input_manifest_json: str | None = None,
        clear_error: bool = False,
        clear_verify: bool = False,
        clear_input_manifest: bool = False,
    ) -> bool:
        if status not in set(StepStatus):
            raise ValueError(f"invalid step status: {status}")
        sets = ["status = ?"]
        vals: list[Any] = [status]
        if verify_json is not None:
            sets.append("verify_json = ?")
            vals.append(verify_json)
        if input_manifest_json is not None:
            sets.append("input_manifest_json = ?")
            vals.append(input_manifest_json)
        # Implementation review P2-2: redispatch same step with flag off clears old manifest, or verifier
        # judges new output using previous receipts (stale is worse than absent).
        if clear_input_manifest:
            if input_manifest_json is not None:
                raise ValueError("clear_input_manifest 与 input_manifest_json 互斥")
            sets.append("input_manifest_json = NULL")
        # graph-mode batch 1 review B4: `is not None` cannot clear old errors; after criteria_unmet
        # FAILED→retry DONE, lingering error/error_kind reaches frontend unchanged via _step_view.
        # DONE writeback explicitly sets NULL (recover_orphans raw SQL previously hit same issue).
        if clear_error:
            if error is not None or error_kind is not None:
                raise ValueError("clear_error 与 error/error_kind 互斥")
            sets.append("error = NULL")
            sets.append("error_kind = NULL")
        # Implementation review B4-A: DONE without this-turn verification clears stale verify_json,
        # preventing old fail verdict accusing new output (through _step_view/caveats).
        if clear_verify:
            if verify_json is not None:
                raise ValueError("clear_verify 与 verify_json 互斥")
            sets.append("verify_json = NULL")
        if turn_id is not None:
            sets.append("turn_id = ?")
            vals.append(turn_id)
        if output_section_ids is not None:
            sets.append("output_section_ids = ?")
            vals.append(json.dumps(list(output_section_ids)))
        if result_digest is not None:
            sets.append("result_digest = ?")
            vals.append(result_digest)
        if payload_json is not None:
            sets.append("payload_json = ?")
            vals.append(payload_json)
        if tokens_input is not None:
            sets.append("tokens_input = ?")
            vals.append(tokens_input)
        if tokens_output is not None:
            sets.append("tokens_output = ?")
            vals.append(tokens_output)
        if error is not None:
            sets.append("error = ?")
            vals.append(error)
        if error_kind is not None:
            sets.append("error_kind = ?")
            vals.append(error_kind)
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(step_id)
        async with open_shared_ctx(self._db_path) as db:
            # Batch 3 review R1-F5: SUPERSEDED is sticky step terminal (symmetrical with plans
            # _TERMINAL_PLAN); SQL guard prevents concurrent approve writeback
            # /preflight/render sentinel reviving dead rows. Superseding itself uses supersede_step CAS.
            cur = await db.execute(
                f"UPDATE plan_steps SET {', '.join(sets)} WHERE id = ? AND status != 'superseded'",  # noqa: S608 — fixed cols
                vals,
            )
            await db.commit()
            return cur.rowcount > 0

    # ─── Driver support ────────────────────────────────────────────
    async def resume_level(self, plan_id: str) -> int | None:
        """最低的、仍有非终态 (非 done/skipped) 步的 level —— driver 的续跑起点.

        全部终态 → None (计划可进汇总/completed). 首跑返 0; **per-step 重试**
        (用户补料/跳过 blocked 步后) 返回该 blocked level, done 步跳过、其
        payload 从 payload_json 取 (m1-execution-tech-design v2 §3.2/§3.4).
        """
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT MIN(level) AS lvl FROM plan_steps WHERE plan_id = ? AND status NOT IN (?, ?, ?)",
                (plan_id, StepStatus.DONE, StepStatus.SKIPPED, StepStatus.SUPERSEDED),
            ) as cur:
                row = await cur.fetchone()
        return int(row["lvl"]) if row and row["lvl"] is not None else None

    async def recover_orphans(self) -> tuple[int, int]:
        """Reconcile stale Plan claims whose dispatch lease is not live.

        reconcile-don't-re-run (tech-lead-review P0-D):
        - RUNNING **plan** → BLOCKED (可续 —— BLOCKED 非终态, try_claim 收它;
          原先标 FAILED = 终态 sticky → 整盘不可恢复, 崩溃即丢已完成工作).
        - RUNNING **step** → PENDING (在途结果未 checkpoint, 须重跑; error 清空).
          DONE 步有 payload → 保留, resume_level 跳过不重跑 = **不重复扣费 / 不重复
          写 done 步 canvas**(P0-D 主要双写由"只重跑未完成"根除).

        崩溃后用户 re-approve(收 BLOCKED)→ run_plan 从 resume_level 续, 只重跑未
        完成步. 返回 (plans, steps) 回收数. (auto-resume 留待未来 PlanRunner.)

        A rolling deploy starts a new Gateway while the old instance can still
        own healthy workers. ``execution_turn_id`` plus the writer's durable
        truth is therefore the authority: legacy uses a live claimed/spawned
        dispatch lease; Terminal owns every current generation not proven to
        be an old, well-formed closed v1 generation. This includes the short
        receipt-to-worker-retire tail and unknown future protocol shapes.

        A worker deliberately keeps its token after PlanDriver changes RUNNING
        to BLOCKED/terminal, until report/terminal/steer tail effects finish.
        If the process crashes or exact-token release keeps failing, that token
        must also be reclaimed once it is stale and has no live dispatch lease;
        otherwise BLOCKED can never be approved again.
        """
        now = _now()
        # A fresh claim is written before the submission/dispatch transaction.
        # Give that request one dispatch lease interval to publish its durable
        # dispatch row.  ``execution_turn_id`` remains the CAS token, so if the
        # request outlives this grace and a retry wins, the stale request cannot
        # bind or release the newer claim.
        fresh_claim_cutoff = (datetime.now(tz=UTC) - timedelta(seconds=30)).isoformat()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turn_spawn_dispatch'",
                ) as table_cursor:
                    dispatch_exists = await table_cursor.fetchone() is not None
                from .session_store import _terminal_protocol_schema_mode

                terminal_discriminator_exists = await _terminal_protocol_schema_mode(db) == "migrated"

                async def _owner_state(
                    turn_id: str,
                ) -> tuple[bool, str | None, bool]:
                    if dispatch_exists:
                        async with db.execute(
                            """
                            SELECT 1 FROM chat_turn_spawn_dispatch
                            WHERE turn_id=? AND state IN ('claimed','spawned')
                              AND lease_expires_at>?
                            """,
                            (turn_id, now),
                        ) as cursor:
                            if await cursor.fetchone() is not None:
                                return True, None, False
                    if not terminal_discriminator_exists:
                        return False, None, False
                    async with db.execute(
                        "SELECT terminal_protocol_version FROM turns WHERE id=?",
                        (turn_id,),
                    ) as cursor:
                        terminal_turn = await cursor.fetchone()
                    if terminal_turn is None or terminal_turn["terminal_protocol_version"] is None:
                        return False, None, False
                    # A persisted Terminal discriminator is enough to enter
                    # fail-closed mode. Only the existing full checkpoint,
                    # receipt/candidate/session-mutation or fenced-delete
                    # verifier may prove that this owner is safely releasable.
                    # Malformed/partial persisted truth aborts the maintenance
                    # transaction; it can never be reinterpreted as legacy.
                    from .terminal_protocol_store import (
                        read_canonical_closed_generation_outcome,
                    )

                    outcome = await read_canonical_closed_generation_outcome(
                        db,
                        turn_id=turn_id,
                        closed_before=datetime.fromisoformat(
                            fresh_claim_cutoff,
                        ),
                    )
                    if outcome is None:
                        return True, None, False
                    terminal_outcome, explicit_stop = outcome
                    return (
                        False,
                        terminal_outcome.value if hasattr(terminal_outcome, "value") else terminal_outcome,
                        explicit_stop,
                    )

                async with db.execute(
                    """
                    SELECT id,status,execution_turn_id FROM plans
                    WHERE updated_at<=? AND (
                      status=? OR (
                        status IN (?,?,?,?,?)
                        AND execution_turn_id IS NOT NULL
                      )
                    )
                    """,
                    (
                        fresh_claim_cutoff,
                        PlanStatus.RUNNING,
                        PlanStatus.AWAITING_AMEND,
                        PlanStatus.BLOCKED,
                        PlanStatus.COMPLETED,
                        PlanStatus.FAILED,
                        PlanStatus.CANCELLED,
                    ),
                ) as cursor:
                    candidates = await cursor.fetchall()
                plan_ids: list[str] = []
                cancelled_plan_ids: list[str] = []
                stale_tail_claims: list[tuple[str, str]] = []
                for candidate in candidates:
                    execution_turn_id = candidate["execution_turn_id"]
                    terminal_outcome: str | None = None
                    terminal_explicit_stop = False
                    if execution_turn_id is not None:
                        (
                            owner_live,
                            terminal_outcome,
                            terminal_explicit_stop,
                        ) = await _owner_state(
                            str(execution_turn_id),
                        )
                        if owner_live:
                            continue
                    if candidate["status"] == PlanStatus.RUNNING:
                        if terminal_outcome == "cancelled" and terminal_explicit_stop:
                            cancelled_plan_ids.append(str(candidate["id"]))
                        else:
                            plan_ids.append(str(candidate["id"]))
                    elif execution_turn_id is not None:
                        stale_tail_claims.append(
                            (str(candidate["id"]), str(execution_turn_id)),
                        )
                if not plan_ids and not cancelled_plan_ids and not stale_tail_claims:
                    await db.execute("ROLLBACK")
                    return (0, 0)
                recovered_steps = 0
                recovered_running = 0
                recovered_cancelled = 0
                if plan_ids:
                    placeholders = ",".join("?" for _ in plan_ids)
                    c2 = await db.execute(
                        f"UPDATE plan_steps SET status=?,error=NULL,error_kind=NULL,updated_at=? WHERE status=? AND plan_id IN ({placeholders})",  # noqa: S608 -- placeholders only
                        (StepStatus.PENDING, now, StepStatus.RUNNING, *plan_ids),
                    )
                    c1 = await db.execute(
                        f"UPDATE plans SET status=?,execution_turn_id=NULL,updated_at=? WHERE status=? AND id IN ({placeholders})",  # noqa: S608 -- placeholders only
                        (PlanStatus.BLOCKED, now, PlanStatus.RUNNING, *plan_ids),
                    )
                    recovered_steps = c2.rowcount
                    recovered_running = c1.rowcount
                if cancelled_plan_ids:
                    placeholders = ",".join("?" for _ in cancelled_plan_ids)
                    cancelled = await db.execute(
                        f"UPDATE plans SET status=?,updated_at=? WHERE status=? AND id IN ({placeholders})",  # noqa: S608 -- placeholders only
                        (
                            PlanStatus.CANCELLED,
                            now,
                            PlanStatus.RUNNING,
                            *cancelled_plan_ids,
                        ),
                    )
                    recovered_cancelled = cancelled.rowcount
                recovered_tails = 0
                for stale_plan_id, stale_turn_id in stale_tail_claims:
                    released = await db.execute(
                        "UPDATE plans SET execution_turn_id=NULL,updated_at=? WHERE id=? AND execution_turn_id=? AND status!=?",
                        (
                            now,
                            stale_plan_id,
                            stale_turn_id,
                            PlanStatus.RUNNING,
                        ),
                    )
                    recovered_tails += released.rowcount
                await db.commit()
                return (
                    recovered_running + recovered_cancelled + recovered_tails,
                    recovered_steps,
                )
            except BaseException:
                # ROLLBACK may itself raise (e.g. COMMIT failed and transaction inactive); never replace real cause
                # (implementation review P2-3); implicit rollback on finally closing connection protects data.
                with contextlib.suppress(Exception):
                    await db.execute("ROLLBACK")
                raise

    async def gc_stale_proposed(self, older_than_days: int = 14) -> int:
        """C1: 清理僵尸提案 —— 超 N 天仍未批准的 PROPOSED 计划(+其 steps)删除。
        **只碰 PROPOSED**(用户 N 天没批准=弃用);RUNNING/BLOCKED/COMPLETED/CANCELLED/
        FAILED 一律不动(活的 / 有价值 / 终态记录)。boot 时调一次,防提案无限累积。
        返删除 plan 数。older_than_days 由 env BOWHEAD_PLAN_PROPOSED_GC_DAYS 传入(默认 14)。"""
        from datetime import timedelta

        cutoff = (datetime.now(tz=UTC) - timedelta(days=max(older_than_days, 1))).isoformat()
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "SELECT id FROM plans WHERE status = ? AND created_at < ?",
                (PlanStatus.PROPOSED, cutoff),
            )
            ids = [r[0] for r in await cur.fetchall()]
            for pid in ids:
                await db.execute("DELETE FROM plan_steps WHERE plan_id = ?", (pid,))
                await db.execute("DELETE FROM plans WHERE id = ?", (pid,))
            await db.commit()
            return len(ids)
