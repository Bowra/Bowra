"""Memory candidate store + Dreaming Light.

Dreaming Light：每个正常完成的 turn 后，由 LLM 观察
    (user_text, final_answer, intent, tool_chain)
提议 0-3 条候选用户偏好（e.g. "用户偏好按渠道拆分 GMV"）。
候选写入 memory_candidates 表，**用户**在 /memory 页自审 →
approve 后才写入 user_memory_entries (V3 PRD-B: user-self-approve, not admin).

流程（memory-revive S1 起真实接线）：
    turn_runner.maybe_dream_post_turn()            # turn 收尾 fire-and-forget task
       └─ DreamingLight.observe(DreamObservation)
            └─ check pause_cache (V3 PRD-B)        # user-paused → skip
            └─ LLM propose candidates（prompt 带存量记忆摘要防重复/提更新）
                 └─ MemoryCandidateStore.enqueue_if_new (pending, 去重含 rejected 抑制)
                      → user approve (chat 内联卡 / memory 页)
                           → UserMemoryStore.upsert

schema:
    memory_candidates(
        id, user_id, subject_type, kind, key, value_json,
        reason, source_turn_id, status, decided_by, decided_at,
        created_at, updated_at, workspace_id,
    )
    status: pending | approved | rejected
    workspace_id: Source Bowhead Space (frozen workspace_id spelling).
        S1 仅记录（scope 预留），S2 做空间级召回。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_memory.privacy_guard import (
    ensure_memory_write_allowed,
    is_memory_read_hidden,
)

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_candidates (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    subject_type    TEXT NOT NULL DEFAULT 'user',
    kind            TEXT NOT NULL,          -- preference | constraint | datarc_binding
    key             TEXT NOT NULL,
    value_json      TEXT NOT NULL,          -- JSON
    reason          TEXT,                   -- LLM 的推理
    source_turn_id  TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    decided_by      TEXT,
    decided_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    content_fingerprint TEXT,
    decision_kind   TEXT,
    target_scope_type TEXT,
    target_workspace_id TEXT,
    approved_resource_type TEXT,
    approved_resource_id TEXT,
    duplicate_of_candidate_id TEXT,
    duplicate_count INTEGER NOT NULL DEFAULT 1,
    content_redacted_at TEXT,
    -- 记忆重做 批1（tech-design §4.2 a）：写入契约上的两个槽。
    -- supersedes_json 之前只能写进 reason —— 一个**没有任何代码解析**的自由文本字段，
    -- 于是「提议更新」永远只是句话，净利率被原样提议了 5 次。
    memory_type     TEXT,                   -- procedural | semantic
    supersedes_json TEXT                    -- JSON array of memory_entries.id
);
CREATE INDEX IF NOT EXISTS idx_cand_status ON memory_candidates(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cand_user ON memory_candidates(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_candidate_suppressions (
    user_id             TEXT NOT NULL,
    source_scope_key    TEXT NOT NULL,
    kind                TEXT NOT NULL,
    fingerprint         TEXT NOT NULL,
    decision_kind       TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    PRIMARY KEY(user_id, source_scope_key, kind, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_candidate_suppression_expiry
    ON memory_candidate_suppressions(expires_at);
"""

# memory-revive S1/S2: column migrations (ALTER older databases when a column is missing).
_MIGRATION_COLUMNS = {
    "workspace_id": "ALTER TABLE memory_candidates ADD COLUMN workspace_id TEXT",
    # S2 C3: warn about expiry only once per lifetime; record now when the warn threshold is crossed to avoid repeated daily GC reminders.
    "expiry_warned_at": "ALTER TABLE memory_candidates ADD COLUMN expiry_warned_at TEXT",
    "version": "ALTER TABLE memory_candidates ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
    "content_fingerprint": "ALTER TABLE memory_candidates ADD COLUMN content_fingerprint TEXT",
    "decision_kind": "ALTER TABLE memory_candidates ADD COLUMN decision_kind TEXT",
    "target_scope_type": "ALTER TABLE memory_candidates ADD COLUMN target_scope_type TEXT",
    "target_workspace_id": "ALTER TABLE memory_candidates ADD COLUMN target_workspace_id TEXT",
    "approved_resource_type": "ALTER TABLE memory_candidates ADD COLUMN approved_resource_type TEXT",
    "approved_resource_id": "ALTER TABLE memory_candidates ADD COLUMN approved_resource_id TEXT",
    "duplicate_of_candidate_id": "ALTER TABLE memory_candidates ADD COLUMN duplicate_of_candidate_id TEXT",
    "duplicate_count": "ALTER TABLE memory_candidates ADD COLUMN duplicate_count INTEGER NOT NULL DEFAULT 1",
    "content_redacted_at": "ALTER TABLE memory_candidates ADD COLUMN content_redacted_at TEXT",
    "memory_type": "ALTER TABLE memory_candidates ADD COLUMN memory_type TEXT",
    "supersedes_json": "ALTER TABLE memory_candidates ADD COLUMN supersedes_json TEXT",
    # Track B batch 1b: **who proposed** the candidate. Default `dreaming_light` preserves existing row semantics.
    # ⚠ Do not reuse `kind` (a strict allowlist from `:771`; out-of-range values raise ValueError),
    # or `reason` (free text, parsed by nobody). These are the two parts of brief pitfall 1.
    "origin": "ALTER TABLE memory_candidates ADD COLUMN origin TEXT NOT NULL DEFAULT 'dreaming_light'",
}

#: Candidate origin. **Allowlist** semantics: existing consumers (triage / GC / batch scripts) all use
#: `WHERE origin='dreaming_light'`, leaving new origins untouched by default.
ORIGIN_DREAMING = "dreaming_light"
ORIGIN_FEEDBACK_DRAFT = "feedback_draft"

#: Origins that **write** suppressions. Reads do not use this; they rely on pools separated by `_suppression_scope_key`
#: (the two mechanisms are redundant; keep only one, as its docstring explains). The two pools have opposite meanings:
#: background distillation suppression means "I already rejected this; do not guess the same thing again";
#: drafts come from **explicit user statements**. Blocking them with background suppressions silently discards explicit requests.
#: The second half of brief pitfall 3.
_SUPPRESSION_ORIGINS = frozenset({ORIGIN_DREAMING})


def _suppression_scope_key(origin: str, workspace_id: str | None) -> str:
    """抑制表的作用域键**带 origin 前缀** —— 两池不串扰。

    表的主键是 `(user_id, source_scope_key, kind, fingerprint)`，没有 origin 列；
    把 origin 编进 scope key 是**不改表**就把两池分开的最小做法。
    ⚠ 读写两侧必须用同一个函数，否则写进去的键读不出来（等于抑制全失效，
    而那是**静默**的：只表现为「拒过的还在提」）。
    """
    return f"{origin}:{_candidate_scope_key(workspace_id)}"


CandidateStatus = Literal["pending", "approved", "rejected"]

#: Memory redesign batch 2: `decided_by` for batch cleanup scripts. See decision_kind categories in `set_decision`.
SYSTEM_TRIAGE_ACTOR = "system_triage"
#: Decision attribution for "Edit / redraft". Like `SYSTEM_TRIAGE_ACTOR`,
#: this identifies **who made the decision and its category**, not who clicked the button.
REDRAFT_ACTOR = "system_redraft"

# ── User-facing reasons never carry memory ids ────────────────────────────────────────────────
#: Canonical memory entry id (`mem_` + hex). Dreaming sees existing memories as `[mem_xxx] key = value`
#: and copies the id into the reason it writes for people (116: 「…与已有记忆 mem_7c729bd92a5c 矛盾」).
#: Stored reasons keep the id; it is resolved to the memory's text only at display time, through the user-scoped,
#: read-hidden-gated gateway lookup, so a deleted or hidden memory never reappears in a reason.
#: ASCII boundaries instead of `\b`: CJK characters count as word characters, so `记忆mem_x矛盾` has no `\b`.
MEMORY_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9_])mem_[0-9a-f]{6,32}(?![A-Za-z0-9_])")
_MEMORY_ID_WITH_SPACE = re.compile(r"([ \t]*)(?<![A-Za-z0-9_])(mem_[0-9a-f]{6,32})(?![A-Za-z0-9_])([ \t]*)")
_CJK_CONTEXT = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]")
_EMPTY_BRACKETS = re.compile(r"[ \t]*(\(\s*\)|（\s*）|\[\s*\])")
#: Characters of the referenced memory quoted in place of its id.
MEMORY_REFERENCE_MAX_CHARS = 24


