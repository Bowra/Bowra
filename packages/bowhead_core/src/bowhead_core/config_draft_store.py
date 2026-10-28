"""配置副驾草稿存储（tech-design-batch1-draft-store.md §2）.

subagent_draft_store 泛化四件套的同模式新写（机制照抄、代码独立，subagent 侧零触碰）：
owner+team 双绑定同 404、row_version CAS、envelope 版本化 JSON、TTL 维护。

kind='lite_proposal'（本批）：轻确认条提议。状态机 proposed→applied→reverted /
proposed→ignored，三个 TTL 列全 NULL——不过期、不 scrub、不 purge（R7「撤销无时限」
拍板；rows 与 turn_log 事件 payload 逐字节相同，清理无隐私收益只杀 undo）。
kind='form_draft'（批 1-b 启用）：ConfigDrawer 草稿，届时用三段 TTL。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import aiosqlite

from bowhead_core._db import open_shared_ctx

CONFIG_DRAFT_SCHEMA_VERSION = 1
KIND_LITE_PROPOSAL = "lite_proposal"
KIND_FORM_DRAFT = "form_draft"

# lite_proposal state machine (store-enforced; replay/conflict triage in transition)
LITE_STATUS_PROPOSED = "proposed"
LITE_STATUS_APPLIED = "applied"
LITE_STATUS_REVERTED = "reverted"
LITE_STATUS_IGNORED = "ignored"

# form_draft state machine (batch 1-b, tech-design-batch1b-drawer.md §2.1)
FORM_STATUS_DRAFT = "draft"
FORM_STATUS_CONSUMED = "consumed"
FORM_STATUS_DISCARDED = "discarded"
FORM_STATUS_EXPIRED = "expired"
FORM_DRAFT_ACTIVE_HOURS = 24  # Active semantics (expired chip + find_active freshness); no scrub or purge


def _form_payload(
    fields: dict[str, Any],
    field_meta: dict[str, Any],
    notes: dict[str, str],
    target: dict[str, Any] | None,
    changed_fields_cumulative: list[str] | None,
) -> dict[str, Any]:
    """form_draft 的 payload 信封。

    批 3（3-0b）：两个新键**只在非 None 时出现**——缺省时 payload 与批 1-b 逐字节相同，
    单例面零差异。两个方法共用本函数，避免「加了一处忘了另一处」（create/update 各自
    重建 payload 是 r1-P1-2 那个 bug 的成因）。
    """
    payload: dict[str, Any] = {"fields": fields, "field_meta": field_meta, "notes": notes, "result": None}
    if target is not None:
        payload["target"] = target
    if changed_fields_cumulative is not None:
        payload["changed_fields_cumulative"] = list(changed_fields_cumulative)
    return payload


_REQUIRED_TABLES = frozenset({"config_copilot_draft"})


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ConfigDraftSchemaUnavailableError(RuntimeError):
    pass


class ConfigDraftSchemaUnsupportedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigDraft:
    id: str
    team_id: str
    owner_user_id: str
    kind: str
    form_id: str
    workspace_id: str | None
    turn_id: str | None
    session_id: str | None
    status: str
    payload: dict[str, Any] | None
    row_version: int
    expires_at: str | None
    content_scrub_at: str | None
    purge_at: str | None
    applied_at: str | None
    reverted_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TransitionResult:
    outcome: str  # "ok" | "replay" | "conflict" | "not_found"
    draft: ConfigDraft | None


@dataclass(frozen=True, slots=True)
class ConfigDraftMaintenanceResult:
    expired: int
    scrubbed: int
    purged: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _envelope(kind: str, value: Any) -> str:
    return json.dumps(
        {"schema_version": CONFIG_DRAFT_SCHEMA_VERSION, "kind": kind, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_envelope(raw: str | None, *, kind: str) -> Any:
    if raw is None:
        return None
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigDraftSchemaUnsupportedError("config_draft_schema_unsupported") from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != CONFIG_DRAFT_SCHEMA_VERSION
        or envelope.get("kind") != kind
        or "value" not in envelope
    ):
        raise ConfigDraftSchemaUnsupportedError("config_draft_schema_unsupported")
    return envelope["value"]


def _payload_kind(kind: str) -> str:
    return "config_lite_proposal" if kind == KIND_LITE_PROPOSAL else "config_form_draft"


def _row_to_draft(row: aiosqlite.Row) -> ConfigDraft:
    payload = _decode_envelope(row["payload_json"], kind=_payload_kind(str(row["kind"])))
    if payload is not None and not isinstance(payload, dict):
        raise ConfigDraftSchemaUnsupportedError("config_draft_schema_unsupported")
    return ConfigDraft(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        owner_user_id=str(row["owner_user_id"]),
        kind=str(row["kind"]),
        form_id=str(row["form_id"]),
        workspace_id=row["workspace_id"],
        turn_id=row["turn_id"],
        session_id=row["session_id"],
        status=str(row["status"]),
        payload=payload,
        row_version=int(row["row_version"]),
        expires_at=row["expires_at"],
        content_scrub_at=row["content_scrub_at"],
        purge_at=row["purge_at"],
        applied_at=row["applied_at"],
        reverted_at=row["reverted_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class ConfigDraftStore:
    def __init__(self, db_path: str, *, clock: Clock | None = None) -> None:
        self.db_path = str(db_path)
        self._clock = clock or SystemClock()

    async def initialize(self) -> None:
        """Verify the Alembic-owned schema without creating or altering it."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ) as cursor,
        ):
            tables = {str(row[0]) for row in await cursor.fetchall()}
        missing = _REQUIRED_TABLES - tables
        if missing:
            raise ConfigDraftSchemaUnavailableError(
                "config_draft_schema_missing:" + ",".join(sorted(missing)),
            )

    async def create_lite_proposal(
        self,
        *,
        draft_id: str,
        team_id: str,
        owner_user_id: str,
        form_id: str,
        workspace_id: str | None,
        turn_id: str | None,
        session_id: str | None,
        rows: list[dict[str, Any]],
        notes: dict[str, str],
        workspace_name: str | None,
        settings_target: dict[str, Any] | None = None,
    ) -> ConfigDraft:
        now_s = _timestamp(self._clock.now())
        payload = {"rows": rows, "notes": notes, "workspace_name": workspace_name}
        if settings_target is not None:
            payload["settings_target"] = settings_target
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO config_copilot_draft(
                  id, team_id, owner_user_id, kind, form_id, workspace_id,
                  turn_id, session_id, status, payload_json, row_version,
                  expires_at, content_scrub_at, purge_at, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,NULL,?,?)
                """,
                (
                    draft_id,
                    team_id,
                    owner_user_id,
                    KIND_LITE_PROPOSAL,
                    form_id,
                    workspace_id,
                    turn_id,
                    session_id,
                    LITE_STATUS_PROPOSED,
                    _envelope(_payload_kind(KIND_LITE_PROPOSAL), payload),
                    now_s,
                    now_s,
                ),
            )
            await db.commit()
        created = await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)
        if created is None:
            raise RuntimeError("config_draft_create_lost")
        return created

    async def create_form_draft(
        self,
        *,
        draft_id: str,
        team_id: str,
        owner_user_id: str,
        form_id: str,
        workspace_id: str | None,
        turn_id: str | None,
        session_id: str | None,
        fields: dict[str, Any],
        field_meta: dict[str, Any],
        notes: dict[str, str],
        target: dict[str, Any] | None = None,
        changed_fields_cumulative: list[str] | None = None,
    ) -> ConfigDraft:
        """批 1-b（tech-design-batch1b §2.1）：ConfigDrawer 草稿。

        expires_at=now+24h（活跃语义：chip 过期态 + find_active 新鲜度）；scrub/purge 双
        NULL——fields 全量已随 config.draft_updated 事件永存 turn_log，scrub 零隐私收益；
        purge 会把 consumed 行变结构化 404 → 「已保存」历史失真（lite 同理不清理）。

        批 3（3-0b）：`target` / `changed_fields_cumulative` 两个可选键——**缺省 None 时
        不写进 payload**，单例面（批 0~1 五个面）的 payload 逐字节不变。store 层不做隐式
        合并：payload 由调用方全权决定（`update_form_draft_fields` 是整包覆盖语义，调用方
        必须显式回传已有 target，否则会被抹掉——这正是 r1-P1-2 逮到的坑）。
        """
        now = _utc(self._clock.now())
        now_s = _timestamp(now)
        expires_s = _timestamp(now + timedelta(hours=FORM_DRAFT_ACTIVE_HOURS))
        payload = _form_payload(fields, field_meta, notes, target, changed_fields_cumulative)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO config_copilot_draft(
                  id, team_id, owner_user_id, kind, form_id, workspace_id,
                  turn_id, session_id, status, payload_json, row_version,
                  expires_at, content_scrub_at, purge_at, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,NULL,NULL,?,?)
                """,
                (
                    draft_id,
                    team_id,
                    owner_user_id,
                    KIND_FORM_DRAFT,
                    form_id,
                    workspace_id,
                    turn_id,
                    session_id,
                    FORM_STATUS_DRAFT,
                    _envelope(_payload_kind(KIND_FORM_DRAFT), payload),
                    expires_s,
                    now_s,
                    now_s,
                ),
            )
            await db.commit()
        created = await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)
        if created is None:
            raise RuntimeError("config_draft_create_lost")
        return created

    async def find_active_form_draft(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        session_id: str,
        form_id: str,
    ) -> ConfigDraft | None:
        """session 内活跃草稿：status='draft' 且未过期的最新一条。"""
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM config_copilot_draft
                WHERE team_id=? AND owner_user_id=? AND session_id=? AND form_id=?
                  AND kind=? AND status=? AND expires_at>?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (team_id, owner_user_id, session_id, form_id, KIND_FORM_DRAFT, FORM_STATUS_DRAFT, now_s),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_draft(row) if row is not None else None

    async def find_latest_form_draft(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        session_id: str,
        form_id: str,
    ) -> ConfigDraft | None:
        """任意状态最新一条（consumed 护栏近邻查询，tech-design-batch1b §2.3-6）。"""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM config_copilot_draft
                WHERE team_id=? AND owner_user_id=? AND session_id=? AND form_id=? AND kind=?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (team_id, owner_user_id, session_id, form_id, KIND_FORM_DRAFT),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_draft(row) if row is not None else None

    async def update_form_draft_fields(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        expected_version: int,
        fields: dict[str, Any],
        field_meta: dict[str, Any],
        notes: dict[str, str],
        target: dict[str, Any] | None = None,
        changed_fields_cumulative: list[str] | None = None,
    ) -> TransitionResult:
        """CAS 更新草稿内容（status 恒 'draft'——只改 payload，不可转移状态）。

        rowcount!=1 → conflict + 现态回读：调用方按 status 分诊（仍 draft=并发 propose 抢先
        bump → 重读重合并；终态/过期 → 新建，tech-design-batch1b §2.3-3）。
        """
        now_s = _timestamp(self._clock.now())
        payload = _form_payload(fields, field_meta, notes, target, changed_fields_cumulative)
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE config_copilot_draft
                SET payload_json=?, row_version=row_version+1, updated_at=?
                WHERE id=? AND team_id=? AND owner_user_id=? AND kind=?
                  AND row_version=? AND status=? AND expires_at>?
                """,
                (
                    _envelope(_payload_kind(KIND_FORM_DRAFT), payload),
                    now_s,
                    draft_id,
                    team_id,
                    owner_user_id,
                    KIND_FORM_DRAFT,
                    expected_version,
                    FORM_STATUS_DRAFT,
                    now_s,
                ),
            )
            await db.commit()
            ok = cursor.rowcount == 1
        draft = await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)
        if draft is None:
            return TransitionResult("not_found", None)
        return TransitionResult("ok" if ok else "conflict", draft)

    async def get_draft(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
    ) -> ConfigDraft | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM config_copilot_draft
                WHERE id=? AND team_id=? AND owner_user_id=?
                """,
                (draft_id, team_id, owner_user_id),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_draft(row) if row is not None else None

    async def transition(
        self,
        draft_id: str,
        *,
        team_id: str,
        owner_user_id: str,
        to_status: str,
        from_statuses: tuple[str, ...],
        result_patch: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """CAS 状态转移。replay=已在目标态（幂等成功，不重写）；conflict=非法前态。

        result_patch（批 1-b）：仅 consumed 转移消费——保存回执写进 payload.result。
        """
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT status, kind, payload_json FROM config_copilot_draft
                    WHERE id=? AND team_id=? AND owner_user_id=?
                    """,
                    (draft_id, team_id, owner_user_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return TransitionResult("not_found", None)
                current = str(row["status"])
                if current == to_status:
                    await db.rollback()
                    outcome = "replay"
                elif current not in from_statuses:
                    await db.rollback()
                    outcome = "conflict"
                else:
                    stamp_col = {
                        LITE_STATUS_APPLIED: ", applied_at=?",
                        LITE_STATUS_REVERTED: ", reverted_at=?",
                    }.get(to_status, "")
                    params: list[Any] = [to_status]
                    if stamp_col:
                        params.append(now_s)
                    payload_col = ""
                    if result_patch is not None and row["payload_json"] is not None:
                        kind = str(row["kind"])
                        value = _decode_envelope(row["payload_json"], kind=_payload_kind(kind))
                        if isinstance(value, dict):
                            value["result"] = result_patch
                            payload_col = ", payload_json=?"
                            params.append(_envelope(_payload_kind(kind), value))
                    params.extend([now_s, draft_id, team_id, owner_user_id])
                    cursor = await db.execute(
                        f"""
                        UPDATE config_copilot_draft
                        SET status=?{stamp_col}{payload_col}, row_version=row_version+1, updated_at=?
                        WHERE id=? AND team_id=? AND owner_user_id=?
                          AND status IN ({",".join("?" * len(from_statuses))})
                        """,
                        (*params, *from_statuses),
                    )
                    await db.commit()
                    outcome = "ok" if cursor.rowcount == 1 else "conflict"
            except BaseException:
                await db.rollback()
                raise
        draft = await self.get_draft(draft_id, team_id=team_id, owner_user_id=owner_user_id)
        return TransitionResult(outcome, draft)

    async def run_maintenance(self, *, limit: int = 100) -> ConfigDraftMaintenanceResult:
        """三段 SQL 全部只触 TTL 非空行——lite_proposal（TTL 全 NULL）天然零触碰。"""
        if not 1 <= limit <= 1000:
            raise ValueError("maintenance limit must be in [1,1000]")
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                expired = await db.execute(
                    """
                    UPDATE config_copilot_draft
                    SET status='expired', row_version=row_version+1, updated_at=?
                    WHERE id IN (
                      SELECT id FROM config_copilot_draft
                      WHERE status='draft' AND expires_at IS NOT NULL AND expires_at<=?
                      ORDER BY expires_at LIMIT ?
                    )
                    """,
                    (now_s, now_s, limit),
                )
                scrubbed = await db.execute(
                    """
                    UPDATE config_copilot_draft
                    SET payload_json=NULL, row_version=row_version+1, updated_at=?
                    WHERE id IN (
                      SELECT id FROM config_copilot_draft
                      WHERE content_scrub_at IS NOT NULL AND content_scrub_at<=?
                        AND payload_json IS NOT NULL
                      ORDER BY content_scrub_at LIMIT ?
                    )
                    """,
                    (now_s, now_s, limit),
                )
                purged = await db.execute(
                    """
                    DELETE FROM config_copilot_draft
                    WHERE id IN (
                      SELECT id FROM config_copilot_draft
                      WHERE purge_at IS NOT NULL AND purge_at<=?
                      ORDER BY purge_at LIMIT ?
                    )
                    """,
                    (now_s, limit),
                )
                await db.commit()
                return ConfigDraftMaintenanceResult(
                    expired=max(expired.rowcount, 0),
                    scrubbed=max(scrubbed.rowcount, 0),
                    purged=max(purged.rowcount, 0),
                )
            except BaseException:
                await db.rollback()
                raise


__all__ = [
    "CONFIG_DRAFT_SCHEMA_VERSION",
    "FORM_DRAFT_ACTIVE_HOURS",
    "FORM_STATUS_CONSUMED",
    "FORM_STATUS_DISCARDED",
    "FORM_STATUS_DRAFT",
    "FORM_STATUS_EXPIRED",
    "KIND_FORM_DRAFT",
    "KIND_LITE_PROPOSAL",
    "LITE_STATUS_APPLIED",
    "LITE_STATUS_IGNORED",
    "LITE_STATUS_PROPOSED",
    "LITE_STATUS_REVERTED",
    "ConfigDraft",
    "ConfigDraftMaintenanceResult",
    "ConfigDraftSchemaUnavailableError",
    "ConfigDraftSchemaUnsupportedError",
    "ConfigDraftStore",
    "TransitionResult",
]
