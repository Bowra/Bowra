"""B 线批 2a′：团队反馈候选（建议收件箱）store。

设计权威：`docs/sprints/agent-feedback-loop-2026-09-08/tech-design.md` §2.1-§2.2。

心智模型＝「机器人开 PR 的收件箱」：候选是对某资产的建/改/退**提议**，
只存草稿与证据，**本 store 永无生效权**——采纳物化（预填对方原生入口）在批 2b-i，
不在这里。

硬规则（全部服务端强制，UT 钉死）：

- **单事务 upsert**：`BEGIN IMMEDIATE` 下同 (team_id, semantic_sig) 的 open 候选
  合并——evidence 是**追加数组**（每份带 contributor/rubric/披露时间戳/provenance
  指针，永不覆盖既有元素）；rubric 首份为主展示，后续在数组里当 co-signer。
  同 contributor 重复确认＝幂等 no-op（不聚合成多次用户选择）。
- **禁自批**：decide 的操作者不得是 proposer 或任何 contributor。
- **配额（hard，落库计数）**：每 proposer 每团队每日**新开**上限 + 每团队 open
  总量上限（不计已到期未翻转的行）；合并进既有候选不占新开配额。
- **冷却**：rejected 与 expired 同权重写 `feedback_candidate_suppressions`
  （独立表，不随候选行消失，带 target 明文列可归属排查）；冷却期内同签名重提
  被拒并把 contributor **去重**累计进 `pending_contributors`——
  **不同人数超过上次 evidence_count 才解除冷却**（「须显著增长」＝更多人背书，
  同一个人反复点按钮不算）。
- **过期＝读时惰性翻转**（批 3 cron 前无后台驱动；过期回执 deferred 批 3）；
  decide 入口先对该候选做同签名过期翻转，防裁决已到期行。
- **落库前处理**：proposed / rubric / provenance_refs 全部过双层 scrub
  （凭据+PII，eval_harvest 同款）；rubric 另过 `sanitize_for_injection`
  （它是未来 eval LLM 的输入）；输入（rubric/sig_text/proposed 递归）自带
  消毒记号（marker 伪造）直接拒。
- 表由 migration `0059_feedback_candidates` 建；本 store **无 initialize DDL**
  （`docs/design/数据库迁移.md` 规则）。
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.prompt_injection_defense import sanitize_for_injection
from bowhead_core.scrub import scrub as _scrub_cred
from bowhead_core.scrubber import scrub as _scrub_pii
from bowhead_core.semantic_fingerprint import (
    derive_semantic_secret,
    semantic_signature_v1,
)

logger = logging.getLogger(__name__)

TARGET_KINDS = frozenset(
    {"agent_def", "team_skill", "semantic", "tool_source", "new_asset", "retire"},
)

# Closed set for retire candidates' proposed.asset_kind; the read-side _RETIRE_NATIVE shares this source of truth.
RETIRE_ASSET_KINDS = frozenset({"agent_def", "team_skill", "tool_source"})

_OPEN = "open"
_ACCEPTED = "accepted"
_REJECTED = "rejected"
_EXPIRED = "expired"
_SUPERSEDED = "superseded"

_SANITIZER_MARK = "[[escaped:"

# FR7 observation window (PRD §4 FR7: default 7 days, soft). Too short misses the common recurrence of a proposal two weeks later;
# too long labels a need returning six months after approval as recurrence, when the need itself has returned.
RECURRENCE_WINDOW_DAYS = 7


class FeedbackCandidateError(Exception):
    """基类：携带稳定 code，路由层据此出结构化错误（错误分类不能说谎）。"""

    code = "feedback_candidate_error"


class FeedbackLoopDisabled(FeedbackCandidateError):
    code = "feedback_loop_disabled"


class RubricRejected(FeedbackCandidateError):
    code = "rubric_rejected"


class QuotaExceeded(FeedbackCandidateError):
    code = "quota_exceeded"

    def __init__(self, which: str) -> None:
        super().__init__(which)
        self.which = which


class SuppressedSignature(FeedbackCandidateError):
    code = "signature_suppressed"


class SelfApprovalError(FeedbackCandidateError):
    code = "self_approval_forbidden"


class InvalidStateError(FeedbackCandidateError):
    code = "invalid_state"


class ConcurrentWriteConflict(FeedbackCandidateError):
    """部分唯一索引兜底触发（正常路径 BEGIN IMMEDIATE 下不该发生）。"""

    code = "concurrent_write_conflict"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def scrub_deep(obj: Any) -> Any:
    """双层脱敏（凭据→PII），递归 dict/list——eval_harvest `_scrub_deep` 同款。"""
    if isinstance(obj, str):
        return _scrub_pii(_scrub_cred(obj))
    if isinstance(obj, dict):
        return {k: scrub_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_deep(v) for v in obj]
    return obj


def _has_marker_text(text: str) -> bool:
    # Apply NFKC before checking: the fullwidth variant "［［ｅｓｃａｐｅｄ：" normalizes to the real marker;
    # checking without normalization permits forgery on any downstream surface that normalizes before consuming (repair review P2-1).
    return _SANITIZER_MARK in unicodedata.normalize("NFKC", text)


def _contains_marker(obj: Any) -> bool:
    if isinstance(obj, str):
        return _has_marker_text(obj)
    if isinstance(obj, dict):
        return any(_contains_marker(k) or _contains_marker(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_contains_marker(v) for v in obj)
    return False


@dataclass(frozen=True)
class FeedbackCandidate:
    id: str
    team_id: str
    target_kind: str
    target_ref: str
    base_fingerprint: str
    proposed: dict[str, Any]
    rubric_text: str
    evidence: list[dict[str, Any]]
    semantic_sig: str
    evidence_count: int
    cluster_id: str | None
    state: str
    proposer_id: str
    decided_by: str | None
    decided_at: str | None
    decide_note: str | None
    expires_at: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> FeedbackCandidate:
        return cls(
            id=row["id"],
            team_id=row["team_id"],
            target_kind=row["target_kind"],
            target_ref=row["target_ref"],
            base_fingerprint=row["base_fingerprint"],
            proposed=json.loads(row["proposed"]),
            rubric_text=row["rubric_text"],
            evidence=json.loads(row["evidence"]),
            semantic_sig=row["semantic_sig"],
            evidence_count=row["evidence_count"],
            cluster_id=row["cluster_id"],
            state=row["state"],
            proposer_id=row["proposer_id"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"],
            decide_note=row["decide_note"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class FeedbackCandidateStore:
    db_path: str
    fingerprint_key: str | bytes | None = field(default=None, repr=False)
    proposer_daily_cap: int = 5
    team_open_cap: int = 200
    default_ttl_days: int = 14
    suppression_days: int = 30
    _secret: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.fingerprint_key is None:
            # Wiring must pass a BOWHEAD_FERNET_KEY derivation; the weak fallback is for development only.
            # Adding the key later changes signature semantics, invalidating all existing cooldowns/unique keys; warn now.
            logger.warning(
                "FeedbackCandidateStore without fingerprint_key: dev-only derivation in use; wire BOWHEAD_FERNET_KEY before prod.",
            )
        seed = self.fingerprint_key or f"dev-only:{self.db_path}"
        self._secret = derive_semantic_secret(seed)

    # ── Writes ──────────────────────────────────────────────────────────────

    async def propose(
        self,
        *,
        team_id: str,
        target_kind: str,
        target_ref: str,
        proposed: dict[str, Any],
        rubric_text: str,
        contributor_id: str,
        sig_text: str,
        provenance_refs: list[str] | None = None,
        base_fingerprint: str = "",
        ttl_days: int | None = None,
    ) -> tuple[FeedbackCandidate, str]:
        """落一条候选；同签名 open 候选存在则合并证据。

        返回 (candidate, outcome)，outcome ∈ {"created", "merged", "duplicate"}。
        """
        if target_kind not in TARGET_KINDS:
            raise ValueError(f"invalid target_kind: {target_kind!r}")
        if target_kind == "retire" and proposed.get("asset_kind") not in RETIRE_ASSET_KINDS:
            # Writes and reads share the same definition: persist exactly what asset a retire candidate retires.
            raise ValueError(f"invalid retire asset_kind: {proposed.get('asset_kind')!r}")
        if _SANITIZER_MARK in rubric_text or _SANITIZER_MARK in sig_text or _contains_marker(proposed):
            # Marker forgery: a sanitizer marker in input undermines the gate that rejects output containing a rewritten marker;
            # proposed is a future prefill payload and must not carry one either.
            raise RubricRejected("sanitizer_marker_in_input")
        # Redact rubric before injection protection (it is persisted and enters future eval context).
        rubric = sanitize_for_injection(_scrub_pii(_scrub_cred(rubric_text)))
        clean_proposed = scrub_deep(proposed)
        refs = [_scrub_pii(_scrub_cred(str(r))) for r in (provenance_refs or [])]
        sig = semantic_signature_v1(
            secret=self._secret,
            target_kind=target_kind,
            target_ref=target_ref,
            text=sig_text,
        )
        now = _utcnow()
        now_iso = _iso(now)
        entry = {
            "contributor_id": contributor_id,
            "rubric_text": rubric,
            "disclosure_ts": now_iso,
            "provenance_refs": refs,
        }

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._expire_due(db, team_id, now, semantic_sig=sig)
                open_row = await self._fetch_open(db, team_id, sig)
                if open_row is not None:
                    result = await self._merge(db, open_row, entry, now_iso)
                    await db.commit()
                    return result

                await self._check_suppression(db, team_id, sig, now, contributor_id)
                await self._check_quota(db, team_id, contributor_id, now)
                candidate_id = f"fbc_{uuid4().hex[:12]}"
                expires = now + timedelta(days=ttl_days or self.default_ttl_days)
                await db.execute(
                    """
                    INSERT INTO feedback_candidates (
                        id, team_id, target_kind, target_ref, base_fingerprint,
                        proposed, rubric_text, evidence, semantic_sig,
                        evidence_count, state, proposer_id, expires_at,
                        created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        candidate_id,
                        team_id,
                        target_kind,
                        target_ref,
                        base_fingerprint,
                        json.dumps(clean_proposed, ensure_ascii=False),
                        rubric,
                        json.dumps([entry], ensure_ascii=False),
                        sig,
                        1,
                        _OPEN,
                        contributor_id,
                        _iso(expires),
                        now_iso,
                        now_iso,
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise ConcurrentWriteConflict(str(exc)) from exc
            except BaseException:
                await db.rollback()
                raise
            created = await self._get(db, candidate_id)
            assert created is not None
            return created, "created"

    async def decide(
        self,
        candidate_id: str,
        *,
        decision: str,
        decided_by: str,
        note: str = "",
    ) -> FeedbackCandidate:
        """审批：open → accepted/rejected。禁自批；rejected 写冷却。

        入口先做同签名过期翻转——「open→expired 仅系统」的状态机不给
        「已到期未翻转」的行留裁决窗口。
        """
        if decision not in (_ACCEPTED, _REJECTED):
            raise ValueError(f"invalid decision: {decision!r}")
        now = _utcnow()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._get(db, candidate_id)
                if row is None:
                    await db.rollback()
                    raise InvalidStateError(candidate_id)
                await self._expire_due(
                    db,
                    row.team_id,
                    now,
                    semantic_sig=row.semantic_sig,
                )
                row = await self._get(db, candidate_id)
                assert row is not None
                if row.state != _OPEN:
                    await db.commit()  # Preserve the valid expiration transition.
                    raise InvalidStateError(candidate_id)
                involved = {row.proposer_id} | {e.get("contributor_id") for e in row.evidence}
                if decided_by in involved:
                    raise SelfApprovalError(decided_by)
                await db.execute(
                    """
                    UPDATE feedback_candidates
                    SET state=?, decided_by=?, decided_at=?, decide_note=?, updated_at=?
                    WHERE id=? AND state=?
                    """,
                    (
                        decision,
                        decided_by,
                        _iso(now),
                        note,
                        _iso(now),
                        candidate_id,
                        _OPEN,
                    ),
                )
                if decision == _REJECTED:
                    await self._write_suppression(
                        db,
                        row.team_id,
                        row.semantic_sig,
                        target_kind=row.target_kind,
                        target_ref=row.target_ref,
                        last_state=_REJECTED,
                        last_evidence_count=row.evidence_count,
                        now=now,
                    )
                await db.commit()
            except InvalidStateError:
                raise
            except BaseException:
                await db.rollback()
                raise
            updated = await self._get(db, candidate_id)
            assert updated is not None
            return updated

    async def withdraw(self, candidate_id: str, *, by: str) -> FeedbackCandidate:
        """撤回：仅当 evidence 只剩本人（且本人＝proposer）时允许，落 superseded。

        （对 tech-design §2.2「proposer 不可迁移任何状态」的唯一例外，
        已回写设计消解——撤自己一个人的提议不需要仪式。）
        """
        now = _utcnow()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._get(db, candidate_id)
                if row is None:
                    await db.rollback()
                    raise InvalidStateError(candidate_id)
                # As in decide, first expire overdue rows (writing suppression); do not leave an overdue, unflipped row
                # a withdrawal window that bypasses expiration suppression (2a′ legacy caught in 2b-i re-review).
                await self._expire_due(db, row.team_id, now, semantic_sig=row.semantic_sig)
                row = await self._get(db, candidate_id)
                assert row is not None
                if row.state != _OPEN:
                    await db.commit()  # Preserve the valid expiration transition.
                    raise InvalidStateError(candidate_id)
                contributors = {e.get("contributor_id") for e in row.evidence}
                if by != row.proposer_id or contributors != {by}:
                    await db.rollback()  # As in decide's SelfApproval path, discard this transaction's same-signature expiration flips
                    raise InvalidStateError("withdraw_requires_sole_contributor")
                await db.execute(
                    """
                    UPDATE feedback_candidates
                    SET state=?, decide_note=?, updated_at=? WHERE id=? AND state=?
                    """,
                    (
                        _SUPERSEDED,
                        "withdrawn_by_proposer",
                        _iso(now),
                        candidate_id,
                        _OPEN,
                    ),
                )
                await db.commit()
            except InvalidStateError:
                raise  # Already committed/rolled back; do not roll back again (as in decide)
            except BaseException:
                await db.rollback()
                raise
            updated = await self._get(db, candidate_id)
            assert updated is not None
            return updated

    async def retarget(
        self,
        candidate_id: str,
        *,
        target_kind: str,
        target_ref: str,
        sig_text: str,
        by: str,
    ) -> tuple[FeedbackCandidate, FeedbackCandidate, str]:
        """转案（tech-design §2.8.4）：open 候选换落点。单事务：

        ①原行 → superseded（note=`rerouted:<new_id>`，decided_by=改道人留痕）；
        ②新签名 **先过冷却**（冷却中⇒SuppressedSignature，pending 照记——防「被拒后换目标再改回来」洗白）；
        ③不计 proposer 日配额、计 team_open_cap；
        ④新行复制 proposer/evidence/rubric/到期日，`proposed.rerouted_from=<old_id>`；同签名 open 已存在则并入证据。
        调用者 ∉ involved（联署人不能自己改道）；审批人按新目标重新裁定，改道人不获得决定权。
        返回 (old, new, outcome)，outcome ∈ {"created", "merged"}。
        """
        if target_kind not in TARGET_KINDS:
            raise ValueError(f"invalid target_kind: {target_kind!r}")
        now = _utcnow()
        now_iso = _iso(now)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._get(db, candidate_id)
                if row is None:
                    await db.rollback()
                    raise InvalidStateError(candidate_id)
                await self._expire_due(db, row.team_id, now, semantic_sig=row.semantic_sig)
                row = await self._get(db, candidate_id)
                assert row is not None
                if row.state != _OPEN:
                    await db.commit()
                    raise InvalidStateError(candidate_id)
                if target_kind == row.target_kind and target_ref == row.target_ref:
                    await db.rollback()
                    raise InvalidStateError("reroute_same_target")
                if target_kind == "retire" and row.proposed.get("asset_kind") not in RETIRE_ASSET_KINDS:
                    await db.rollback()
                    raise ValueError("retire target requires proposed.asset_kind")
                involved = {row.proposer_id} | {e.get("contributor_id") for e in row.evidence}
                if by in involved:
                    await db.rollback()
                    raise SelfApprovalError(by)
                new_sig = semantic_signature_v1(secret=self._secret, target_kind=target_kind, target_ref=target_ref, text=sig_text)
                await self._expire_due(db, row.team_id, now, semantic_sig=new_sig)
                open_row = await self._fetch_open(db, row.team_id, new_sig)
                if open_row is not None and open_row.id != row.id:
                    merged = open_row
                    for entry in row.evidence:
                        merged, _ = await self._merge(db, merged, entry, now_iso)
                    new_id = merged.id
                    outcome = "merged"
                else:
                    # Cooldown counts ALL original endorsers (three cosigners retargeting must not be blocked by a one-person rejection cooldown).
                    await self._check_suppression(db, row.team_id, new_sig, now, row.proposer_id, extra_contributors=involved)
                    # Retargeting has net-zero open count (the original is immediately superseded); exclude it so reaching the cap does not reject this.
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM feedback_candidates WHERE team_id=? AND state=? AND expires_at>? AND id<>?",
                        (row.team_id, _OPEN, now_iso, row.id),
                    )
                    (open_total,) = await cursor.fetchone()
                    if int(open_total) >= self.team_open_cap:
                        await db.rollback()
                        raise QuotaExceeded("team_open")
                    new_id = f"fbc_{uuid4().hex[:12]}"
                    proposed = {**row.proposed, "rerouted_from": row.id}
                    await db.execute(
                        """
                        INSERT INTO feedback_candidates (
                            id, team_id, target_kind, target_ref, base_fingerprint,
                            proposed, rubric_text, evidence, semantic_sig,
                            evidence_count, state, proposer_id, expires_at,
                            created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            new_id,
                            row.team_id,
                            target_kind,
                            target_ref,
                            "",
                            json.dumps(proposed, ensure_ascii=False),
                            row.rubric_text,
                            json.dumps(row.evidence, ensure_ascii=False),
                            new_sig,
                            row.evidence_count,
                            _OPEN,
                            row.proposer_id,
                            row.expires_at,
                            now_iso,
                            now_iso,
                        ),
                    )
                    outcome = "created"
                await db.execute(
                    """
                    UPDATE feedback_candidates
                    SET state=?, decided_by=?, decided_at=?, decide_note=?, updated_at=? WHERE id=? AND state=?
                    """,
                    (_SUPERSEDED, by, now_iso, f"rerouted:{new_id}", now_iso, row.id, _OPEN),
                )
                await db.commit()
            except (InvalidStateError, SuppressedSignature):
                raise  # Already committed or rolled back
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise ConcurrentWriteConflict(str(exc)) from exc
            except BaseException:
                await db.rollback()
                raise
            old = await self._get(db, row.id)
            created = await self._get(db, new_id)
            assert old is not None and created is not None
            return old, created, outcome

    async def mark_deep_dive(self, candidate_id: str, *, by: str) -> FeedbackCandidate:
        """把 open 候选的 `proposed.deep_dive` 置 True（批 3c：本人再次点名「查清楚为什么」）。
        只有 involved（proposer ∪ contributors）能置；它是排序键不是审批事实，不改 updated_at 以外的状态。"""

        now = _utcnow()
        now_iso = _iso(now)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._get(db, candidate_id)
                if row is None:
                    await db.rollback()
                    raise InvalidStateError(candidate_id)
                await self._expire_due(db, row.team_id, now, semantic_sig=row.semantic_sig)  # As in decide/withdraw
                row = await self._get(db, candidate_id)
                assert row is not None
                if row.state != _OPEN:
                    await db.commit()  # Preserve the valid expiration transition
                    raise InvalidStateError(candidate_id)
                involved = {row.proposer_id} | {e.get("contributor_id") for e in row.evidence}
                if by not in involved:
                    await db.rollback()
                    raise InvalidStateError("deep_dive_requires_involved")  # Not involved; do not reuse SelfApproval user-facing text
                proposed = {**row.proposed, "deep_dive": True}
                await db.execute(
                    "UPDATE feedback_candidates SET proposed=?, updated_at=? WHERE id=? AND state=?",
                    (json.dumps(proposed, ensure_ascii=False), now_iso, candidate_id, _OPEN),
                )
                await db.commit()
            except InvalidStateError:
                raise
            except BaseException:
                await db.rollback()
                raise
            updated = await self._get(db, candidate_id)
            assert updated is not None
            return updated

    # ── Reads ───────────────────────────────────────────────────────────────

    async def get(self, candidate_id: str) -> FeedbackCandidate | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await self._get(db, candidate_id)

    async def list_open(self, team_id: str) -> list[FeedbackCandidate]:
        """team 的 open 候选（读时惰性把到期行翻成 expired 并写冷却）。

        注意：惰性过期**不判 feature flag**——它是既有数据的合法状态迁移，
        关闸只停「新候选产生」，不该把已到期的行冻成永远 open。
        """
        now = _utcnow()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._expire_due(db, team_id, now)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            cursor = await db.execute(
                """
                SELECT * FROM feedback_candidates
                WHERE team_id=? AND state=? ORDER BY updated_at DESC
                """,
                (team_id, _OPEN),
            )
            rows = await cursor.fetchall()
            return [FeedbackCandidate.from_row(r) for r in rows]

    async def list_decided(self, team_id: str, *, limit: int = 50) -> list[FeedbackCandidate]:
        """The team's most recently closed candidates (accepted, rejected, expired or withdrawn), newest first."""
        now = _utcnow()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._expire_due(db, team_id, now)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            cursor = await db.execute(
                """
                SELECT * FROM feedback_candidates
                WHERE team_id=? AND state<>? ORDER BY COALESCE(decided_at, updated_at) DESC, id LIMIT ?
                """,
                (team_id, _OPEN, limit),
            )
            rows = await cursor.fetchall()
            return [FeedbackCandidate.from_row(r) for r in rows]

    # ── Internals ───────────────────────────────────────────────────────────

    async def sweep_expired(self, now: datetime | None = None) -> list[FeedbackCandidate]:
        """全团队过期翻转（自己开事务），返回**所有**「expired 且未发回执」的行。

        回执真相在 sidecar 表 `feedback_casework.receipt_at`（0062）；谁翻的无所谓——
        propose/decide/withdraw/list_open 惰性翻的行也在这里补回执（tech-design §2.8.4 P1-2）。
        回执发成功后调用方 `mark_receipt`。
        """
        now = now or _utcnow()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._expire_due(db, None, now)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            cursor = await db.execute(
                """
                SELECT c.* FROM feedback_candidates c
                LEFT JOIN feedback_casework w ON w.candidate_id=c.id
                WHERE c.state=? AND w.receipt_at IS NULL
                ORDER BY c.updated_at
                """,
                (_EXPIRED,),
            )
            return [FeedbackCandidate.from_row(r) for r in await cursor.fetchall()]

    async def mark_receipt(self, candidate_id: str, when: datetime | None = None) -> None:
        """记「终态回执已发」——落 sidecar 行（无行则建，trigger=explicit 占位）。"""

        stamp = _iso(when or _utcnow())
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute("SELECT team_id FROM feedback_candidates WHERE id=?", (candidate_id,))
            row = await cursor.fetchone()
            if row is None:
                return
            await db.execute(
                """
                INSERT INTO feedback_casework (candidate_id, team_id, trigger, status, receipt_at, created_at, updated_at)
                VALUES (?, ?, 'explicit', 'queued', ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET receipt_at=COALESCE(feedback_casework.receipt_at, excluded.receipt_at), updated_at=excluded.updated_at
                """,
                (candidate_id, row[0], stamp, stamp, stamp),
            )
            await db.commit()

    async def find_recurrences(
        self,
        team_id: str,
        candidates: list[FeedbackCandidate],
        *,
        within_days: int = RECURRENCE_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> dict[str, FeedbackCandidate]:
        """FR7 复发检测：这些候选里，哪些是**已经批过的同一件事又回来了**。

        判据是结构事实，不是模型判断：同团队 + 同 `semantic_sig`（签名本身已含 target_kind/ref，
        所以同签名蕴含同落点；转案换了目标 ⇒ 签名变 ⇒ 不算复发，那是换落点不是复发）+ 历史候选
        `state='accepted'` + 决定时间在观察窗内。

        为什么值得单独报：今天没有任何机制认出「这条上次批过」，审批人会把它当成一条全新建议
        再批一次，改法还可能与上次不同——越改越乱。命中意味着三种可能之一：改的地方不对、
        改了没生效、或被后续编辑覆盖回去了。v1 只报告，不自动回滚（回滚本体不归本线）。

        返回 {当前候选 id: 最近一条已采纳的历史候选}；无命中的键不出现。
        """

        sigs = {c.semantic_sig for c in candidates if c.semantic_sig}
        if not sigs:
            return {}
        cutoff = _iso((now or _utcnow()) - timedelta(days=within_days))
        placeholders = ",".join("?" for _ in sigs)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT * FROM feedback_candidates
                WHERE team_id=? AND state=? AND decided_at>=? AND semantic_sig IN ({placeholders})
                ORDER BY decided_at DESC
                """,  # noqa: S608 -- placeholders only
                (team_id, _ACCEPTED, cutoff, *sigs),
            )
            rows = [FeedbackCandidate.from_row(r) for r in await cursor.fetchall()]
        by_sig: dict[str, FeedbackCandidate] = {}
        for row in rows:  # Already ordered by decided_at DESC; first is newest
            by_sig.setdefault(row.semantic_sig, row)
        out: dict[str, FeedbackCandidate] = {}
        for cand in candidates:
            prior = by_sig.get(cand.semantic_sig)
            if prior is not None and prior.id != cand.id:
                out[cand.id] = prior
        return out

    async def list_casework_pool(self, *, older_than: datetime) -> list[FeedbackCandidate]:
        """跨团队 open 且创建早于 older_than 的候选（教练办案候补池）；不惰性翻转，不判 flag。"""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM feedback_candidates WHERE state=? AND created_at<=? ORDER BY created_at",
                (_OPEN, _iso(older_than)),
            )
            return [FeedbackCandidate.from_row(r) for r in await cursor.fetchall()]

    async def _get(
        self,
        db: aiosqlite.Connection,
        candidate_id: str,
    ) -> FeedbackCandidate | None:
        cursor = await db.execute(
            "SELECT * FROM feedback_candidates WHERE id=?",
            (candidate_id,),
        )
        row = await cursor.fetchone()
        return FeedbackCandidate.from_row(row) if row else None

    async def _fetch_open(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        sig: str,
    ) -> FeedbackCandidate | None:
        cursor = await db.execute(
            """
            SELECT * FROM feedback_candidates
            WHERE team_id=? AND semantic_sig=? AND state=?
            """,
            (team_id, sig, _OPEN),
        )
        row = await cursor.fetchone()
        return FeedbackCandidate.from_row(row) if row else None

    async def _merge(
        self,
        db: aiosqlite.Connection,
        row: FeedbackCandidate,
        entry: dict[str, Any],
        now_iso: str,
    ) -> tuple[FeedbackCandidate, str]:
        contributors = {e.get("contributor_id") for e in row.evidence}
        if entry["contributor_id"] in contributors:
            # Idempotent retries from the same contributor do not aggregate into multiple user choices (same semantics as track A).
            return row, "duplicate"
        evidence = [*row.evidence, entry]
        await db.execute(
            """
            UPDATE feedback_candidates
            SET evidence=?, evidence_count=?, updated_at=? WHERE id=? AND state=?
            """,
            (
                json.dumps(evidence, ensure_ascii=False),
                len(evidence),
                now_iso,
                row.id,
                _OPEN,
            ),
        )
        merged = await self._get(db, row.id)
        assert merged is not None
        return merged, "merged"

    async def _check_suppression(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        sig: str,
        now: datetime,
        contributor_id: str,
        *,
        extra_contributors: set[str] | None = None,
    ) -> None:
        """冷却判定：pending contributors **去重**，不同人数 > 上次 evidence_count
        才解除——同一个人反复重试不构成「证据显著增长」。"""
        cursor = await db.execute(
            """
            SELECT last_evidence_count, pending_contributors, suppressed_until
            FROM feedback_candidate_suppressions
            WHERE team_id=? AND semantic_sig=?
            """,
            (team_id, sig),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        if str(row["suppressed_until"]) <= _iso(now):
            await db.execute(
                "DELETE FROM feedback_candidate_suppressions WHERE team_id=? AND semantic_sig=?",
                (team_id, sig),
            )
            return
        try:
            pending = set(json.loads(row["pending_contributors"]))
        except (TypeError, json.JSONDecodeError):
            pending = set()
        pending.add(contributor_id)
        pending.update(c for c in (extra_contributors or set()) if isinstance(c, str) and c)
        if len(pending) > int(row["last_evidence_count"]):
            # More people endorse it: lift the cooldown and allow reopening.
            await db.execute(
                "DELETE FROM feedback_candidate_suppressions WHERE team_id=? AND semantic_sig=?",
                (team_id, sig),
            )
            return
        await db.execute(
            """
            UPDATE feedback_candidate_suppressions
            SET pending_contributors=?, updated_at=?
            WHERE team_id=? AND semantic_sig=?
            """,
            (json.dumps(sorted(pending)), _iso(now), team_id, sig),
        )
        # Cooldown counts must survive the failure path (this propose is rejected), so commit here;
        # the _expire_due flips committed along with them are valid transitions independent of this operation's success.
        await db.commit()
        raise SuppressedSignature(sig)

    async def _check_quota(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        proposer_id: str,
        now: datetime,
    ) -> None:
        day_start = now.date().isoformat()
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM feedback_candidates
            WHERE team_id=? AND proposer_id=? AND created_at>=?
            """,
            (team_id, proposer_id, day_start),
        )
        (daily,) = await cursor.fetchone()
        if int(daily) >= self.proposer_daily_cap:
            raise QuotaExceeded("proposer_daily")
        # The open count excludes overdue rows not yet flipped on read; nobody calls list_open during a dark batch,
        # and zombie open rows must not consume quota.
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM feedback_candidates
            WHERE team_id=? AND state=? AND expires_at>?
            """,
            (team_id, _OPEN, _iso(now)),
        )
        (open_total,) = await cursor.fetchone()
        if int(open_total) >= self.team_open_cap:
            raise QuotaExceeded("team_open")

    async def _expire_due(
        self,
        db: aiosqlite.Connection,
        team_id: str | None,
        now: datetime,
        semantic_sig: str | None = None,
    ) -> None:
        params: list[Any] = [_OPEN, _iso(now)]
        clause = "state=? AND expires_at<=?"
        if team_id is not None:
            clause += " AND team_id=?"
            params.append(team_id)
        if semantic_sig is not None:
            clause += " AND semantic_sig=?"
            params.append(semantic_sig)
        cursor = await db.execute(
            f"""
            SELECT id, team_id, semantic_sig, target_kind, target_ref, evidence_count
            FROM feedback_candidates WHERE {clause}
            """,
            params,
        )
        due = await cursor.fetchall()
        for row in due:
            await db.execute(
                "UPDATE feedback_candidates SET state=?, updated_at=? WHERE id=?",
                (_EXPIRED, _iso(now), row["id"]),
            )
            await self._write_suppression(
                db,
                str(row["team_id"]),
                str(row["semantic_sig"]),
                target_kind=str(row["target_kind"]),
                target_ref=str(row["target_ref"]),
                last_state=_EXPIRED,
                last_evidence_count=int(row["evidence_count"]),
                now=now,
            )

    async def _write_suppression(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        sig: str,
        *,
        target_kind: str,
        target_ref: str,
        last_state: str,
        last_evidence_count: int,
        now: datetime,
    ) -> None:
        until = _iso(now + timedelta(days=self.suppression_days))
        await db.execute(
            """
            INSERT INTO feedback_candidate_suppressions (
                team_id, semantic_sig, target_kind, target_ref, last_state,
                last_evidence_count, pending_contributors, suppressed_until,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,'[]',?,?,?)
            ON CONFLICT(team_id, semantic_sig) DO UPDATE SET
                target_kind=excluded.target_kind,
                target_ref=excluded.target_ref,
                last_state=excluded.last_state,
                last_evidence_count=excluded.last_evidence_count,
                pending_contributors='[]',
                suppressed_until=excluded.suppressed_until,
                updated_at=excluded.updated_at
            """,
            (
                team_id,
                sig,
                target_kind,
                target_ref,
                last_state,
                last_evidence_count,
                until,
                _iso(now),
                _iso(now),
            ),
        )