def memory_ids_in(text: str | None) -> set[str]:
    """Memory ids mentioned in free text (for resolving them to their text before display)."""
    return set(MEMORY_ID_PATTERN.findall(text or ""))


def humanize_memory_ids(text: str | None, memory_texts: dict[str, str] | None = None) -> str | None:
    """Replace memory ids in a user-facing sentence with the memory's own (truncated) text.

    A known id becomes the quoted memory (「…」 in CJK sentences, “…” otherwise); an id that cannot be
    resolved is dropped together with the spacing it needed. Never returns an id.
    """
    if not text:
        return text
    texts = memory_texts or {}
    cjk_sentence = re.search(r"[\u3400-\u9fff]", text) is not None

    def _replace(match: re.Match[str]) -> str:
        lead, _mid, trail = match.group(1), match.group(2), match.group(3)
        before = text[match.start() - 1] if match.start() > 0 else ""
        after = text[match.end()] if match.end() < len(text) else ""
        quoted = " ".join((texts.get(_mid) or "").split())
        if not quoted:
            # Keep one space only when the id sat between two non-CJK words ("memory mem_x was" → "memory was").
            joins_words = bool(lead and trail and before and after)
            return " " if joins_words and not _CJK_CONTEXT.match(before) and not _CJK_CONTEXT.match(after) and after not in ".,;:!?)]" else ""
        if len(quoted) > MEMORY_REFERENCE_MAX_CHARS:
            quoted = quoted[:MEMORY_REFERENCE_MAX_CHARS].rstrip() + "…"
        if cjk_sentence:
            left = " " if lead and before and not _CJK_CONTEXT.match(before) else ""
            right = " " if trail and after and not _CJK_CONTEXT.match(after) else ""
            return f"{left}「{quoted}」{right}"
        return f"{' ' if lead else ''}“{quoted}”{' ' if trail else ''}"

    out = _MEMORY_ID_WITH_SPACE.sub(_replace, text)
    out = _EMPTY_BRACKETS.sub("", out)
    return out.strip() or None


#: Attribution on the old pending candidate when a newer statement covers the same topic.
#: ★ Separate from `REDRAFT_ACTOR`: that records the user **clicking "Edit"**; this records the user
#: restating the same thing differently, with the system deciding to supersede it. The audit entries must distinguish them.
#:
#: ⚠ Used **only** in `enqueue_if_new` queue consolidation (direct UPDATE), without calling
#: `set_decision`, which opens its own connection; calling it inside the enqueue transaction would nest transactions.
#: Therefore **do not** add a branch for it in `set_decision`: that code can never be reached
#: (a branch added on 2026-09-09 was proven dead by mutation testing and removed).
SUPERSEDE_ACTOR = "system_superseded"

#: The second return value of `enqueue_if_new_with_outcome`: **why** enqueueing did or did not happen.
#: ★ Three `return None` paths previously looked identical: duplicate pending (already awaiting a decision, so a card exists),
#: suppression after user rejection, and write-gate refusal. Callers could not distinguish them, so the taint fallback path reported
#: "Added as a candidate; you can confirm it" for all three, although the latter two have no card (checklist ④, found on 2026-09-10).
#: ⚠ `bowhead_core.memory_auto_capture` has the same literals (it cannot import this package); a guard test enforces agreement.
ENQUEUE_CREATED = "created"
ENQUEUE_DUPLICATE_PENDING = "duplicate_pending"
ENQUEUE_SUPPRESSED = "suppressed"
ENQUEUE_GATE_REFUSED = "gate_refused"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass
class MemoryCandidate:
    id: str
    user_id: str
    subject_type: str
    kind: str
    key: str
    value: Any
    reason: str | None
    source_turn_id: str | None
    status: str
    decided_by: str | None
    decided_at: str | None
    created_at: str
    updated_at: str
    # memory-revive S1: source space (scope reserved; S2 adds space-level recall).
    workspace_id: str | None = None
    version: int = 1
    decision_kind: str | None = None
    approved_resource_type: str | None = None
    approved_resource_id: str | None = None
    # Memory redesign batch 1: type and supersession declaration (None = omitted by distillation / an old row).
    memory_type: str | None = None
    supersedes: tuple[str, ...] = ()
    #: The pool this candidate came from (`ORIGIN_DREAMING` / `ORIGIN_FEEDBACK_DRAFT`).
    #: ⚠ The column already existed (`_MIGRATION_COLUMNS`), but **this dataclass had never included it**
    #: ⇒ stored in the database, omitted by `_row()`, and downstream `getattr(cand, "origin", ...)` always returned the default.
    #: Parsing is a field allowlist: a new column omitted here is invisible to everything outside the package.
    #: Found by the "Edit" endpoint on 2026-09-09 (draft candidates were classified as dreaming, immediately returning 400).
    origin: str = ORIGIN_DREAMING


@dataclass
class MemoryCandidateStore:
    db_path: str
    fingerprint_key: str | bytes | None = field(default=None, repr=False)
    _fingerprint_secret: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        seed = self.fingerprint_key or f"dev-only:{Path(self.db_path).resolve()}"
        seed_bytes = seed.encode("utf-8") if isinstance(seed, str) else seed
        self._fingerprint_secret = hmac.new(
            seed_bytes,
            b"bowhead/memory-candidate/fingerprint/v1",
            hashlib.sha256,
        ).digest()

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA secure_delete=ON")
            await db.executescript(SCHEMA)
            cur = await db.execute("PRAGMA table_info(memory_candidates)")
            existing = {row[1] for row in await cur.fetchall()}
            for column, ddl in _MIGRATION_COLUMNS.items():
                if column not in existing:
                    await db.execute(ddl)
            await _normalize_candidate_decisions(db)
            await _backfill_candidate_fingerprints(
                db,
                secret=self._fingerprint_secret,
            )
            await _backfill_candidate_suppressions(db)
            await _redact_nonrecoverable_candidates(db)
            await _canonicalize_pending_duplicates(db)
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_memory_candidate_pending_fingerprint
                ON memory_candidates(user_id, content_fingerprint)
                WHERE status='pending'
                  AND content_fingerprint IS NOT NULL
                  AND duplicate_of_candidate_id IS NULL
                """,
            )
            await db.commit()

    async def enqueue(
        self,
        *,
        user_id: str,
        kind: str,
        key: str,
        value: Any,
        reason: str | None,
        source_turn_id: str | None,
        workspace_id: str | None = None,
        memory_type: str | None = None,
        supersedes: object = (),
        origin: str = ORIGIN_DREAMING,
    ) -> MemoryCandidate:
        value_json = _candidate_value_json(
            kind=kind,
            key=key,
            value=value,
        )
        fingerprint = _candidate_content_fingerprint(
            secret=self._fingerprint_secret,
            kind=kind,
            value_json=value_json,
            workspace_id=workspace_id,
            supersedes_json=_supersedes_json(supersedes),
        )
        cid = f"cand_{uuid4().hex[:12]}"
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await _prepare_user_write(
                db,
                user_id=user_id,
            )
            await db.execute(
                "INSERT INTO memory_candidates(id, user_id, kind, key, value_json, "
                "reason, source_turn_id, created_at, updated_at, workspace_id, "
                "content_fingerprint, memory_type, supersedes_json, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    user_id,
                    kind,
                    key,
                    value_json,
                    reason,
                    source_turn_id,
                    now,
                    now,
                    workspace_id,
                    fingerprint,
                    memory_type,
                    _supersedes_json(supersedes),
                    origin,
                ),
            )
            await db.commit()
            # ⚠ Read back **inside** the `async with` block: the connection closes when the block exits
            # (observed `ValueError: no active connection`).
            return await _read_back(db, cid)

    async def list_pending(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryCandidate]:
        sql = "SELECT * FROM memory_candidates WHERE status='pending'"
        params: list = []
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if user_id and await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def list_for_user(
        self,
        *,
        user_id: str,
        status: str = "pending",
        limit: int = 50,
        source_turn_id: str | None = None,
    ) -> list[MemoryCandidate]:
        """V3 PRD-B: list candidates for a specific user filtered by status.

        status: 'pending' | 'approved' | 'rejected' | 'all'
        source_turn_id: memory-revive S1 — chat 内联卡的 turn 级过滤（done 后 FE
        轮询本 turn 产生的候选）。
        """
        if status not in ("pending", "approved", "rejected", "all"):
            raise ValueError(f"invalid status: {status!r}")
        sql = "SELECT * FROM memory_candidates WHERE user_id=?"
        params: list[Any] = [user_id]
        if status != "all":
            sql += " AND status=?"
            params.append(status)
        if source_turn_id:
            sql += " AND source_turn_id=?"
            params.append(source_turn_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def enqueue_if_new_with_outcome(
        self,
        *,
        user_id: str,
        kind: str,
        key: str,
        value: Any,
        reason: str | None,
        source_turn_id: str | None,
        workspace_id: str | None = None,
        memory_type: str | None = None,
        supersedes: object = (),
        origin: str = ORIGIN_DREAMING,
        dedupe_window_days: int = 7,
        decided_suppress_days: int = 30,
    ) -> tuple[MemoryCandidate | None, str]:
        """V3.5 PRD-C: dedupe enqueue — 回 `(候选, 为什么)`；None 时第二项说明是哪一种没入队（equivalent candidate
        (same user+kind+value) exists. memory-revive S1 扩展去重范围：

        - pending：**任意** status='pending' 同值即抑制（S2 C2：去掉原 7d 时间窗——
          pending 存活到 GC 30d，但去重只挡 7d，第 7~30 天同值可重复入队 = 复读机长尾）。
          `dedupe_window_days` 保留签名向后兼容，pending 分支不再消费它。
        - approved：decided_suppress_days（30d）内已批准入库的同值无需再提
        - rejected：仅**用户亲手拒绝**的抑制（review-fix F2：GC 的 system_auto_reject
          是「过期清理」不是「用户决策」——用户从没看过的候选不许被再压 30 天）
        """
        _ = (
            dedupe_window_days,
            decided_suppress_days,
        )  # retained for call compatibility; persisted suppressions own expiry
        value_json = _candidate_value_json(
            kind=kind,
            key=key,
            value=value,
        )
        fingerprint = _candidate_content_fingerprint(
            secret=self._fingerprint_secret,
            kind=kind,
            value_json=value_json,
            workspace_id=workspace_id,
            supersedes_json=_supersedes_json(supersedes),
        )
        # ── Memory redesign batch 2: place the write gate at the **common boundary** (tech-design §4.3) ──
        # ★ The first version placed it in `DreamingLight.observe`, which is only **one caller**.
        # `capture_nl_intent` (implicit natural-language intent capture) also calls `enqueue_if_new` directly,
        # so the one-off requests / language preferences / pasted credentials it enqueued bypassed the gate entirely
        # and still appeared on the memory page and bell: exactly what tightening upstream acceptance before making the bell useful should prevent.
        # "Changing one place changes nothing": put the gate where every write path must pass.
        if not _write_gate_allows(kind=kind, key=key, value=value, turn_id=source_turn_id or ""):
            logger.info("memory_candidate.gate_refused kind=%s key=%s", kind, key)
            return None, ENQUEUE_GATE_REFUSED

        cid = f"cand_{uuid4().hex[:12]}"
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await _prepare_user_write(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "SELECT id FROM memory_candidates WHERE user_id=? AND content_fingerprint=? AND status='pending'",
                (user_id, fingerprint),
            )
            if await cur.fetchone():
                await db.rollback()
                logger.info("memory_candidate.skipped outcome=%s kind=%s key=%s", ENQUEUE_DUPLICATE_PENDING, kind, key)
                return None, ENQUEUE_DUPLICATE_PENDING
            # Suppressions are pooled by origin (see `_suppression_scope_key`): query only the candidate's own pool,
            # never another origin's. ⚠ **Do not add a second `if origin in ...` guard here**:
            # testing showed the two checks were redundant (removing either alone kept tests green; removing both made them fail).
            # Keeping both creates two implementations, neither individually verified by tests.
            suppressed = False
            cur = await db.execute(
                """
                SELECT 1
                FROM memory_candidate_suppressions
                WHERE user_id=?
                  AND source_scope_key=?
                  AND kind=?
                  AND fingerprint=?
                  AND datetime(expires_at)>datetime('now')
                """,
                (
                    user_id,
                    _suppression_scope_key(origin, workspace_id),
                    kind,
                    fingerprint,
                ),
            )
            suppressed = bool(await cur.fetchone())
            if suppressed:
                await db.rollback()
                logger.info("memory_candidate.skipped outcome=%s kind=%s key=%s", ENQUEUE_SUPPRESSED, kind, key)
                return None, ENQUEUE_SUPPRESSED
            try:
                # ★★ Keep only the latest candidate for the same topic.
                #
                # Production observation (116, 2026-09-09): among 38 pending candidates, `summary_max_sentences`
                # appeared **three times**, with values "3-4 sentences", "三句话以内", and "两句话以内"
                # — three conflicting versions awaiting user decisions simultaneously. Another pair was directly opposed:
                # require plan approval before a report / output directly without a second confirmation.
                #
                # Root cause: deduplication uses `content_fingerprint` = {kind, value, workspace_id},
                # **excluding key**. It prevents identical repetition, not restating the same topic differently,
                # which is how users actually speak.
                #
                # ⚠ Why not add key to the fingerprint (the initial instinct, which was wrong):
                # the primary key of `memory_candidate_suppressions` **is the fingerprint**, so changing it
                # **invalidates every existing suppression**, letting rejected claims reappear that day.
                # This pitfall is already documented above `_candidate_content_fingerprint` in this file.
                # ⇒ Separate the concepts: fingerprint = "this content was rejected" (suppression identity, byte-stable);
                #    key = "which topic this is about" (queue identity). Consolidate during enqueueing.
                await db.execute(
                    """
                    UPDATE memory_candidates
                    SET status='rejected',
                        decision_kind='superseded_by_newer',
                        decided_by=?,
                        decided_at=?,
                        updated_at=?,
                        version=version+1,
                        key='',
                        value_json='null',
                        reason=NULL,
                        source_turn_id=NULL,
                        workspace_id=NULL,
                        content_fingerprint=NULL,
                        supersedes_json=NULL,
                        memory_type=NULL,
                        content_redacted_at=?
                    WHERE user_id=? AND status='pending' AND kind=? AND key=?
                      AND COALESCE(workspace_id,'')=COALESCE(?,'')
                    """,
                    (SUPERSEDE_ACTOR, now, now, now, user_id, kind, key, workspace_id),
                )
                await db.execute(
                    """
                    INSERT INTO memory_candidates(
                        id, user_id, kind, key, value_json, reason,
                        source_turn_id, created_at, updated_at, workspace_id,
                        content_fingerprint, memory_type, supersedes_json, origin
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        user_id,
                        kind,
                        key,
                        value_json,
                        reason,
                        source_turn_id,
                        now,
                        now,
                        workspace_id,
                        fingerprint,
                        memory_type,
                        _supersedes_json(supersedes),
                        origin,
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                await db.rollback()
                # Concurrent insertion of identical content won the unique-constraint race; equivalent to duplicate pending: a card exists.
                return None, ENQUEUE_DUPLICATE_PENDING
            return await _read_back(db, cid), ENQUEUE_CREATED

    async def enqueue_if_new(self, **kwargs: Any) -> MemoryCandidate | None:
        """只要候选、不关心**为什么**没入队的调用方用这个；参数与
        `enqueue_if_new_with_outcome` 完全相同（全部是关键字参数）。

        ★ 要对用户说话的调用方必须用 `enqueue_if_new_with_outcome`：None 有三种来由，
        其中两种（被抑制、被写入闸拒）意味着**没有卡**，不能再说「去确认一下」。
        """
        cand, _outcome = await self.enqueue_if_new_with_outcome(**kwargs)
        return cand

    async def list_users_with_stale_pending(
        self,
        *,
        max_age_days: int = 30,
    ) -> list[str]:
        """review-fix P1: auto_reject_stale 前查受影响用户（GC 清零后解 badge 用）。"""
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                # ★ Batch 1b allowlist: automatic expiry rejection also touches only background-distilled candidates.
                # Draft candidates are explicit user requests; inactivity must not automatically reject them.
                "SELECT DISTINCT user_id FROM memory_candidates WHERE status='pending' "
                "AND COALESCE(origin, 'dreaming_light') = 'dreaming_light' "
                "AND datetime(created_at) < datetime('now', ?)",
                (f"-{int(max_age_days)} days",),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def list_users_with_unwarned_expiring(
        self,
        *,
        warn_days: int = 23,
        max_age_days: int = 30,
    ) -> list[str]:
        """S2 C3: 查有「即将过期但还没预告过」pending 候选的 distinct user。

        窗口 age ∈ (warn_days, max_age_days)：age > warn_days（`created_at < now-warn_days`）
        且 age < max_age_days（`created_at > now-max_age_days`，还没被 GC auto-reject），
        且 `expiry_warned_at IS NULL`。上下界不重叠 auto_reject（age>max_age_days），一生一次。
        """
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT DISTINCT user_id FROM memory_candidates "
                "WHERE status='pending' AND expiry_warned_at IS NULL "
                "AND datetime(created_at) < datetime('now', ?) "
                "AND datetime(created_at) > datetime('now', ?)",
                (f"-{int(warn_days)} days", f"-{int(max_age_days)} days"),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def mark_expiry_warned(
        self,
        *,
        user_id: str,
        warn_days: int = 23,
        max_age_days: int = 30,
    ) -> int:
        """S2 C3: 给该 user 落在预告窗内、未标记的 pending 候选盖 expiry_warned_at=now.

        与 list_users_with_unwarned_expiring 同窗口——emit 成功后调，保证一生一次。
        """
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await _prepare_user_write(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "UPDATE memory_candidates SET expiry_warned_at=? "
                "WHERE user_id=? AND status='pending' AND expiry_warned_at IS NULL "
                "AND datetime(created_at) < datetime('now', ?) "
                "AND datetime(created_at) > datetime('now', ?)",
                (now, user_id, f"-{int(warn_days)} days", f"-{int(max_age_days)} days"),
            )
            await db.commit()
            return cur.rowcount or 0

    async def auto_reject_stale(
        self,
        *,
        max_age_days: int = 30,
        decided_by: str = "system_auto_reject",
    ) -> int:
        """V3 PRD-B Cron: auto-reject pending candidates older than max_age_days.

        Uses datetime() to normalize ISO 'T' vs sqlite ' ' (spike #19).
        Returns count of newly rejected rows.
        """
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                """
                SELECT user_id
                FROM memory_candidates
                WHERE (
                    status='pending'
                    AND datetime(created_at)<datetime('now', ?)
                ) OR (
                    decision_kind='user_rejected'
                    AND content_redacted_at IS NULL
                    AND datetime(decided_at)<datetime('now', ?)
                )
                UNION
                SELECT user_id
                FROM memory_candidate_suppressions
                WHERE datetime(expires_at)<=datetime('now')
                """,
                (
                    f"-{int(max_age_days)} days",
                    f"-{int(max_age_days)} days",
                ),
            ) as cursor,
        ):
            user_ids = [str(row[0]) for row in await cursor.fetchall()]
        rejected = 0
        for user_id in user_ids:
            now = _now()
            async with aiosqlite.connect(self.db_path) as db:
                await _prepare_user_write(
                    db,
                    user_id=user_id,
                )
                cur = await db.execute(
                    """
                    UPDATE memory_candidates
                    SET status='rejected',
                        decision_kind='system_expired',
                        key='',
                        value_json='null',
                        reason=NULL,
                        source_turn_id=NULL,
                        workspace_id=NULL,
                        content_fingerprint=NULL,
                        decided_by=?,
                        decided_at=?,
                        updated_at=?,
                        content_redacted_at=?,
                        version=version+1
                    WHERE user_id=?
                      AND status='pending'
                      -- ★ 批 1b 白名单：真正执行驳回的这条 UPDATE 也要带上它。
                      -- 只在上面那句 SELECT 上过滤是不够的：那只是挑用户，
                      -- 这条才是动数据的地方（少一处就等于没过滤）。
                      AND COALESCE(origin, 'dreaming_light') = 'dreaming_light'
                      AND datetime(created_at) < datetime('now', ?)
                    """,
                    (
                        decided_by,
                        now,
                        now,
                        now,
                        user_id,
                        f"-{int(max_age_days)} days",
                    ),
                )
                await db.execute(
                    """
                    UPDATE memory_candidates
                    SET key='',
                        value_json='null',
                        reason=NULL,
                        source_turn_id=NULL,
                        workspace_id=NULL,
                        content_fingerprint=NULL,
                        updated_at=?,
                        content_redacted_at=?,
                        version=version+1
                    WHERE user_id=?
                      AND decision_kind='user_rejected'
                      AND content_redacted_at IS NULL
                      AND datetime(decided_at)<datetime('now', ?)
                    """,
                    (
                        now,
                        now,
                        user_id,
                        f"-{int(max_age_days)} days",
                    ),
                )
                await db.execute(
                    """
                    DELETE FROM memory_candidate_suppressions
                    WHERE user_id=?
                      AND datetime(expires_at)<=datetime('now')
                    """,
                    (user_id,),
                )
                await db.commit()
                rejected += cur.rowcount or 0
        return rejected

    async def get(self, cid: str) -> MemoryCandidate | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM memory_candidates WHERE id=?",
                (cid,),
            ) as cur:
                row = await cur.fetchone()
            if row and await is_memory_read_hidden(
                db,
                user_id=str(row["user_id"]),
            ):
                return None
        return _row(row) if row else None

    async def set_decision(
        self,
        *,
        cid: str,
        status: CandidateStatus,
        decided_by: str,
    ) -> MemoryCandidate | None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """
                SELECT status, user_id, workspace_id, kind,
                       content_fingerprint, origin
                FROM memory_candidates
                WHERE id=?
                """,
                (cid,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await db.rollback()
                return None
            await ensure_memory_write_allowed(
                db,
                user_id=str(row["user_id"]),
            )
            if row["status"] != "pending":
                await db.rollback()
            else:
                # ★ `system_triaged` is not an optional third category: reusing `user_rejected` in batch cleanup scripts
                # would write ~35 **decisions the user never made** into
                # `memory_candidate_suppressions` (30 days) and attribute them to the user in the audit log.
                # `system_expired` would also be false (these candidates did not expire).
                # Like `system_expired`, do not write suppressions: cleanup is an operational action, not a user decision.
                if status == "approved":
                    decision_kind = "user_approved"
                elif decided_by == REDRAFT_ACTOR:
                    # ★ Track B batch 1b: "Edit / redraft" **is not rejection**.
                    # Reusing `user_rejected` records "I want to rephrase this" as "I do not want this",
                    # giving the audit entry the opposite meaning. Drafts already skip suppressions, but their **semantics** must still stay distinct:
                    # if a future origin is assigned to the pool that writes suppressions, a user's "Edit"
                    # would block their own request for 30 days.
                    decision_kind = "feedback_redrafted"
                elif decided_by == "system_auto_reject":
                    decision_kind = "system_expired"
                elif decided_by == SYSTEM_TRIAGE_ACTOR:
                    decision_kind = "system_triaged"
                else:
                    decision_kind = "user_rejected"
                redact = status == "approved" or decision_kind in (
                    "system_expired",
                    "system_triaged",
                    # Redact redrafts too: the new version supersedes the old body; retaining it only leaves another plaintext copy.
                    "feedback_redrafted",
                )
                _row_origin = str(_optional_row_value(row, "origin") or ORIGIN_DREAMING)
                # ★ Only the background-distillation pool writes suppressions. Drafts are explicit user requests:
                # "Edit / redraft" must not become a 30-day exact-content suppression, or the next statement of the same request
                # would be blocked by the user's own previous "Edit" (brief pitfall 3).
                if (
                    decision_kind in ("user_approved", "user_rejected")
                    and _row_origin in _SUPPRESSION_ORIGINS
                    and row["content_fingerprint"] is not None
                ):
                    await _upsert_candidate_suppression(
                        db,
                        user_id=str(row["user_id"]),
                        origin=_row_origin,
                        workspace_id=(str(row["workspace_id"]) if row["workspace_id"] is not None else None),
                        kind=str(row["kind"]),
                        fingerprint=str(row["content_fingerprint"]),
                        decision_kind=decision_kind,
                        created_at=now,
                        expires_at=_after_days(30),
                    )
                await db.execute(
                    """
                    UPDATE memory_candidates
                    SET status=?,
                        decision_kind=?,
                        decided_by=?,
                        decided_at=?,
                        updated_at=?,
                        version=version+1,
                        key=CASE WHEN ? THEN '' ELSE key END,
                        value_json=CASE WHEN ? THEN 'null' ELSE value_json END,
                        reason=CASE WHEN ? THEN NULL ELSE reason END,
                        source_turn_id=CASE WHEN ? THEN NULL ELSE source_turn_id END,
                        workspace_id=CASE WHEN ? THEN NULL ELSE workspace_id END,
                        content_fingerprint=CASE
                            WHEN ? THEN NULL
                            ELSE content_fingerprint
                        END,
                        supersedes_json=CASE
                            WHEN ? THEN NULL
                            ELSE supersedes_json
                        END,
                        memory_type=CASE
                            WHEN ? THEN NULL
                            ELSE memory_type
                        END,
                        content_redacted_at=CASE WHEN ? THEN ? ELSE content_redacted_at END
                    WHERE id=?
                    """,
                    (
                        status,
                        decision_kind,
                        decided_by,
                        now,
                        now,
                        redact,
                        redact,
                        redact,
                        redact,
                        redact,
                        redact,
                        redact,
                        redact,
                        redact,
                        now,
                        cid,
                    ),
                )
                await db.commit()
        return await self.get(cid)


def _candidate_value_json(
    *,
    kind: str,
    key: str,
    value: Any,
) -> str:
    if kind not in (
        "preference",
        "constraint",
        "datarc_binding",
        "note",
    ):
        raise ValueError(f"invalid kind: {kind!r}")
    if len(key) > 128:
        raise ValueError(f"key too long: {len(key)} > 128")
    value_json = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    if len(value_json) > 2000:
        raise ValueError(f"value too large: {len(value_json)} > 2000")
    return value_json


#: ★ `supersedes_json` **must** enter the fingerprint: before enabling the gate, Dreaming had already enqueued "净利率口径=X"
#: (`supersedes_json=NULL`; pending deduplication has no time window, so it remains indefinitely). After enabling the gate, the model finally
#: adds `supersedes:['mem_abc']`, but the value is identical ⇒ fingerprint match ⇒ `return None`,
#: **permanently discarding the supersession declaration**; approving that stale candidate cannot supersede anything.
#: This is the release path of the bug that motivated this sprint. The same applies to the 30-day suppression written on approve.
def _candidate_content_fingerprint(
    *,
    secret: bytes,
    kind: str,
    value_json: str,
    workspace_id: str | None,
    supersedes_json: str | None = None,
) -> str:
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError:
        value = value_json
    payload: dict[str, Any] = {
        "kind": kind,
        "value": value,
        "workspace_id": workspace_id,
    }
    # ⚠ Add this key **only when nonempty**: adding it unconditionally changes **every** existing fingerprint,
    # while `memory_candidate_suppressions` stores the old fingerprints ⇒ all 30-day suppressions become invalid,
    # letting rejected claims reappear that day. Rows with empty supersedes must retain byte-identical canonical strings.
    if supersedes_json:
        payload["supersedes"] = supersedes_json
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(
        secret,
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _optional_row_value(row: aiosqlite.Row, name: str) -> object:
    """老库可能还没这一列（`_MIGRATION_COLUMNS` 补列前的启动窗口）。"""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _parse_supersedes(raw: object) -> tuple[str, ...]:
    """`supersedes_json` → 去重后的 id 元组。坏 JSON / 非列表一律当没写。"""
    if not raw:
        return ()
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in parsed if str(item or "").strip()))


def _supersedes_json(values: object) -> str | None:
    if isinstance(values, str):
        values = [values]
    ids = [str(v).strip() for v in (values or []) if str(v or "").strip()]
    return json.dumps(list(dict.fromkeys(ids)), ensure_ascii=False) if ids else None


def _candidate_scope_key(workspace_id: str | None) -> str:
    return workspace_id or "__no_space__"


def _after_days(days: int) -> str:
    return (datetime.now(tz=UTC) + timedelta(days=days)).isoformat(
        timespec="seconds",
    )


async def _upsert_candidate_suppression(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    origin: str,
    workspace_id: str | None,
    kind: str,
    fingerprint: str,
    decision_kind: str,
    created_at: str,
    expires_at: str,
) -> None:
    await db.execute(
        """
        INSERT INTO memory_candidate_suppressions(
            user_id, source_scope_key, kind, fingerprint,
            decision_kind, created_at, expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, source_scope_key, kind, fingerprint)
        DO UPDATE SET
            decision_kind=excluded.decision_kind,
            created_at=excluded.created_at,
            expires_at=excluded.expires_at
        """,
        (
            user_id,
            _suppression_scope_key(origin, workspace_id),
            kind,
            fingerprint,
            decision_kind,
            created_at,
            expires_at,
        ),
    )


async def _backfill_candidate_fingerprints(
    db: aiosqlite.Connection,
    *,
    secret: bytes,
) -> None:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """
        SELECT id, kind, value_json, workspace_id, supersedes_json
        FROM memory_candidates
        WHERE content_fingerprint IS NULL
          AND content_redacted_at IS NULL
          AND NOT (key='' AND value_json='null')
        """,
    ) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        await db.execute(
            """
            UPDATE memory_candidates
            SET content_fingerprint=?
            WHERE id=? AND content_fingerprint IS NULL
            """,
            (
                _candidate_content_fingerprint(
                    secret=secret,
                    kind=str(row["kind"]),
                    value_json=str(row["value_json"]),
                    workspace_id=(str(row["workspace_id"]) if row["workspace_id"] is not None else None),
                    # ⚠ Include this in backfill too: otherwise the next startup recomputes rows with supersedes
                    # as fingerprints **without** it, immediately restoring the collision just fixed.
                    supersedes_json=_optional_row_value(row, "supersedes_json"),
                ),
                row["id"],
            ),
        )


async def _backfill_candidate_suppressions(
    db: aiosqlite.Connection,
) -> None:
    """把「抑制表出现之前用户已经做过的裁决」补成抑制行（`initialize` 每次启动都跑）。

    ⚠⚠ 这里的 `source_scope_key` 必须与 `_suppression_scope_key` **逐字一致**。
    初版写的是裸的 `COALESCE(workspace_id,'__no_space__')`；后来抑制键加了 origin 前缀
    （`{origin}:{scope}`，两池不串扰），读侧与 `set_decision` 的写侧都改了，**唯独漏了这里**
    ⇒ 回填写进去的每一行都读不出来：用户在抑制表之前点过的「不要」不被尊重，那条建议照旧再提，
    而且是**静默**的。2026-09-15 在 116 上发现：同一指纹既有裸键行也有带前缀行，裸键那些永远匹配不上。
    「改一处 = 没改」；这个函数当时一个测试都没有，所以没人红。

    ⚠ 只补**会写抑制的那一池**（`_SUPPRESSION_ORIGINS`，与 `set_decision` 同一个常量，不在 SQL 里另抄一份）。
    起草档（`feedback_draft`）按设计**不写抑制**——用户的「改一下」不该变成对自己诉求的 30 天封杀；
    加前缀却不加这个过滤，等于把设计上明令不做的事补了回去。
    """
    origins = sorted(_SUPPRESSION_ORIGINS)
    if not origins:  # pragma: no cover - An empty constant must not silently backfill a set of suppressions.
        return
    placeholders = ",".join("?" for _ in origins)
    await db.execute(
        f"""
        INSERT INTO memory_candidate_suppressions(
            user_id, source_scope_key, kind, fingerprint,
            decision_kind, created_at, expires_at
        )
        SELECT
            user_id,
            COALESCE(origin, '{ORIGIN_DREAMING}') || ':' || COALESCE(workspace_id, '__no_space__'),
            kind,
            content_fingerprint,
            CASE
                WHEN MAX(CASE WHEN status='approved' THEN 1 ELSE 0 END)=1
                    THEN 'user_approved'
                ELSE 'user_rejected'
            END,
            MAX(COALESCE(decided_at, created_at)),
            datetime(MAX(COALESCE(decided_at, created_at)), '+30 days')
        FROM memory_candidates
        WHERE content_fingerprint IS NOT NULL
          AND decision_kind IN ('user_approved', 'user_rejected')
          AND COALESCE(origin, '{ORIGIN_DREAMING}') IN ({placeholders})
        GROUP BY
            user_id,
            COALESCE(origin, '{ORIGIN_DREAMING}'),
            COALESCE(workspace_id, '__no_space__'),
            kind,
            content_fingerprint
        HAVING datetime(MAX(COALESCE(decided_at, created_at)), '+30 days')
               > datetime('now')
        ON CONFLICT(user_id, source_scope_key, kind, fingerprint)
        DO UPDATE SET
            decision_kind=excluded.decision_kind,
            created_at=excluded.created_at,
            expires_at=excluded.expires_at
        """,
        origins,
    )


async def _normalize_candidate_decisions(
    db: aiosqlite.Connection,
) -> None:
    await db.execute(
        """
        UPDATE memory_candidates
        SET decision_kind=CASE
            WHEN status='approved' THEN 'user_approved'
            WHEN duplicate_of_candidate_id IS NOT NULL
                OR COALESCE(decided_by, '')='system_migration'
                THEN 'migration_duplicate'
            WHEN COALESCE(decided_by, '')='system_auto_reject'
                THEN 'system_expired'
            ELSE 'user_rejected'
        END
        WHERE decision_kind IS NULL
          AND status IN ('approved', 'rejected')
        """,
    )


async def _redact_nonrecoverable_candidates(
    db: aiosqlite.Connection,
) -> None:
    now = _now()
    await db.execute(
        """
        UPDATE memory_candidates
        SET key='',
            value_json='null',
            reason=NULL,
            source_turn_id=NULL,
            workspace_id=NULL,
            content_fingerprint=NULL,
            -- 两个新列同进同出：只清 supersedes_json 会让「已脱敏」的行还留着
            -- 内容派生出来的 memory_type，两列待遇不一致。
            supersedes_json=NULL,
            memory_type=NULL,
            content_redacted_at=?,
            updated_at=?,
            version=version+1
        WHERE content_redacted_at IS NULL
          AND (
            decision_kind='system_expired'
            OR status='approved'
            OR (
              decision_kind='user_rejected'
              AND datetime(COALESCE(decided_at, created_at))
                  < datetime('now', '-30 days')
            )
          )
        """,
        (now, now),
    )


async def _canonicalize_pending_duplicates(
    db: aiosqlite.Connection,
) -> None:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """
        SELECT user_id, content_fingerprint
        FROM memory_candidates
        WHERE status='pending'
          AND content_fingerprint IS NOT NULL
          AND duplicate_of_candidate_id IS NULL
        GROUP BY user_id, content_fingerprint
        HAVING COUNT(*) > 1
        """,
    ) as cursor:
        duplicate_groups = await cursor.fetchall()
    now = _now()
    for group in duplicate_groups:
        async with db.execute(
            """
            SELECT id
            FROM memory_candidates
            WHERE user_id=? AND content_fingerprint=?
              AND status='pending'
              AND duplicate_of_candidate_id IS NULL
            ORDER BY created_at, id
            """,
            (group["user_id"], group["content_fingerprint"]),
        ) as cursor:
            rows = await cursor.fetchall()
        canonical_id = str(rows[0]["id"])
        await db.execute(
            """
            UPDATE memory_candidates
            SET duplicate_count=?
            WHERE id=?
            """,
            (len(rows), canonical_id),
        )
        for duplicate in rows[1:]:
            await db.execute(
                """
                UPDATE memory_candidates
                SET status='rejected',
                    decision_kind='migration_duplicate',
                    duplicate_of_candidate_id=?,
                    key='',
                    value_json='null',
                    reason=NULL,
                    source_turn_id=NULL,
                    workspace_id=NULL,
                    decided_by='system_migration',
                    decided_at=?,
                    updated_at=?,
                    content_redacted_at=?,
                    version=version+1
                WHERE id=? AND status='pending'
                """,
                (
                    canonical_id,
                    now,
                    now,
                    now,
                    duplicate["id"],
                ),
            )


async def _prepare_user_write(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> None:
    await db.execute("PRAGMA secure_delete=ON")
    await db.execute("BEGIN IMMEDIATE")
    await ensure_memory_write_allowed(
        db,
        user_id=user_id,
    )


def _row(row: aiosqlite.Row) -> MemoryCandidate:
    # initialize() migrations ensure workspace_id exists; use None for old rows read through an unmigrated bare connection
    # (sqlite3.Row raises IndexError for a missing column; `in row` iterates values rather than keys, so it cannot be used).
    try:
        workspace_id = row["workspace_id"]
    except (IndexError, KeyError):
        workspace_id = None
    keys = set(row.keys())
    return MemoryCandidate(
        id=row["id"],
        user_id=row["user_id"],
        subject_type=row["subject_type"],
        kind=row["kind"],
        key=row["key"],
        value=json.loads(row["value_json"] or "null"),
        reason=row["reason"],
        source_turn_id=row["source_turn_id"],
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        workspace_id=workspace_id,
        version=int(row["version"]) if "version" in keys else 1,
        decision_kind=row["decision_kind"] if "decision_kind" in keys else None,
        approved_resource_type=row["approved_resource_type"] if "approved_resource_type" in keys else None,
        approved_resource_id=row["approved_resource_id"] if "approved_resource_id" in keys else None,
        memory_type=row["memory_type"] if "memory_type" in keys else None,
        supersedes=_parse_supersedes(row["supersedes_json"] if "supersedes_json" in keys else None),
        origin=str(row["origin"] or ORIGIN_DREAMING) if "origin" in keys else ORIGIN_DREAMING,
    )


async def _read_back(db: aiosqlite.Connection, cid: str) -> MemoryCandidate:
    """刚写完的那一行，**从库里读回来**再走 `_row`。

    ★★ 为什么不手写构造：`MemoryCandidate` 原本有**三条装配路径**
    （`enqueue` / `enqueue_if_new` 各自手拼 + `_row`），加一个字段要三处都追，
    漏掉任何一处的失败**是静默的** —— DB 那行是对的，只有某一条读路拿到默认值。
    两天里这条根因产出过三个真 bug：
      · `origin` 没进 dataclass ⇒「改一下」端点 400；
      · `origin` 没进两个构造点 ⇒ 点完「改一下」按钮当场消失（116 真机抓到）；
      · 手拼返回的是**入参** value，而库里存的是 `json.dumps(default=str)` 之后的
        ⇒ 传 `date` 对象时，enqueue 返回 `date(2026,9,10)`、读回来是 `'2026-09-10'`
        （实测；今天候选值都是字符串所以没暴露，是埋着的雷）。

    现在只剩**一条**装配路径。加列时只改 `_row`。
    ⚠ 代价是每次入队多一次 SELECT —— 入队不是热路径（每轮至多几条，且有冷却+日上限）。
    ⚠ 走 `_row` 而不是 `store.get()`：后者带 `is_memory_read_hidden` 检查，
    而我们刚写完、明知它在；且这里要留在**同一个事务**里。
    """
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM memory_candidates WHERE id=?", (cid,)) as cur:
        row = await cur.fetchone()
    if row is None:  # pragma: no cover - Just INSERTed in this transaction; failure to read it indicates a real problem.
        raise RuntimeError(f"candidate vanished right after insert: {cid}")
    return _row(row)


# ─────────────────────────────────────────────────────────
# Dreaming Light — LLM-driven candidate generation
# ─────────────────────────────────────────────────────────
from dataclasses import dataclass as _dc

try:
    from bowhead_router import LLMRequest, Message
except ImportError:
    LLMRequest = None  # type: ignore[assignment]
    Message = None  # type: ignore[assignment]


_DREAMING_SYSTEM_TEMPLATE = """你是 Bowhead 的 Dreaming Light 观察员。你看完了一次数据分析的对话，需要提议 0-3 条**候选用户偏好**记录到长期记忆。

候选字段：
- kind: preference | constraint | datarc_binding | note
- key:  短标识（英文/拼音，snake_case）；kind=note 时可空字符串
- value: 值（字符串 / 对象）。kind=note 时是一句完整的中文/英文 bullet text (≤200 char)
- reason: 一句话解释为什么要记下来
<<CANONICAL_FIELDS>>
kind 选择 (V3.5 PRD-C 加 'note'):
- 结构化 (preference / constraint / datarc_binding): key=value 形式, 适合明确字段映射
- note: 自由文本 bullet, 适合"用户老板叫张总" / "周报必带 SEC cross-check" 这类难塞 key=value 的偏好

判定规则：
1. 非常严格。只在用户**明确表达偏好**时才记（不是一次性用法）。例：用户说"我更喜欢看环比而不是同比" → 记。
2. 不记一次性查询（"今年 GMV"）。
3. 不记敏感数据（手机号、邮箱、金额）。
4. 不记 LLM 自己的假设。
5. 如果输入里带了「已有记忆与待确认项」：**不要重复提议其中已覆盖的内容**（含换措辞的同义重复）；
   如果本次对话与已有记忆矛盾（用户变心），提议**更新**——输出新值，<<CANONICAL_SUPERSEDE_RULE>><<CANONICAL_KEY_RULE>>

输出 JSON：
{"candidates": [
<<CANONICAL_EXAMPLE>>
]}

如果没有值得记的 → {"candidates": []}。只输出 JSON。"""


# ── Memory redesign batch 1: the `supersedes` slot (tech-design §4.2 a) ───────────────────────
# With the gate off, the three sections below are empty/original strings ⇒ the distillation prompt is **byte-identical** to the current one.
_LEGACY_SUPERSEDE_RULE = "reason 里注明覆盖了哪条旧记忆。"
_CANONICAL_SUPERSEDE_RULE = (
    "并把被取代那条的 `mem_xxx` 填进 `supersedes`。\n"
    "   **只写在 reason 里没有用**：reason 是给人看的说明，系统不会据此取代任何东西；\n"
    "   真正生效的是 `supersedes` 这个字段。"
)
_CANONICAL_KEY_RULE = "\n6. 同一个主题请复用「已有记忆」里那条的 key，别新起一个——key 就是主题，换 key 等于新开一条。"
_CANONICAL_FIELDS = (
    "- memory_type: procedural（呈现/格式：单位、图表类型、排版）"
    "| semantic（口径/定义：指标定义、必含板块、术语）\n"
    "- supersedes: 数组，要被这条**取代**的旧记忆 id"
    "（就是「已有记忆」里方括号中的 mem_xxx）；不取代任何旧条时给 []\n"
)
_LEGACY_EXAMPLE = """  {"kind": "preference", "key": "prefer_qoq", "value": "always show QoQ in trend reports",
   "reason": "user said `我更喜欢看环比` in this turn"},
  {"kind": "note", "key": "", "value": "用户老板叫张总, 周报里别提 GMV 提交易额",
   "reason": "user mentioned boss naming + reporting style"}"""
_CANONICAL_EXAMPLE = """  {"kind": "preference", "key": "prefer_qoq", "value": "always show QoQ in trend reports",
   "reason": "user said `我更喜欢看环比` in this turn", "memory_type": "semantic", "supersedes": []},
  {"kind": "note", "key": "boss_naming", "value": "用户老板叫张总, 周报里别提 GMV 提交易额",
   "reason": "user mentioned boss naming + reporting style", "memory_type": "procedural",
   "supersedes": ["mem_1bf370c9c86a"]}"""


def build_dreaming_system(*, canonical: bool) -> str:
    """蒸馏 system prompt。`canonical=True` 才开 `supersedes` / `memory_type` 两个槽。

    ★ 槽和 digest 必须**同时**在：模型只有在「已有记忆」里看得见 `[mem_xxx]` 才填得出
    `supersedes`（id 由 `turn_runner._existing_memory_digest` 随同一个闸注入）。
    只改 prompt 不给 id = 让模型编一个 id 出来。
    """
    # Use replace instead of str.format: JSON examples in the template contain literal `{` and `}`
    # that format treats as placeholders (observed failure: KeyError: '"candidates"').
    out = _DREAMING_SYSTEM_TEMPLATE
    for token, value in (
        ("<<CANONICAL_FIELDS>>", _CANONICAL_FIELDS if canonical else ""),
        ("<<CANONICAL_SUPERSEDE_RULE>>", _CANONICAL_SUPERSEDE_RULE if canonical else _LEGACY_SUPERSEDE_RULE),
        ("<<CANONICAL_KEY_RULE>>", _CANONICAL_KEY_RULE if canonical else ""),
        ("<<CANONICAL_EXAMPLE>>", _CANONICAL_EXAMPLE if canonical else _LEGACY_EXAMPLE),
    ):
        out = out.replace(token, value)
    return out


def _memory_storage_ready() -> bool:
    """记忆重做总闸。`bowhead_memory` 不硬依赖 `bowhead_core`（同 vector_client 的
    惰性 import 写法）——拿不到就当闸关，绝不在这里复制第二份 flag 读法。"""
    try:
        from bowhead_core.memory_readiness import memory_storage_ready
    except ImportError:
        return False
    return memory_storage_ready()


#: Backward compatibility: the final string with the gate off (existing unit tests import it by name).
DREAMING_SYSTEM = build_dreaming_system(canonical=False)


@_dc
class DreamObservation:
    turn_id: str
    user_id: str
    user_text: str
    final_answer: str
    intent: str
    tool_chain: list[str]
    # memory-revive S1: digest of existing memories + pending candidates (assembled by the caller; empty string = none).
    # Semantic deduplication must happen during distillation; exact value_json matching cannot stop LLM rephrasing (discovery §6.2).
    existing_memory: str = ""
    # memory-revive S1: source space (scope reserved).
    workspace_id: str | None = None
    # memory-i18n: user-configured output language ("en-US"/"zh-CN"; empty string = unknown → no language constraint,
    # preserving current behavior). Without this, distillation follows the conversation language, giving English-language users in Chinese-data environments
    # Chinese candidates (116 observation: the reason for the Chinese-language turn on 8-16 was Chinese).
    output_language: str = ""


# Settings key for memory page pause toggle (V3 PRD-B)
SETTINGS_KEY_DREAMING_PAUSED = "memory_dreaming_paused"


@dataclass
class PauseStateCache:
    """V3 PRD-B: in-memory cache for user "dreaming paused" toggle.

    Reads `memory_dreaming_paused` from UserSettingsStore. Caches per-user
    for `ttl_seconds` to avoid hammering sqlite on every turn end.

    单 worker 已 verify (Dockerfile.app 默认 1 worker, 无 --workers flag).
    Multi-worker 时各 worker 各自缓存, 真值在 sqlite, 最差 ttl_seconds 不一致.
    """

    settings_store: Any  # UserSettingsStore
    ttl_seconds: float = 10.0
    _cache: dict[str, tuple[bool, float]] = field(default_factory=dict)

    async def is_paused(self, user_id: str) -> bool:
        if not user_id:
            return False
        now_t = time.monotonic()
        cached = self._cache.get(user_id)
        if cached is not None and (now_t - cached[1]) < self.ttl_seconds:
            return cached[0]
        paused = await self._fetch(user_id)
        self._cache[user_id] = (paused, now_t)
        return paused

    def invalidate(self, user_id: str) -> None:
        """Call after user toggles pause via API to evict stale cache entry."""
        self._cache.pop(user_id, None)

    async def _fetch(self, user_id: str) -> bool:
        if self.settings_store is None:
            return False
        try:
            raw = await self.settings_store.get(user_id, SETTINGS_KEY_DREAMING_PAUSED)
        except Exception as exc:
            # DB locked / store mid-init / etc. Default to NOT paused so
            # DreamingLight degrades gracefully (Dreaming continues, user
            # can re-pause once store recovers).
            logger.warning(
                "pause_state_cache.fetch_failed user=%s err=%s",
                user_id,
                exc,
            )
            return False
        if raw is None:
            return False
        return str(raw).strip().lower() in ("1", "true", "yes", "on")


@_dc
class DreamingLight:
    router: Any  # bowhead_router.Router
    candidate_store: MemoryCandidateStore
    llm_agent_type: str = "echo"  # Model tiers: Dreaming Light fire-and-forget memory candidates → fast tier.
    max_output_tokens: int = 400
    pause_cache: PauseStateCache | None = None  # V3 PRD-B: skip if user paused
    # review-fix F5: turn completion is shared across all channels (completed turns from scheduled routines/Feishu
    # trigger it too); TurnRequest has no channel field to filter (S2 accounting).
    # Control cost with per-user cooldown + daily cap. In-memory state (single-worker deployment verified,
    # the same assumption as the PauseStateCache docstring); reset on restart is acceptable (cost guardrail, not quota).
    min_interval_seconds: float = 60.0
    daily_cap: int = 40
    _last_observe_at: dict[str, float] = field(default_factory=dict)
    _daily_counts: dict[str, tuple[str, int]] = field(default_factory=dict)

    def _rate_limited(self, user_id: str) -> bool:
        now_t = time.monotonic()
        last = self._last_observe_at.get(user_id)
        if last is not None and (now_t - last) < self.min_interval_seconds:
            return True
        today = datetime.now(tz=UTC).date().isoformat()
        day, count = self._daily_counts.get(user_id, (today, 0))
        if day == today and count >= self.daily_cap:
            return True
        self._last_observe_at[user_id] = now_t
        self._daily_counts[user_id] = (today, count + 1) if day == today else (today, 1)
        return False

    async def observe(self, obs: DreamObservation, *, router: Any | None = None) -> list[MemoryCandidate]:
        """LLM 看一次 turn，提议候选并 enqueue。返回新创建的候选列表.

        ``router``：本 turn 生效的 Router（credential-readiness D2.1——BYO 团队由
        turn_runner 传 ``request.team_router``）。缺省回落构造期的 ``self.router``；
        prod 下构造期是 RefusalAdapter，漏传会被 D1 拦住并归因 reason=dreaming。
        """
        if LLMRequest is None:
            return []
        # ★★ These two logs are the **only trace** that this turn produced nothing; they must be visible at INFO.
        # Observed cost on 2026-09-16: the same account on 116 was used concurrently in another session; two of three turns
        # occurred within 60 seconds of someone else's turn and were silently skipped. Debug logs print nothing in production (BOWHEAD_LOG_LEVEL=INFO),
        # making "distillation did not run" indistinguishable from "distillation ran but proposed nothing" in the logs;
        # diagnosing this took an entire round of investigation.
        # ⚠ Rate limiting is per **user**, not per session: someone else's turns on the same account still consume the allowance,
        # so investigation must examine **all turns for this user**, not just the intervals between one's own turns.
        if self.pause_cache is not None and await self.pause_cache.is_paused(obs.user_id):
            logger.info("dreaming.skipped user=%s reason=paused", obs.user_id)
            return []
        if self._rate_limited(obs.user_id):
            logger.info(
                "dreaming.skipped user=%s reason=rate_limited min_interval_s=%s daily_cap=%s",
                obs.user_id,
                self.min_interval_seconds,
                self.daily_cap,
            )
            return []
        prompt = (
            f"turn_id: {obs.turn_id}\n"
            f"intent: {obs.intent}\n"
            f"tool_chain: {obs.tool_chain}\n"
            f"user 说: {obs.user_text[:500]}\n"
            f"最终答复: {obs.final_answer[:500]}\n"
        )
        if obs.existing_memory.strip():
            prompt += f"\n已有记忆与待确认项（判定规则 5：勿重复提议，矛盾时提议更新）:\n{obs.existing_memory[:1500]}\n"
        # memory-i18n: candidates are user-visible text (confirmation cards / memory page); follow the user's language setting.
        # Write the instruction in the target language (the spawn-language lesson: a Chinese instruction to "use English"
        # inside an all-Chinese prompt does not work for flash-tier models); add no line for unknown languages, preserving current behavior.
        if obs.output_language == "en-US":
            prompt += (
                "\nOutput language: write every candidate's value and reason in English"
                " (the user's configured output language). Keep proper nouns"
                " (people/brand/system names) as-is.\n"
            )
        elif obs.output_language == "zh-CN":
            prompt += "\n输出语言：所有候选的 value 与 reason 一律用中文写（用户设置的输出语言）；专有名词（人名/品牌/系统名）保留原文。\n"
        req = LLMRequest(
            model="",
            messages=[Message(role="user", content=prompt)],
            system=build_dreaming_system(canonical=_memory_storage_ready()),
            temperature=0.3,
            max_output_tokens=self.max_output_tokens,
            trace_id=f"dream:{obs.turn_id}",
        )
        try:
            # credit-billing gap 7: this call previously never reached the ledger (the outstanding "S2 accounting" comment in this file
            # refers to it). Set reason="dreaming" to distinguish its usage from user-request-driven turn usage:
            # deciding whether users should pay for spontaneous system memory distillation requires this measurement first.
            from bowhead_router.usage import usage_context

            with usage_context(
                turn_id=obs.turn_id,
                user_id=obs.user_id,
                workspace_id=obs.workspace_id,
                reason="dreaming",
            ):
                resp = await (router or self.router).invoke(self.llm_agent_type, req)
        except Exception as e:
            logger.warning("dreaming.llm_failed turn=%s err=%s", obs.turn_id, e)
            return []
        data = _parse_json(resp.content)
        if not data:
            return []
        cands = data.get("candidates") or []
        if not isinstance(cands, list):
            return []

        created: list[MemoryCandidate] = []
        for c in cands:
            if not isinstance(c, dict):
                continue
            kind = str(c.get("kind") or "").strip()
            key = str(c.get("key") or "").strip()
            value = c.get("value")
            # V3.5 PRD-C: 'note' kind accepted (dreaming proposes NL bullet text).
            # 'note' kind allows empty key (note has content as value not k=v shape).
            if kind not in (
                "preference",
                "constraint",
                "datarc_binding",
                "note",
            ):
                continue
            if value is None:
                continue
            if kind != "note" and not key:
                # Structured kinds require a key; only note kind permits empty
                continue
            # memory-revive S1: enqueue_if_new (bare enqueue repeats the same preference every turn;
            # deduplication includes rejected/approved suppression: rejected content must not reappear unchanged, discovery §6.2).
            try:
                cand = await self.candidate_store.enqueue_if_new(
                    user_id=obs.user_id,
                    kind=kind,
                    key=key,
                    value=value,
                    reason=str(c.get("reason") or "")[:400] or None,
                    source_turn_id=obs.turn_id,
                    workspace_id=obs.workspace_id,
                    # Memory redesign batch 1: persist type and supersession declarations exactly as given by the model.
                    # **Adjudication happens on the write side** (approve sets confidence by writer identity and uses the decision table
                    # to determine whether replacement is allowed). Accepting a declaration does not endorse it.
                    memory_type=(str(c.get("memory_type") or "").strip().lower() or None),
                    supersedes=c.get("supersedes") or (),
                )
            except ValueError as ve:
                # review-fix F8: skip an over-limit candidate without disrupting the others in the batch.
                logger.warning("dreaming.candidate_rejected turn=%s err=%s", obs.turn_id, ve)
                continue
            if cand is not None:
                created.append(cand)
        logger.info(
            "dreaming.observed turn=%s proposed=%d created=%d",
            obs.turn_id,
            len(cands),
            len(created),
        )
        return created


def _write_gate_allows(*, kind: str, key: str, value: Any, turn_id: str) -> bool:
    """记忆重做 批2：候选入队前过一次 write gate。

    `bowhead_memory` 不硬依赖 `bowhead_core`（同 `vector_client` 的惰性 import 写法）
    —— 拿不到就当闸关放行，绝不在这里复制第二份词表。

    ★ 取文本必须用 `comparable_text` + `combined_text`，与 approve 那条路**逐字同源**：
    初版这里用 `str(value)`，dict 值走 Python repr（单引号）骗过了密钥扫描
    ⇒ 入队放行、approve 才炸，用户拿到一个无从下手的 422。
    「词表只有一份」不够，**取文本的方式也必须只有一份**。

    ⚠ 运维 kind（`datarc_binding`）不过闸：那是机器状态不是偏好，拿偏好词表判它，
    一个带 token 的绑定会被当成「含密钥」静默丢掉（批1 已定「闸不许碰运维行」）。
    """
    if kind == "datarc_binding":
        return True
    try:
        from bowhead_core.memory_write_gate import check
        from bowhead_core.memory_write_policy import comparable_text
    except ImportError:
        # An unavailable gate and a disabled gate look identical externally; leave a trace, or a broken
        # bowhead_core import silently leaves the gate open.
        logger.warning("memory_candidate.gate_unavailable kind=%s (bowhead_core import failed)", kind)
        return True
    # Candidates always originate from a conversation turn, so attribution uses turn_id; candidates without turn_id should never be enqueued.
    verdict = check(
        value_text=comparable_text(json.dumps(value, ensure_ascii=False)),
        has_turn_id=bool(turn_id),
        actor_type=None,
        key=key,
    )
    if not verdict.allowed:
        # ★ Record **which gate** refused: during rollout, the key question is "how much are we dropping, and under which rule?",
        # and enqueueing has the highest volume. The first version returned only bool, losing the refusal reason immediately.
        logger.info("memory_candidate.gate_refused kind=%s reason=%s", kind, verdict.reason)
    return verdict.allowed


def _parse_json(content: str) -> dict[str, Any] | None:
    c = content.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", c, re.DOTALL)
    if m:
        c = m.group(1)
    brace = c.find("{")
    if brace > 0:
        c = c[brace:]
    try:
        obj = json.loads(c)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
