"""Canvas / Section / Revision 持久化 (TD §12.1).

Schema:
    canvases(id, session_id, workspace_id, user_id, title, status, created_at, updated_at)
    canvas_sections(id, canvas_id, seq, kind, payload JSON, rev, created_at, updated_at)
    canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by, at)

Canvas mode 约定：1 session = 1 canvas。section 顺序由 seq 决定。
每次 append/patch/reorder/delete 写一条 revisions。

与 session_store 共库。schema 幂等（IF NOT EXISTS）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.chart.expression import validate_stored_composition
from bowhead_core.terminal_llm_invocation import detached_auxiliary_scope

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

# R2 report state machine (lifecycle): active=fresh baseline; verified=owner verified;
# stale=expired data window/upstream changes/quarantined sections (quarantine-derived);
# archived=excluded from AI/KB retrieval. Automatic anchor invalidation belongs to gated R3; this version supports manual status + quarantine derivation only.
CANVAS_STATUSES: tuple[str, ...] = ("active", "verified", "stale", "archived")


_REPORT_LIST_SORT_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    # (column of the report-list row, direction); the last key is always the unique id tie-break.
    "updated_desc": (("updated_at", "DESC"), ("id", "DESC")),
    "sections_desc": (("sec_count", "DESC"), ("updated_at", "DESC"), ("id", "DESC")),
    "title_asc": (("sort_title", "ASC"), ("id", "ASC")),
}
_REPORT_LIST_CURSOR_TYPES: dict[str, type] = {"updated_at": str, "sec_count": int, "sort_title": str, "id": str}
_REPORT_LIST_CURSOR_MAX_INT = 2**31
_REPORT_LIST_CURSOR_MAX_STR = 512


def normalize_report_list_sort(sort: str | None) -> str:
    """Map an unknown report-list sort to the default ``updated_desc`` (legacy behavior)."""
    return sort if sort in _REPORT_LIST_SORT_KEYS else "updated_desc"


def report_list_cursor_values(item: dict[str, Any], sort: str) -> list[Any]:
    """Return the sort-key values of a :meth:`CanvasStore.list_for_user` row for keyset paging."""
    fields = {
        "updated_at": item.get("updated_at"),
        "sec_count": int(item.get("sections_count") or 0),
        "sort_title": item.get("title") or "",
        "id": item.get("id"),
    }
    return [fields[column] for column, _ in _REPORT_LIST_SORT_KEYS[normalize_report_list_sort(sort)]]


def report_list_cursor_values_valid(values: Any, sort: str) -> bool:
    """Whether decoded cursor ``values`` have the shape and types ``sort`` expects."""
    keys = _REPORT_LIST_SORT_KEYS[normalize_report_list_sort(sort)]
    if not isinstance(values, list) or len(values) != len(keys):
        return False
    for value, (column, _) in zip(values, keys, strict=True):
        expected = _REPORT_LIST_CURSOR_TYPES[column]
        if isinstance(value, bool) or not isinstance(value, expected):
            return False
        # Bound client-supplied values: SQLite raises OverflowError beyond int64, and no
        # real sort value comes near these limits.
        if isinstance(value, int) and not 0 <= value < _REPORT_LIST_CURSOR_MAX_INT:
            return False
        if isinstance(value, str) and len(value) > _REPORT_LIST_CURSOR_MAX_STR:
            return False
    return True


def _effective_status(stored: str, quarantined_count: int) -> str:
    """显示用的有效状态: archived 优先; 有 quarantined section → stale(收编); 否则原状态."""
    if stored == "archived":
        return "archived"
    if quarantined_count > 0:
        return "stale"
    return stored if stored in CANVAS_STATUSES else "active"


#: Sections excluded from report surface (workspace attachments: SQL/code/bare tables), matching frontend selectReportSurfaceSections.
_REPORT_SURFACE_EXCLUDED_KINDS = frozenset({"code_block", "data_table"})

#: Terminal done field for report sections saved this turn and visible after refresh; identical across live/persistence/replay.
REPORT_SECTIONS_SAVED_FIELD = "report_sections_saved"


async def report_sections_saved_field(store: Any, turn_id: str) -> dict[str, int]:
    """终态 ``done`` 要并入的 ``{"report_sections_saved": N}``；拿不到就返空字典。

    在 Turn 终态落库、画布终态裁决（隐藏 / 折叠）之后、``done`` 发出之前调用：值随 ``done``
    一起持久化，F5 读回放里的同一条 ``done``。没有画布存储或查询失败时不带字段（前端按
    「不知道几节」显示），不能写 0 冒充「一节都没保存」，也绝不因此挡住终态。
    """
    if store is None or not turn_id:
        return {}
    counter = getattr(store, "count_report_sections_saved_for_turn", None)
    if counter is None:
        return {}
    try:
        count = await counter(turn_id)
    except Exception as exc:
        logger.warning("canvas.report_sections_saved_failed turn=%s err=%s", turn_id, exc)
        return {}
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return {}
    return {REPORT_SECTIONS_SAVED_FIELD: count}


# R4 full-text search (FTS5): index human-readable strings from section payloads; skip structural/identity/URL fields.
_FTS_SKIP_KEYS = frozenset(
    {
        "id",
        "kind",
        "artifact_id",
        "cardUrl",
        "url",
        "src",
        "href",
        "section_id",
        "chart_section_id",
        "from",
        "to",
    }
)
_FTS_BODY_BUDGET = 8000  # Per-section indexed-character cap; prevents oversized payloads bloating the index


def _harvest_strings(obj: Any, out: list[str], budget: list[int]) -> None:
    if budget[0] <= 0:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            out.append(s)
            budget[0] -= len(s)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in _FTS_SKIP_KEYS:
                continue
            _harvest_strings(v, out, budget)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _harvest_strings(v, out, budget)


def _section_search_text(payload: dict[str, Any]) -> str:
    """收 section payload 里的可搜索人话文本(markdown/标题/表格/callout 等)."""
    out: list[str] = []
    _harvest_strings(payload, out, [_FTS_BODY_BUDGET])
    return " ".join(out)


def _fts_match_query(q: str) -> str:
    """把用户串转成 trigram FTS5 phrase 查询(子串匹配, CJK 友好). 转义双引号."""
    return '"' + q.replace('"', '""') + '"'


# For split_section: gen3 [N] citations contain only an index; source IDs live in the sources section,
# so **Markdown cannot recover them**. Finding a citation during split means this section references data →
# inherit all source_refs (conservative, preserves links; Feishu 07.06 #11 decision).
# citation-audit M3-5: previously gen2 extraction ran first ([num:…:src:S]/[ref:S] → IDs for precise
# filtering); after M2 cleanup it always returned empty and fell back to full inheritance, so remove extractor and both regexes.
_NUMBERED_CITE_RE = re.compile(r"\[\d{1,3}\]")

# WP3 review #7: structural/identity fields clients must never change through section payload PATCH.
# kind=type (changes cause type confusion), id=identity, rev=version (expected_rev channel), seq=order
# (/reorder channel), timestamps=server-written.
# report-engine Phase-2 WP-A: protect owner/added_by too; server stamps ownership from peer identity
# as a security boundary, never mutable through payload patching (security re-review P0:
# otherwise same-owner writers can forge attribution through _meta.added_by). owner is a separate column
# (see patch_section caller check); added_by is the old lineage field in payload._meta.
# Neither may be written by patch.
_PATCH_PROTECTED_FIELDS = frozenset(
    {
        "id",
        "kind",
        "rev",
        "seq",
        "created_at",
        "updated_at",
        "owner",
        "added_by",
    }
)
# canvas-update-turn batch A2: server-stamped payload._meta lineage subkeys may be carried by patch, never changed.
# ψ-E eager card path stamps composed_by/chapter on append (canvas_composer); the finisher uses them to determine
# whether chrome was rendered. added_by is the old lineage field (WP-A).
_META_PROTECTED_SUBKEYS = frozenset(
    {"added_by", "composed_by", "chapter", "folded_by", "provenance", "gap"}
)  # Batch B1: tool patches cannot alter folded_by/provenance/gap after first write


def merge_protected_meta(prev: dict[str, Any], safe_patch: dict[str, Any]) -> dict[str, Any]:
    """`_meta` 子键级合并：浅合并会让一次带 `_meta` 的 patch 整个顶掉 lineage 字段。
    受保护子键一律以已存值为准。`CanvasStore.patch_section` 与 `AtomicCanvasOps.flush`
    （`canvas_patch_section` 工具走的通道）共用，两条写入口一个口径。返回新的 safe_patch。"""
    if not (isinstance(safe_patch.get("_meta"), dict) and isinstance(prev.get("_meta"), dict)):
        return safe_patch
    merged = {**prev["_meta"], **safe_patch["_meta"]}
    for k in _META_PROTECTED_SUBKEYS:
        if k in prev["_meta"]:
            merged[k] = prev["_meta"][k]
    return {**safe_patch, "_meta": merged}


async def _figures_in_canvas(db: aiosqlite.Connection, canvas_id: str) -> dict[str, str]:
    """本画布里可被 `[figure:id]` 指向的图 ``{section_id: 标题}``。

    在调用方**同一事务**里读——图与正文在一次写入里先后落库时，分开取会有 TOCTOU。
    不过滤 superseded：B1-2 从不折叠仍被正文引用的图，折叠过的图照样是合法锚目标。
    """
    from bowhead_core.figure_anchor_layout import FIGURE_KINDS, figure_prose_title

    kinds = tuple(sorted(FIGURE_KINDS))
    placeholders = ",".join("?" * len(kinds))
    async with db.execute(
        f"SELECT id, kind, payload FROM canvas_sections WHERE canvas_id=? AND kind IN ({placeholders})",  # noqa: S608 — placeholders generated from the constant set's length
        (canvas_id, *kinds),
    ) as cur:
        rows = await cur.fetchall()
    out: dict[str, str] = {}
    for row in rows:
        try:
            payload = json.loads(row[2] or "{}")
        except Exception:
            payload = {}
        # Use the prose variant: missing title remains empty; mid-sentence anchors lose only their token. figure_title's kind fallback
        # would put machine terms like data_table into user sentences.
        out[str(row[0])] = figure_prose_title(payload, str(row[1]))
    return out


async def _normalized_payload(
    db: aiosqlite.Connection,
    canvas_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    content_touched: bool = True,
) -> dict[str, Any]:
    """写入前把正文里的图锚**位置归一**（`figure_anchor_layout`）。

    为什么在存储层：画布 PM 只提升段首锚，而句中/行尾/与上文同段的独占行都会原样漏成
    字面量；漏出来自三个不同写手（write_report / canvas_patch_section / 前端编辑），
    挂在任何单条写路上都会漏掉别的。设计见 tech-design-figure-anchor.md。
    没有错位锚时返回**原对象**，字节不变。
    """
    from bowhead_core.figure_anchor_layout import MARKDOWN_PAYLOAD_KINDS, normalize_figure_anchors

    if kind not in MARKDOWN_PAYLOAD_KINDS or not isinstance(payload, dict):
        return payload
    if not content_touched:
        # This write contains no body (only _meta/source_refs, or structural split/merge operations) →
        # **preserve every byte**. Previously this moved anchors without deleting them, but moving a mid-sentence anchor
        # replaces its token with the figure title; clicking split unexpectedly rewrote the user's sentence, worse than a leaked anchor.
        # Position healing belongs to actual body writes and the existing-data job (review round 3 #2).
        return payload
    md = payload.get("markdown")
    if not isinstance(md, str) or "[figure:" not in md:
        return payload
    try:
        figures = await _figures_in_canvas(db, canvas_id)
        new_md, stats = normalize_figure_anchors(md, figures)
    except Exception:
        logger.warning("figure_anchor.normalize_failed canvas=%s kind=%s — 原样落库", canvas_id, kind, exc_info=True)
        return payload
    if not stats["changed"]:
        return payload
    logger.info(
        "figure_anchor.normalized canvas=%s kind=%s moved=%d stripped=%d titled=%d",
        canvas_id,
        kind,
        stats["moved"],
        stats["stripped"],
        stats["titled"],
    )
    return {**payload, "markdown": new_md}


SCHEMA = """
CREATE TABLE IF NOT EXISTS canvases (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(session_id)
);

CREATE INDEX IF NOT EXISTS idx_canvases_session  ON canvases(session_id);
CREATE INDEX IF NOT EXISTS idx_canvases_ws_user  ON canvases(workspace_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS canvas_sections (
    id              TEXT PRIMARY KEY,
    canvas_id       TEXT NOT NULL REFERENCES canvases(id),
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,                       -- JSON
    rev             INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    quarantined     INTEGER NOT NULL DEFAULT 0,          -- i18n v1 R4: 1 = export 拒绝
    quarantine_reason TEXT,                               -- 标 quarantine 时的人话理由
    -- D6 (canvas-empty bug 2026-05-25): hide canvas section for chat-mode turns.
    -- Set after turn finalize when turn sections lack insight_card/recommendations
    -- → chat bubble inline_artifacts 仍能 surface 这些 sections; canvas tab 隐藏.
    -- surface_rationale: human-readable why (audit/admin/debug).
    hidden_for_chat_mode INTEGER NOT NULL DEFAULT 0,
    surface_rationale TEXT,
    -- surface_target: 单张图自己声明的去向（'chat'/'canvas'/NULL=未声明）。
    -- 与 surface_rationale 分开：后者是 human-readable 调试字段，且会被
    -- mark_turn_hidden_for_chat_mode 无条件覆写；声明必须留得住（迁移 0029）。
    surface_target TEXT,
    -- report-agent-authorship α v1 (2026-06-24): collapse-in-place. A prior turn's
    -- report block is marked `superseded` when a LATER report-worthy turn composes
    -- a fresh report (BOWHEAD_CANVAS_LATEST_WINS). DISTINCT from hidden_for_chat_mode
    -- (chat-mode hide): superseded sections are STILL returned by list_sections (NOT
    -- filtered) — the frontend renders them as a collapsed, expandable "previous
    -- report" header IN PLACE (not removed). So hydration stays consistent + the
    -- block is user-recoverable by expanding (no separate unhide route needed).
    -- superseded_by_turn = the later turn whose report superseded this one (audit).
    superseded INTEGER NOT NULL DEFAULT 0,
    superseded_by_turn TEXT,
    -- report-agent-authorship α v1 (2026-06-24): the turn that appended this section,
    -- denormalized from canvas_revisions.by_turn_id so the FE can GROUP sections by
    -- their originating turn (the history-collapse accordion shows one foldable entry
    -- per prior report: "第 N 轮 · <title> · N 段 · time"). NULL on legacy rows.
    turn_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_sections_canvas  ON canvas_sections(canvas_id, seq);
CREATE INDEX IF NOT EXISTS idx_sections_quarantine ON canvas_sections(quarantined) WHERE quarantined = 1;
-- D6 idx_sections_visible: created in initialize() AFTER _ensure_column for
-- hidden_for_chat_mode (legacy DB CREATE INDEX would fail if column missing).

CREATE TABLE IF NOT EXISTS canvas_revisions (
    id              TEXT PRIMARY KEY,
    canvas_id       TEXT NOT NULL,
    section_id      TEXT,
    op              TEXT NOT NULL,                       -- append | patch | reorder | delete
    prev_payload    TEXT,                                -- JSON or NULL
    new_payload     TEXT,                                -- JSON or NULL
    by_turn_id      TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_canvas ON canvas_revisions(canvas_id, created_at DESC);

-- Sprint ρ Phase A P0-1: count_sections_for_turn() hits canvas_revisions on
-- every post-flush chokepoint (lead_orchestrator callsite 4). Without an
-- index on by_turn_id we full-scan revisions per success turn.
CREATE INDEX IF NOT EXISTS idx_revisions_turn
    ON canvas_revisions(by_turn_id) WHERE by_turn_id IS NOT NULL;
"""


SectionOp = Literal["append", "patch", "reorder", "delete"]


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _nid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


# A2 (report-edit-audit): inline visual references in host payload. Same literal as report_final_gate /
# report_writer_loop _FIGURE_REF_RE ([figure:id] is the cross-module body-anchor syntax).
_INLINE_FIGURE_RE = re.compile(r"\[figure:([a-zA-Z0-9_]+)\]")


def _inline_visual_refs(payload: dict[str, Any]) -> list[str]:
    """宿主 payload 引用的视觉 section id（保持出现序、去重）。

    两个来源：text-based 正文里的 `[figure:id]` 锚，与 insight_card 的
    `evidence[].chart_section_id`（镜像 `canvas_export_projection._insight_visual_refs`）。
    """
    refs: list[str] = []
    seen: set[str] = set()

    def _add(ref: object) -> None:
        if isinstance(ref, str) and ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)

    for key in ("markdown", "text"):
        value = payload.get(key)
        if isinstance(value, str):
            for match in _INLINE_FIGURE_RE.finditer(value):
                _add(match.group(1))
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                _add(item.get("chart_section_id"))
    return refs


@dataclass
class Canvas:
    id: str
    session_id: str
    workspace_id: str
    user_id: str
    title: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass
class Section:
    id: str
    canvas_id: str
    seq: int
    kind: str
    payload: dict[str, Any]
    rev: int
    created_at: str
    updated_at: str
    # i18n v1 R4: quarantine flag (historical numeric errors, off-by-N scales, etc.)
    # 1 = export pipeline rejects; 0 = normal
    quarantined: bool = False
    quarantine_reason: str | None = None
    # D6 (2026-05-25): hidden from canvas tab when turn is chat-mode
    # (no insight_card / recommendations). Section still queryable by
    # inline_artifacts wire so chat bubble surfaces it.
    hidden_for_chat_mode: bool = False
    surface_rationale: str | None = None
    surface_target: str | None = None
    # report-engine Phase-2 WP-A: writing peer's unique id (server-stamped);
    # None = unowned/claimable (legacy rows + non-report writers).
    owner: str | None = None
    # report-agent-authorship α v1 (2026-06-24): collapse-in-place marker — a prior
    # report block superseded by a later report-worthy turn. NOT filtered from
    # list_sections; surfaced to FE (to_report_section) so it renders collapsed.
    superseded: bool = False
    superseded_by_turn: str | None = None
    # α v1: originating turn (denormalized from canvas_revisions.by_turn_id) so the FE
    # can group superseded sections by their report-turn in the history accordion.
    turn_id: str | None = None

    def to_report_section(self) -> dict[str, Any]:
        """前端 ReportSection 形态：payload 打平，再带上 id + kind + rev.

        α v1: 带上 `superseded`（仅 True 时）让前端把被取代的旧报告块渲成可展开
        折叠头（collapse-in-place，不从视图移除）；`turn_id` 让前端按轮分组历史报告。"""
        out: dict[str, Any] = dict(self.payload)
        out.update({"id": self.id, "kind": self.kind, "rev": self.rev})
        if self.superseded:
            out["superseded"] = True
        if self.turn_id:
            out["turn_id"] = self.turn_id
        return out


class CanvasRevConflictError(RuntimeError):
    """Raised when a compare-and-set patch sees a newer section revision."""

    def __init__(self, current_rev: int) -> None:
        super().__init__(f"canvas section rev conflict: current_rev={current_rev}")
        self.current_rev = current_rev


class CanvasNotOwnerError(RuntimeError):
    """report-engine Phase-2 WP-A: raised when a caller tries to patch a section
    it does not own. Ownership is server-stamped from the writing peer's unique
    id at section creation; a section with NULL owner (legacy / unclaimed) is
    editable by anyone (claimable). The store enforces this when `caller` is
    passed; callers that pass no `caller` (HTTP route / legacy ops) skip the
    check (backward-compatible)."""

    def __init__(self, owner: str | None) -> None:
        super().__init__(f"canvas section not owned by caller: owner={owner}")
        self.owner = owner


class CanvasSectionSupersededError(RuntimeError):
    """canvas-update-turn 批 B0（review 修）：``patch_section(reject_superseded=True)`` 在**同一事务**里
    看到目标段已折叠进 History 时抛出。写路此前只在事务外按快照判 superseded（TOCTOU：plan 轮
    ``on_step_composed`` 的折叠会插在快照与 patch 之间，折叠段里出现新鲜正文）。"""

    def __init__(self, section_id: str) -> None:
        super().__init__(f"canvas section superseded: {section_id}")
        self.section_id = section_id


class CanvasRestoreUnsupportedError(RuntimeError):
    """方案A (report-edit-audit): delete revision 是旧格式 (裸 payload, 无恢复
    envelope) —— 行级元数据 (kind/seq/owner/…) 已随删除丢失, 无法可靠重建, restore
    明确拒绝而不是猜。仅影响 envelope 上线前的历史删除记录。"""


class CanvasSplitDegenerateError(RuntimeError):
    """affordance r3 §5.2: offset 落在 markdown 边界 (<=0 或 >=len) 的 split 会
    产出一半空壳——原实现钳制后照做, 前端菜单夺焦 bug 曾以 offset=0 连环制造空
    section。改为显式拒绝, 路由映射 422。"""

    def __init__(self, *, offset: int, length: int) -> None:
        super().__init__(f"degenerate split: offset={offset} length={length}")
        self.offset = offset
        self.length = length


@dataclass
class Revision:
    id: str
    canvas_id: str
    section_id: str | None
    op: SectionOp
    prev_payload: dict[str, Any] | None
    new_payload: dict[str, Any] | None
    by_turn_id: str | None
    created_at: str
    #: This change's AI turn ordinal within the entire report (starting at 1); manual edits/machine cleanup have None.
    #: Populated only by CanvasStore.list_revisions; see _turn_ordinals for rules.
    turn_ordinal: int | None = None


#: Turn sentinel for **machine** reorders in revision ledger (batch jobs/runtime missing-turn_id fallback).
#: Omitting it leaves NULL by_turn_id, misclassified by CanvasStore.has_manual_reorder
#: as user dragging, permanently skipping canonical sorting: the server locks itself out.
MACHINE_REORDER_TURN_ID = "machine:reorder"

#: by_turn_id with this prefix denotes machine cleanup (batch jobs), not AI turns; same as frontend MACHINE_TURN_PREFIX.
MACHINE_TURN_PREFIX = "machine:"

#: First write time of each AI turn across **all** Canvas changes (one aggregation using idx_revisions_canvas).
#: Empty/NULL (manual editing) and machine: prefix (machine cleanup) are not turns.
#: Use substr, not LIKE: SQLite LIKE is ASCII case-insensitive, unlike frontend startsWith.
_TURN_FIRST_WRITE_SQL = (
    "SELECT by_turn_id, MIN(created_at) AS first_at FROM canvas_revisions "
    "WHERE canvas_id=? AND by_turn_id IS NOT NULL AND by_turn_id <> '' "
    "AND substr(by_turn_id, 1, ?) <> ? "
    "GROUP BY by_turn_id"
)


def _turn_ordinals(first_writes: Iterable[tuple[str, str]]) -> dict[str, int]:
    """``(by_turn_id, 首写时间)`` → 整份报告内的 AI 轮次序号（从 1 起）。

    按首写时间升序编号；``created_at`` 只精确到秒，同刻用 ``by_turn_id`` 定序，
    结果与输入顺序无关（排序键写在这里而不是 SQL 的 ORDER BY 上，便于直接验证）。
    """
    ordered = sorted(first_writes, key=lambda pair: (pair[1], pair[0]))
    return {turn_id: index for index, (turn_id, _) in enumerate(ordered, start=1)}


@dataclass
class CanvasStore:
    db_path: str | Path
    _path: str = field(init=False)

    def __post_init__(self) -> None:
        self._path = str(self.db_path)
        # V4 Sprint 2 PRD-D-4-1: optional invalidation hook (injected by gateway main.py)
        # Signature: async (event_kind, canvas_id, section_id|None, new_payload|None) -> None
        # event_kind ∈ {"canvas_deleted", "section_deleted", "payload_changed"}
        self._anchor_invalidation_hook = None  # type: ignore[assignment]
        # R4: FTS5 availability (True only after initialize creates the virtual table); unavailable search falls back to title/id LIKE.
        self._fts_enabled = True

    async def initialize(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self._path) as db:
            # i18n v1 R4: old-database ALTER must precede SCHEMA's CREATE INDEX...quarantined,
            # or creating the partial index on quarantined fails.
            for stmt in [
                "ALTER TABLE canvas_sections ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE canvas_sections ADD COLUMN quarantine_reason TEXT",
            ]:
                try:  # noqa: SIM105
                    await db.execute(stmt)
                except Exception:  # noqa: S110
                    pass  # Column exists (new install/migrated), or table absent (new install uses SCHEMA)
            await db.executescript(SCHEMA)
            # Sprint ρ Phase A P0-1: explicit idempotent migration so existing
            # prod DBs (created before SCHEMA carried `idx_revisions_turn`)
            # pick up the index without a separate migration step. IF NOT EXISTS
            # makes it safe to run on every boot.
            await db.execute("CREATE INDEX IF NOT EXISTS idx_revisions_turn ON canvas_revisions(by_turn_id) WHERE by_turn_id IS NOT NULL")
            # Sprint ω Phase 6 (r3.1 §2.12 + SRE P1-S2 invariant):
            # schema_version column for forward-compat read path. r3.1 evidence:
            # SQLite has NEVER supported `ADD COLUMN IF NOT EXISTS` (PostgreSQL-only),
            # so use PRAGMA sniff (Eng-verified r3.1) before ALTER. DEFAULT MUST
            # be a constant literal (O(1) metadata-only); current `DEFAULT 1` is safe.
            await self._ensure_column(
                db,
                "canvas_sections",
                "schema_version",
                "INTEGER NOT NULL DEFAULT 1",
            )
            await self._ensure_column(
                db,
                "canvas_revisions",
                "schema_version",
                "INTEGER NOT NULL DEFAULT 1",
            )
            # D6 (canvas-empty bug 2026-05-25): hidden_for_chat_mode flag +
            # surface_rationale audit. Old DBs ALTER; new DBs already in SCHEMA.
            await self._ensure_column(
                db,
                "canvas_sections",
                "hidden_for_chat_mode",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self._ensure_column(
                db,
                "canvas_sections",
                "surface_rationale",
                "TEXT",
            )
            # Candidate 4 (migration 0029): individual figure's declared target; maintained with versions/0029, both paths idempotent.
            await self._ensure_column(
                db,
                "canvas_sections",
                "surface_target",
                "TEXT",
            )
            # report-engine Phase-2 WP-A: per-section `owner` (write-tool peer's
            # unique id, server-stamped at creation). NULL on legacy rows =
            # claimable/unowned (editable by anyone) so old reports stay editable
            # — see patch_section's caller check. `canvas_revisions.owner` mirrors
            # it so "who wrote this number" is auditable from the ledger
            # (security re-review P1: anti-evidence-tampering must be queryable, not
            # only enforced at write time).
            await self._ensure_column(db, "canvas_sections", "owner", "TEXT")
            await self._ensure_column(db, "canvas_revisions", "owner", "TEXT")
            # report-agent-authorship α v1 (2026-06-24): collapse-in-place superseded
            # marker. Old DBs ALTER; new DBs already in SCHEMA. DEFAULT 0 = O(1) meta.
            await self._ensure_column(
                db,
                "canvas_sections",
                "superseded",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self._ensure_column(db, "canvas_sections", "superseded_by_turn", "TEXT")
            # α v1: denormalized originating turn (for the FE history-collapse grouping).
            await self._ensure_column(db, "canvas_sections", "turn_id", "TEXT")
            # empty-report-library (2026-08-25): compose-time verdict on substantive output this turn.
            # substantive / empty / NULL (undecided). **Record only; no read-side behavior change**:
            # report library keeps displaying as before; after enough observations/accuracy measurements, separately decide whether to filter.
            # Why compose time: inferring from Canvas shapes later errs both ways (prose writers put tables/charts/
            # recommendations in narrative → reject real reports; agents produce KPI cards around no-data claims →
            # admit empty reports). Assessment: docs/sprints/empty-report-library-2026-08-25/.
            # Maintained with migrations/versions/0023; both paths idempotent (as with 0021 artifacts).
            await self._ensure_column(db, "canvases", "content_status", "TEXT")
            with contextlib.suppress(Exception):
                await db.execute("CREATE INDEX IF NOT EXISTS idx_sections_visible ON canvas_sections(canvas_id, hidden_for_chat_mode, seq)")
            # R4: FTS5 full-text index (trigram tokenizer: CJK/substrings, case-insensitive).
            # canvas_id/section_id are UNINDEXED (stored only); body contains harvested searchable text.
            # Missing fts5 compilation/trigram support (SQLite<3.34) disables it; search falls back to LIKE.
            try:
                await db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS canvas_fts USING fts5(canvas_id UNINDEXED, section_id UNINDEXED, body, tokenize='trigram')"
                )
                await self._backfill_fts(db)
            except Exception as e:
                self._fts_enabled = False
                logger.warning("canvas_fts unavailable, search falls back to LIKE: %s", e)
            await db.commit()

    async def _backfill_fts(self, db: Any) -> None:
        """首次建表(或索引为空)时, 从 canvas_sections 回填 FTS. 幂等: 仅当 fts 空且有 section 时回填."""
        async with db.execute("SELECT COUNT(*) FROM canvas_fts") as cur:
            fts_n = int((await cur.fetchone())[0])
        if fts_n > 0:
            return
        async with db.execute("SELECT id, canvas_id, payload FROM canvas_sections") as cur:
            rows = await cur.fetchall()
        n = 0
        for sid, cid, payload_json in rows:
            try:
                payload = json.loads(payload_json or "{}")
            except (ValueError, TypeError):
                continue
            body = _section_search_text(payload) if isinstance(payload, dict) else ""
            if body:
                await db.execute(
                    "INSERT INTO canvas_fts(canvas_id, section_id, body) VALUES(?,?,?)",
                    (cid, sid, body),
                )
                n += 1
        if n:
            logger.info("canvas_fts backfilled %d sections", n)

    async def _fts_sync(
        self,
        db: Any,
        *,
        canvas_id: str,
        section_id: str,
        payload: dict[str, Any],
    ) -> None:
        """同事务内重建一个 section 的 FTS 行(delete+insert). FTS 是索引非真值, 失败只 warn."""
        if not self._fts_enabled:
            return
        try:
            await db.execute("DELETE FROM canvas_fts WHERE section_id=?", (section_id,))
            body = _section_search_text(payload)
            if body:
                await db.execute(
                    "INSERT INTO canvas_fts(canvas_id, section_id, body) VALUES(?,?,?)",
                    (canvas_id, section_id, body),
                )
        except Exception as e:
            logger.warning("fts_sync failed section=%s err=%s", section_id, e)

    async def _fts_remove(self, db: Any, section_id: str) -> None:
        if not self._fts_enabled:
            return
        try:
            await db.execute("DELETE FROM canvas_fts WHERE section_id=?", (section_id,))
        except Exception as e:
            logger.warning("fts_remove failed section=%s err=%s", section_id, e)

    @staticmethod
    async def _ensure_column(db: Any, table: str, col: str, decl: str) -> None:
        """Idempotent ADD COLUMN via PRAGMA sniff. r3.1 §2.12 — SQLite has no
        `ADD COLUMN IF NOT EXISTS`, so we check first then add. DEFAULT MUST be
        a constant literal (O(1) metadata-only); reject `DEFAULT <expr>`."""
        cur = await db.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
            (table, col),
        )
        row = await cur.fetchone()
        if row is None:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # ─── canvas ────────────────────────────────────────────
    async def ensure_canvas(
        self,
        *,
        session_id: str,
        workspace_id: str,
        user_id: str,
        title: str | None = None,
    ) -> Canvas:
        """1 session = 1 canvas。已存在则返回，否则新建."""
        existing = await self.get_canvas_by_session(session_id)
        if existing:
            return existing
        now = _now()
        cid = _nid("cv")
        try:
            async with open_shared_ctx(self._path) as db:
                await db.execute(
                    "INSERT INTO canvases(id, session_id, workspace_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cid, session_id, workspace_id, user_id, title, now, now),
                )
                await db.commit()
        except aiosqlite.IntegrityError:
            # get→INSERT has a TOCTOU window: concurrent first creation (e.g. N writers ensure during T7 slotless fallback)
            # all read None and then INSERT; UNIQUE(session_id) allows only one winner.
            # Losers reread the winner's row instead of raising IntegrityError (which would silently lose a writer's chapter).
            raced = await self.get_canvas_by_session(session_id)
            if raced:
                return raced
            raise
        return Canvas(
            id=cid,
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user_id,
            title=title,
            status="active",
            created_at=now,
            updated_at=now,
        )

    async def _account_visibility_sql(self, alias: str = "canvases") -> str:
        """Hide retired owners immediately, including reports shared before retirement."""
        async with (
            open_shared_ctx(self._path) as db,
            db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='account_deletions'") as cur,
        ):
            if await cur.fetchone() is None:
                return "1"
        return f"NOT EXISTS(SELECT 1 FROM account_deletions retired WHERE retired.user_id={alias}.user_id)"

    async def get_canvas(self, canvas_id: str) -> Canvas | None:
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM canvases WHERE id=? AND {await self._account_visibility_sql()}",
                (canvas_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_canvas(row) if row else None

    async def session_ids_for_canvases(self, canvas_ids: list[str]) -> dict[str, str]:
        """`get_canvas(cid).session_id` 的批量版：``{canvas_id: session_id}``；不存在的报告不在字典里。

        与单条版同一张表、同样不加任何过滤。给一次要推很多产物归属的调用方用。
        """
        ids = list(dict.fromkeys(cid for cid in canvas_ids if cid))
        out: dict[str, str] = {}
        for start in range(0, len(ids), 500):  # Far below SQLite's bound-parameter limit
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            async with (
                open_shared_ctx(self._path) as db,
                db.execute(f"SELECT id, session_id FROM canvases WHERE id IN ({placeholders})", chunk) as cur,  # noqa: S608 - placeholders only
            ):
                for canvas_id, session_id in await cur.fetchall():
                    out[str(canvas_id)] = session_id
        return out

    async def get_canvas_by_session(self, session_id: str) -> Canvas | None:
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM canvases WHERE session_id=? AND {await self._account_visibility_sql()}",
                (session_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_canvas(row) if row else None

    async def read_canvas_projection_state(
        self,
        canvas_id: str,
    ) -> tuple[Canvas, list[Section]] | None:
        """Read Canvas metadata and all lifecycle rows from one SQLite snapshot."""
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            async with db.execute(
                f"SELECT * FROM canvases WHERE id=? AND {await self._account_visibility_sql()}",
                (canvas_id,),
            ) as cur:
                canvas_row = await cur.fetchone()
            if canvas_row is None:
                await db.rollback()
                return None
            async with db.execute(
                "SELECT * FROM canvas_sections WHERE canvas_id=? ORDER BY seq, id",
                (canvas_id,),
            ) as cur:
                section_rows = await cur.fetchall()
            await db.commit()
        return _row_to_canvas(canvas_row), [_row_to_section(row) for row in section_rows]

    async def set_title(self, canvas_id: str, title: str) -> None:
        async with open_shared_ctx(self._path) as db:
            await db.execute(
                "UPDATE canvases SET title=?, updated_at=? WHERE id=?",
                (title, _now(), canvas_id),
            )
            await db.commit()

    async def read_retained_projection(self, *, session_id: str, turn_id: str) -> dict[str, Any] | None:
        """Bound the owned projection and complete revision ledger in one read.

        Reorder/delete entries remain evidence even without a surviving section.
        This is a material reader, not authorization to edit the Canvas.
        """
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            async with db.execute(f"SELECT * FROM canvases WHERE session_id=? AND {await self._account_visibility_sql()}", (session_id,)) as cur:
                canvases = await cur.fetchall()
            async with db.execute("SELECT DISTINCT canvas_id FROM canvas_revisions WHERE by_turn_id=?", (turn_id,)) as cur:
                touched = {row[0] for row in await cur.fetchall()}
            if not canvases:
                if touched:
                    raise ValueError("ordinary_canvas_material_unavailable")
                return None
            if len(canvases) != 1 or touched - {canvases[0]["id"]}:
                raise ValueError("ordinary_canvas_material_unavailable")
            canvas = dict(canvases[0])
            async with db.execute(
                "SELECT (SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM canvas_sections WHERE canvas_id=?) + "
                "(SELECT COALESCE(SUM(COALESCE(LENGTH(CAST(prev_payload AS BLOB)),0)+COALESCE(LENGTH(CAST(new_payload AS BLOB)),0)),0) "
                "FROM canvas_revisions WHERE canvas_id=?)",
                (canvas["id"], canvas["id"]),
            ) as cur:
                if (await cur.fetchone())[0] > 4 * 1024 * 1024:
                    raise ValueError("ordinary_canvas_material_unavailable")
            async with db.execute("SELECT * FROM canvas_sections WHERE canvas_id=? ORDER BY seq,id LIMIT 1025", (canvas["id"],)) as cur:
                sections = [dict(row) for row in await cur.fetchall()]
            async with db.execute("SELECT * FROM canvas_revisions WHERE canvas_id=? ORDER BY created_at,id LIMIT 4097", (canvas["id"],)) as cur:
                revisions = [dict(row) for row in await cur.fetchall()]
            if len(sections) > 1024 or len(revisions) > 4096:
                raise ValueError("ordinary_canvas_material_unavailable")
            return {"canvas": canvas, "sections": sections, "revisions": revisions}

    async def set_status(self, canvas_id: str, status: str) -> bool:
        """R2: 改报告生命周期状态(active/verified/stale/archived). 命中一行返 True.

        owner 校验在路由层(_load_owned), 此处只做枚举校验 + 写库, 对齐 set_title 风格。
        archived 的报告不进 AI/KB 检索(由 list_for_user(include_archived=False) 落地)。
        """
        if status not in CANVAS_STATUSES:
            raise ValueError(f"invalid canvas status {status!r}; must be one of {CANVAS_STATUSES}")
        async with open_shared_ctx(self._path) as db:
            cur = await db.execute(
                "UPDATE canvases SET status=?, updated_at=? WHERE id=?",
                (status, _now(), canvas_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # ─── sections ──────────────────────────────────────────
    async def list_sections(
        self,
        canvas_id: str,
        *,
        include_hidden: bool = False,
    ) -> list[Section]:
        """D6 (2026-05-25): default filter hides chat-mode sections from
        canvas tab. Pass `include_hidden=True` for admin / replay / hide
        decision logic that needs the full per-turn section list.
        """
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            if include_hidden:
                sql = "SELECT * FROM canvas_sections WHERE canvas_id=? ORDER BY seq"
                params: tuple[Any, ...] = (canvas_id,)
            else:
                sql = "SELECT * FROM canvas_sections WHERE canvas_id=? AND hidden_for_chat_mode=0 ORDER BY seq"
                params = (canvas_id,)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_section(r) for r in rows]

    async def list_export_projection(self, canvas_id: str) -> list[Section]:
        """Return persisted sections in CurrentCanvasContentSet v1.

        This is intentionally separate from ``list_sections``: the page still
        needs superseded rows for its expandable history, while V2 export is
        explicitly scoped to the current version.
        """
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM canvas_sections WHERE canvas_id=? AND hidden_for_chat_mode=0 AND superseded=0 ORDER BY seq, id",
                (canvas_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_section(row) for row in rows]

    async def set_content_status(self, canvas_id: str, status: str | None) -> None:
        """记下 compose 期对「这一轮有没有实质产出」的判定（empty-report-library）。

        `status` 为 `None` ＝ 判据没把握，**保持原值不动**（别把「未判定」写成一个结论，
        也别覆盖掉上一轮已经下过的判断）。只在有结论时落库。

        本方法**只写不读**：报告库的行为这一批不变（先记录、后使用）。
        """
        if not status:
            return
        async with open_shared_ctx(self._path) as db:
            await db.execute(
                "UPDATE canvases SET content_status=?, updated_at=? WHERE id=?",
                (status, _now(), canvas_id),
            )
            await db.commit()

    async def mark_turn_hidden_for_chat_mode(
        self,
        turn_id: str,
        rationale: str,
    ) -> list[str]:
        """D6 (2026-05-25): mark all sections appended by `turn_id` as hidden
        from canvas tab. Used at turn finalize when the turn is chat-mode
        (no insight_card / recommendations).

        Returns list of section_ids marked hidden (for SSE done event payload).
        Sections remain in DB + still surface via inline_artifacts wire to
        chat bubble; only the canvas tab listing filters them out.
        """
        if not turn_id:
            return []
        async with open_shared_ctx(self._path) as db:
            # Identify sections via canvas_revisions ledger (same pattern as
            # list_sections_appended_by_turn). Only `op='append'` ops count.
            # ⚠ Exclude sections **explicitly declaring target=canvas**: the agent assigned this figure to Canvas
            # when drawing it; the end-of-turn evidence fallback must not overturn that (candidate 4 §4.1:
            # target can narrow exposure, never widen it; conversely turn-level decisions cannot suppress explicit Canvas targeting).
            # NULL (undeclared) and chat still participate in batch marking as before.
            async with db.execute(
                "SELECT s.id FROM canvas_sections s JOIN canvas_revisions r ON r.section_id = s.id "
                "WHERE r.by_turn_id = ? AND r.op = 'append' "
                "AND (s.surface_target IS NULL OR s.surface_target != 'canvas')",
                (turn_id,),
            ) as cur:
                rows = await cur.fetchall()
            section_ids = [row[0] for row in rows]
            if not section_ids:
                return []
            # Bulk update
            placeholders = ",".join("?" for _ in section_ids)
            await db.execute(
                f"UPDATE canvas_sections SET hidden_for_chat_mode=1, surface_rationale=?, updated_at=? WHERE id IN ({placeholders})",
                (rationale, _now(), *section_ids),
            )
            await db.commit()
        return section_ids

    async def unhide_sections_for_report(
        self,
        section_ids: list[str],
        rationale: str,
    ) -> list[str]:
        """Bring individual chat-hidden sections back onto the canvas because a
        report written in a later turn anchors them with `[figure:id]`.

        D6 hides every section of a chat-delivered turn. When the user follows up
        with "turn the analysis above into a report", the writer correctly cites
        those sections by their real ids, and they must become visible again
        under the same id (116 `t_6846eb7b274a`: four such anchors were rejected
        as unknown and the turn redrew the same charts).

        Unlike `unhide_turn`, this is keyed by section and is driven by an
        explicit reference in the user's report, so it also lifts sections the
        agent had declared `surface_target='chat'`. Superseded sections are
        never lifted. Returns only the ids that actually transitioned.
        """
        ids = [s for s in (section_ids or []) if s]
        if not ids:
            return []
        async with open_shared_ctx(self._path) as db:
            placeholders = ",".join("?" for _ in ids)
            async with db.execute(
                f"SELECT id FROM canvas_sections WHERE id IN ({placeholders}) AND hidden_for_chat_mode = 1 AND superseded = 0",
                tuple(ids),
            ) as cur:
                lifted = [row[0] for row in await cur.fetchall()]
            if not lifted:
                return []
            lifted_ph = ",".join("?" for _ in lifted)
            await db.execute(
                f"UPDATE canvas_sections SET hidden_for_chat_mode=0, surface_rationale=?, updated_at=? WHERE id IN ({lifted_ph})",
                (rationale, _now(), *lifted),
            )
            await db.commit()
        return lifted

    async def mark_sections_superseded(
        self,
        *,
        section_ids: list[str],
        superseded_by_turn: str | None = None,
    ) -> list[str]:
        """report-agent-authorship α v1 (2026-06-24): collapse-in-place. Mark the
        given sections as `superseded` (a prior report block replaced by a later
        report-worthy turn). DISTINCT from `mark_turn_hidden_for_chat_mode`:
          - superseded sections are STILL returned by ``list_sections`` (NOT
            filtered) — the FE renders them as a collapsed expandable header in
            place, so the block stays user-recoverable + hydration-consistent.
          - keyed by explicit section_ids (the orchestrator computes "all visible
            minus this turn's"), not by turn — so a single later turn can fold the
            accumulated prior blocks of several earlier turns at once.

        Idempotent: only sections currently ``superseded=0`` are updated; returns
        ONLY the newly-superseded ids (so the caller emits the SSE collapse event
        just for blocks that actually transitioned — already-folded blocks are
        already collapsed on the FE). Empty input / no transition → ``[]``.
        """
        ids = [s for s in (section_ids or []) if s]
        if not ids:
            return []
        async with open_shared_ctx(self._path) as db:
            placeholders = ",".join("?" for _ in ids)
            # SELECT-first so we return exactly the ids that transition 0→1 (the
            # bulk UPDATE's rowcount can't tell us WHICH rows changed).
            async with db.execute(
                f"SELECT id FROM canvas_sections WHERE id IN ({placeholders}) AND superseded = 0",
                tuple(ids),
            ) as cur:
                fresh = [row[0] for row in await cur.fetchall()]
            if not fresh:
                return []
            fresh_ph = ",".join("?" for _ in fresh)
            await db.execute(
                f"UPDATE canvas_sections SET superseded=1, superseded_by_turn=?, updated_at=? WHERE id IN ({fresh_ph})",
                (superseded_by_turn, _now(), *fresh),
            )
            await db.commit()
        return fresh

    async def set_section_surface_target(
        self,
        section_id: str,
        target: str,
    ) -> None:
        """候选 4：把**单张图**自己声明的去向落库。

        `target='chat'` 顺带置 `hidden_for_chat_mode=1` —— 那是「不在画布上露面」的
        既有语义，且**必须落库**而不只是压事件：用户轮次中途 F5 走的是 canvas API
        （按 `hidden_for_chat_mode` 过滤），只压 SSE 的话刷新后图照样冒出来。

        `target='canvas'` **不动** `hidden_for_chat_mode`：它的作用是让轮末的
        `mark_turn_hidden_for_chat_mode` 跳过这一段（见那里的 SQL 条件）。
        """
        if not section_id or target not in ("chat", "canvas"):
            return
        async with open_shared_ctx(self._path) as db:
            if target == "chat":
                await db.execute(
                    "UPDATE canvas_sections SET surface_target=?, hidden_for_chat_mode=1, updated_at=? WHERE id=?",
                    (target, _now(), section_id),
                )
            else:
                await db.execute(
                    "UPDATE canvas_sections SET surface_target=?, updated_at=? WHERE id=?",
                    (target, _now(), section_id),
                )
            await db.commit()

    async def unhide_turn(
        self,
        turn_id: str,
        rationale: str | None = None,
    ) -> list[str]:
        """D6 (2026-05-25): reverse of `mark_turn_hidden_for_chat_mode`.

        Reserved for future "promote to canvas" feature (Lead LLM override
        token / admin endpoint). Not wired to Lead in v1; admin can call
        via /admin/canvas/turn/{id}/promote.
        """
        if not turn_id:
            return []
        async with open_shared_ctx(self._path) as db:
            async with db.execute(
                # ⚠ AND s.surface_target IS NULL: never overturn explicit per-figure declarations.
                # Reversal remedies a mistaken turn-level verdict; a figure's target was declared by the agent at drawing time,
                # so a revised turn verdict must not promote it onto Canvas (candidate 4 §4.5).
                "SELECT s.id FROM canvas_sections s "
                "JOIN canvas_revisions r ON r.section_id = s.id "
                "WHERE r.by_turn_id = ? AND r.op = 'append' "
                "AND s.hidden_for_chat_mode = 1 "
                "AND s.surface_target IS NULL",
                (turn_id,),
            ) as cur:
                rows = await cur.fetchall()
            section_ids = [row[0] for row in rows]
            if not section_ids:
                return []
            placeholders = ",".join("?" for _ in section_ids)
            await db.execute(
                f"UPDATE canvas_sections SET hidden_for_chat_mode=0, surface_rationale=?, updated_at=? WHERE id IN ({placeholders})",
                (rationale or "manually_promoted", _now(), *section_ids),
            )
            await db.commit()
        return section_ids

    async def list_section_ids_by_kind(
        self,
        canvas_id: str,
        *,
        kind: str,
    ) -> set[str]:
        """Sprint ν1 — 返指定 kind 的 section ID set. validate 用.

        比 list_sections 轻 (只 SELECT id, 不 row_to_section).
        """
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT id FROM canvas_sections WHERE canvas_id=? AND kind=?",
                (canvas_id, kind),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return {row[0] for row in rows}

    async def append_section(
        self,
        *,
        canvas_id: str,
        kind: str,
        payload: dict[str, Any],
        by_turn_id: str | None = None,
        owner: str | None = None,
    ) -> Section:
        # report-engine Phase-2 WP-A: `owner` is the writing peer's unique id
        # (the report-write handler resolves it from get_current_peer_name(); the
        # store stays orchestrator-agnostic and just persists what it's given).
        # None = unowned/claimable (legacy + non-report callers).
        #
        # WP-A.2: `BEGIN IMMEDIATE` as the first statement takes the DB write lock
        # up front, so the MAX(seq)+1 read → INSERT is serialized against any
        # concurrent appender (which waits up to busy_timeout=5000 from
        # open_shared_ctx, then reads the now-updated MAX) — closes the seq
        # TOCTOU the re-review flagged (two connections each reading MAX before
        # either inserts → duplicate seq). PRAGMAs from open_shared_ctx are
        # non-DML (no open txn), so explicit BEGIN here is valid — same proven
        # shape as AtomicCanvasOps.flush, but with busy_timeout (which that path
        # lacks). open_shared_ctx closes the conn on any error → implicit rollback.
        now = _now()
        sid = _nid("sec")
        if kind == "chart":
            validate_stored_composition(payload.get("spec"))
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM canvas_sections WHERE canvas_id=?",
                (canvas_id,),
            ) as cur:
                r = await cur.fetchone()
            seq = int(r[0]) if r else 1
            payload = await _normalized_payload(db, canvas_id, kind, payload)
            await db.execute(
                "INSERT INTO canvas_sections(id, canvas_id, seq, kind, payload, owner, turn_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, canvas_id, seq, kind, json.dumps(payload, ensure_ascii=False), owner, by_turn_id, now, now),
            )
            await db.execute(
                "UPDATE canvases SET updated_at=? WHERE id=?",
                (now, canvas_id),
            )
            await db.execute(
                "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, owner, created_at) "
                "VALUES (?, ?, ?, 'append', NULL, ?, ?, ?, ?)",
                (_nid("rv"), canvas_id, sid, json.dumps(payload, ensure_ascii=False), by_turn_id, owner, now),
            )
            await self._fts_sync(db, canvas_id=canvas_id, section_id=sid, payload=payload)  # R4
            await db.commit()
        return Section(
            id=sid,
            canvas_id=canvas_id,
            seq=seq,
            kind=kind,
            payload=payload,
            rev=1,
            created_at=now,
            updated_at=now,
            owner=owner,
            turn_id=by_turn_id,
        )

    async def patch_section(
        self,
        *,
        section_id: str,
        patch: dict[str, Any],
        expected_rev: int | None = None,
        by_turn_id: str | None = None,
        caller: str | None = None,
        skip_owner_check: bool = False,
        canvas_id: str | None = None,
        reject_superseded: bool = False,
    ) -> Section | None:
        """浅合并 payload。未知 section 返回 None.

        canvas-update-turn 批 B0：``skip_owner_check=True`` 只跳过 owner 互斥校验（跨轮修订：
        上一轮写手已退出，没有并发面），``caller`` 照传 —— revision 行的 ``owner`` 列记的是
        caller，账本要记真实写手（review P1：``caller=None`` 会把它写成 NULL）。
        B0 review 修：``canvas_id`` 给了就只在这块画布里找段——此前按裸 id 查表，工具面传别的
        会话的 section_id 也能改到（owner 为 NULL 的图/chrome 段尤甚）；不在本画布 ⇒ 返 None
        （调用方译成 section_not_found）。``reject_superseded=True`` 在同一事务里拒已折叠的段
        （快照判定有 TOCTOU 窗口）。两个参数缺省都不改旧行为。

        report-engine Phase-2 WP-A — ownership enforcement (SELECT-first, then
        branch, so not_owner / rev_conflict / not_found stay distinguishable;
        a single `WHERE id=? AND rev=? AND owner=?` could not tell them apart):
          - `caller` given + section has a non-NULL `owner` that differs →
            ``CanvasNotOwnerError``. (`owner` is immutable, so it's safe to
            check once here and not fold into the rev CAS.)
          - NULL `owner` (legacy/unclaimed) → editable by anyone (no error).
          - `caller=None` (HTTP route / legacy ops) → ownership check skipped
            (backward-compatible; those paths gate ownership at the canvas/user
            layer via `_load_owned`).
        Then the existing `expected_rev` compare-and-set is unchanged.
        """
        now = _now()
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            if canvas_id:
                _sel = db.execute(
                    "SELECT * FROM canvas_sections WHERE id=? AND canvas_id=?",
                    (section_id, canvas_id),
                )
            else:
                _sel = db.execute(
                    "SELECT * FROM canvas_sections WHERE id=?",
                    (section_id,),
                )
            async with _sel as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            # ownership pre-check (immutable owner → check once, not in the CAS)
            section_owner = row["owner"]
            if caller is not None and not skip_owner_check and section_owner is not None and section_owner != caller:
                raise CanvasNotOwnerError(section_owner)
            if reject_superseded and bool(row["superseded"]):
                raise CanvasSectionSupersededError(section_id)
            current_rev = int(row["rev"])
            if expected_rev is not None and current_rev != expected_rev:
                raise CanvasRevConflictError(current_rev)
            prev = json.loads(row["payload"])
            # WP3 review #7: strip structural/identity fields clients must never change; prevent section PATCH from putting
            # kind (type confusion)/id/rev (identity/version) into payload. Owners may still edit content fields
            # (markdown/spec/source_refs etc.); write access is already owner-only (_load_owned), so this is
            # defense in depth, not a security boundary. Finer per-kind content allowlists are deferred.
            safe_patch = merge_protected_meta(prev, {k: v for k, v in patch.items() if k not in _PATCH_PROTECTED_FIELDS})
            new = {**prev, **safe_patch}
            if row["kind"] == "chart" and "spec" in safe_patch:
                validate_stored_composition(new.get("spec"))
            new_rev = current_rev + 1
            canvas_id = row["canvas_id"]
            # content_touched: did this patch actually include body? Writes changing only _meta/source_refs
            # must not delete body content (review #7), but may still reposition misplaced anchors (move only, no deletion).
            new = await _normalized_payload(db, canvas_id, str(row["kind"]), new, content_touched="markdown" in safe_patch)
            if expected_rev is None:
                await db.execute(
                    "UPDATE canvas_sections SET payload=?, rev=?, updated_at=? WHERE id=?",
                    (json.dumps(new, ensure_ascii=False), new_rev, now, section_id),
                )
            else:
                update_cur = await db.execute(
                    "UPDATE canvas_sections SET payload=?, rev=?, updated_at=? WHERE id=? AND rev=?",
                    (json.dumps(new, ensure_ascii=False), new_rev, now, section_id, expected_rev),
                )
                if update_cur.rowcount == 0:
                    await db.rollback()
                    async with db.execute(
                        "SELECT rev FROM canvas_sections WHERE id=?",
                        (section_id,),
                    ) as cur:
                        latest = await cur.fetchone()
                    if latest is None:
                        return None
                    raise CanvasRevConflictError(int(latest["rev"]))
            await db.execute(
                "UPDATE canvases SET updated_at=? WHERE id=?",
                (now, canvas_id),
            )
            await db.execute(
                "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, owner, created_at) "
                "VALUES (?, ?, ?, 'patch', ?, ?, ?, ?, ?)",
                (
                    _nid("rv"),
                    canvas_id,
                    section_id,
                    json.dumps(prev, ensure_ascii=False),
                    json.dumps(new, ensure_ascii=False),
                    by_turn_id,
                    caller,
                    now,
                ),
            )
            await self._fts_sync(db, canvas_id=canvas_id, section_id=section_id, payload=new)  # R4
            await db.commit()
            result = Section(
                id=section_id,
                canvas_id=canvas_id,
                seq=int(row["seq"]),
                kind=row["kind"],
                payload=new,
                rev=new_rev,
                created_at=row["created_at"],
                updated_at=now,
                owner=section_owner,
                # citation-audit impl-review B-P2-4: returned Section previously lost turn_id/superseded,
                # leaving section_patched DTOs incomplete and corrupting turn grouping in FE/IM late-join insertion.
                # Patch does not change either column; use original row values.
                turn_id=row["turn_id"],
                superseded=bool(row["superseded"]),
            )
        # V4 Sprint 2 PRD-D-4-1: cascade (post-commit, hook gets MERGED new payload)
        await self._fire_anchor_invalidation("payload_changed", canvas_id, section_id, new)
        return result

    async def has_manual_reorder(self, canvas_id: str) -> bool:
        """该画布是否被**人**调过顺序（规范排序据此让路）。

        信号 = ``op='reorder'`` 且 ``by_turn_id`` 为空：机器重排一律带 turn
        （compose / chrome promote / agent 工具），用户面 ``/reorder`` 路由不传。

        ⚠ 作业脚本走的也是用户面语义：``scripts/canvas_seq_normalize.py`` 原来写
        ``by_turn_id=None``，跑一遍就会把它排过的画布**永久**标成「用户拖过」，
        本批修的问题会被自己的清理作业锁死 —— 所以作业必须带
        :data:`MACHINE_REORDER_TURN_ID` 哨兵。

        ⚠ 存量的 NULL 行分辨不出来源（116 上 182 个画布有这种行，其中 0 个在那之后
        又有机器写入 ⇒ 都是静止画布，实际影响为 0）。这条守卫是**画布级、全历史**的：
        命中一次，该画布此后所有轮次都不再规范排序。
        """
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT 1 FROM canvas_revisions WHERE canvas_id=? AND op='reorder' AND (by_turn_id IS NULL OR by_turn_id='') LIMIT 1",
                (canvas_id,),
            ) as cur,
        ):
            return await cur.fetchone() is not None

    async def reorder_sections(
        self,
        *,
        canvas_id: str,
        ordered_ids: list[str],
        by_turn_id: str | None = None,
    ) -> list[str]:
        """重排 canvas section 序，**整份重编 seq=1..N**。

        A3 (report-edit-audit): 原实现只给传入的 id 写 seq=1..len(ordered_ids)、
        **不动其余 section** → 与未列出者的既有 seq 直接撞车，读取 `ORDER BY seq, id`
        撞车后按 id 乱序（P1-1 实锤：编辑一个字就把库序打乱、导出 PDF 头部散架）。

        现语义 = **补全重编**：`最终序 = [传入且存在的 id 按给定序] + [未列出的 id 按原
        相对序]`，对最终序整体重编 seq。为何不对部分序硬 400：`canvas_reorder_sections`
        是真 agent 工具（canvas-writer 可调、LLM 会发部分 id 列表），硬拒会打崩 agent 流。

        `BEGIN IMMEDIATE` 提前占写锁（同 `append_section`）：原实现无显式事务边界，
        并发 reorder 交错时 UPDATE 序不可控。收尾断言同 canvas seq 唯一 —— 违例即
        raise 让事务回滚，把"撞车"变成不可能而不是靠调用方自觉。

        Returns: 实际落库的完整顺序。
        """
        now = _now()
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id FROM canvas_sections WHERE canvas_id=? ORDER BY seq, id",
                (canvas_id,),
            ) as cur:
                existing = [str(r["id"]) for r in await cur.fetchall()]
            requested = [sid for sid in ordered_ids if sid in set(existing)]
            requested_set = set(requested)
            final_order = requested + [sid for sid in existing if sid not in requested_set]
            for new_seq, sid in enumerate(final_order, start=1):
                await db.execute(
                    "UPDATE canvas_sections SET seq=?, updated_at=? WHERE id=? AND canvas_id=?",
                    (new_seq, now, sid, canvas_id),
                )
            async with db.execute(
                "SELECT COUNT(*) AS n FROM (SELECT seq FROM canvas_sections WHERE canvas_id=? GROUP BY seq HAVING COUNT(*)>1)",
                (canvas_id,),
            ) as cur:
                dup_row = await cur.fetchone()
            if dup_row is not None and int(dup_row["n"]) > 0:
                raise RuntimeError(f"canvas {canvas_id}: duplicate seq after reorder (rolled back)")
            await db.execute(
                "UPDATE canvases SET updated_at=? WHERE id=?",
                (now, canvas_id),
            )
            await db.execute(
                "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
                "VALUES (?, ?, NULL, 'reorder', NULL, ?, ?, ?)",
                (_nid("rv"), canvas_id, json.dumps({"order": final_order}, ensure_ascii=False), by_turn_id, now),
            )
            await db.commit()
        return final_order

    async def _fire_anchor_invalidation(
        self,
        event_kind: str,
        canvas_id: str,
        section_id: str | None,
        new_payload: dict[str, Any] | None,
    ) -> None:
        """V4 Sprint 2 PRD-D-4-1 anchor cascade（post-commit 统一入口，memory-revive S1
        收编原三处 inline 样板 + 补挂 split/merge/clear）。Hook 错误只记日志绝不上抛——
        canvas 写入必须成功，读时兜底（memory_notes routes 的 runtime 重算）保证展示正确性。
        """
        if self._anchor_invalidation_hook is None:
            return
        try:
            with detached_auxiliary_scope(reason="canvas_anchor_invalidation"):
                await self._anchor_invalidation_hook(
                    event_kind,
                    canvas_id,
                    section_id,
                    new_payload,
                )
        except Exception as e:
            logger.warning(
                "anchor_invalidation_hook_failed event=%s canvas=%s section=%s err=%s",
                event_kind,
                canvas_id,
                section_id,
                e,
            )

    async def get_section_canvas_id(self, section_id: str) -> str | None:
        """A0-4 (report-edit-audit): 供 gateway 校验 sid∈canvas 的轻量查询."""
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT canvas_id FROM canvas_sections WHERE id=?",
                (section_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def delete_section(
        self,
        *,
        section_id: str,
        by_turn_id: str | None = None,
        canvas_id: str | None = None,
        expected_rev: int | None = None,
        cascade: bool = False,
    ) -> list[str]:
        """删除单个 section (``cascade=True`` 时连带其独占的内联视觉块).

        A0-4 (report-edit-audit): ``canvas_id`` 给定时校验归属——section 不属于该
        canvas 视同不存在(返 False, 不泄漏跨 canvas 信息)。此前 gateway 路由只验
        canvas 所有权、不验 sid∈canvas, 拥有任意 canvas 即可删他人 section (IDOR)。
        ``expected_rev`` 给定且与当前 rev 不符 → CanvasRevConflictError (与
        patch_section 的 compare-and-set 同语义), 防并发写丢。

        A2 (report-edit-audit, 口径已拍=级联删除): ``cascade`` 时把宿主独占的内联
        chart/image 一起删, **单事务**内逐个写 delete revision。子集由服务端自己从
        宿主 payload 解析 (`[figure:id]` / insight `evidence[].chart_section_id`),
        **不接受调用方传子 id 列表** —— 宿主 markdown 是用户可编辑内容, 可被写入
        任意 `[figure:sec_他人]`; 故三重过滤缺一不可:
          ① 同 canvas (不然=借刀跨 canvas 删除, 与 A0-4 同形 IDOR);
          ② kind ∈ {chart,image} 白名单 (不许连带删正文/来源);
          ③ 未被本 canvas 其他存活 section 引用 (双引用图只删独占的)。

        Returns: 实删的 section id 列表 (未找到/归属不符 → `[]`)。空列表 falsy,
        既有 `if not ok:` 调用点语义不变; 级联需要清单回传给 FE, 故不用共享实例
        属性 (store 是单例, 并发删除会串味)。
        """
        now = _now()
        deleted_ids: list[str] = [section_id]
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM canvas_sections WHERE id=?",
                (section_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return []
            if canvas_id is not None and row["canvas_id"] != canvas_id:
                return []
            if expected_rev is not None and int(row["rev"]) != expected_rev:
                raise CanvasRevConflictError(int(row["rev"]))
            canvas_id = row["canvas_id"]
            cascade_rows: list[Any] = []
            if cascade:
                cascade_rows = await self._resolve_cascade_children(db, canvas_id=canvas_id, host_row=row)
                deleted_ids.extend(str(child["id"]) for child in cascade_rows)
            # Plan A (report-edit-audit dogfood P1): delete revisions upgrade from payload-only storage
            # to full restoration snapshots (envelopes). Old records support viewing but not restoration: deleted rows lose kind/seq/
            # metadata, leaving restore_deleted_sections unable to reconstruct them; a root cause of the drawer's inability
            # to roll back deletion. batch groups host and cascading child visuals for whole-group restoration
            # so [figure:id] anchors are not dangling. Recognize dicts with restore_schema==1; retain old rows
            # unchanged (restore reports unsupported for old rows, without guessing).
            batch_id = _nid("delb")
            restore_meta_cols = (
                "rev",
                "created_at",
                "updated_at",
                "quarantined",
                "quarantine_reason",
                "hidden_for_chat_mode",
                "surface_rationale",
                # Candidate 4: deletion→restoration must retain declarations, or deleting/restoring silently loses
                # the figure's destination ([[feedback_my_own_fixtures_are_the_happy_path]]).
                "surface_target",
                "schema_version",
                "owner",
                "superseded",
                "superseded_by_turn",
                "turn_id",
            )
            for target in [row, *cascade_rows]:
                tid = str(target["id"])
                await db.execute("DELETE FROM canvas_sections WHERE id=?", (tid,))
                await self._fts_remove(db, tid)  # R4
                try:
                    target_payload = json.loads(target["payload"] or "{}")
                except (TypeError, ValueError):
                    target_payload = {}
                envelope = {
                    "restore_schema": 1,
                    "batch": batch_id,
                    "batch_host": section_id,
                    "kind": target["kind"],
                    "seq": target["seq"],
                    "payload": target_payload,
                    "row_meta": {col: target[col] for col in restore_meta_cols},
                }
                await db.execute(
                    "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
                    "VALUES (?, ?, ?, 'delete', ?, NULL, ?, ?)",
                    (_nid("rv"), canvas_id, tid, json.dumps(envelope, ensure_ascii=False), by_turn_id, now),
                )
            await db.execute(
                "UPDATE canvases SET updated_at=? WHERE id=?",
                (now, canvas_id),
            )
            await db.commit()
        # V4 Sprint 2 PRD-D-4-1: cascade (post-commit, non-blocking on hook error)
        for tid in deleted_ids:
            await self._fire_anchor_invalidation("section_deleted", canvas_id, tid, None)
        return deleted_ids

    async def _resolve_cascade_children(
        self,
        db: Any,
        *,
        canvas_id: str,
        host_row: Any,
    ) -> list[Any]:
        """A2: 解析宿主独占的内联视觉块 (见 delete_section 的三重过滤说明)."""
        payload = json.loads(host_row["payload"] or "{}")
        referenced = _inline_visual_refs(payload)
        if not referenced:
            return []
        # ① Same Canvas + ② kind allowlist, enforced in one SQL query (cross-Canvas IDs are simply absent).
        async with db.execute(
            "SELECT * FROM canvas_sections WHERE canvas_id=? AND kind IN ('chart','image')",
            (canvas_id,),
        ) as cur:
            candidate_rows = {str(r["id"]): r for r in await cur.fetchall()}
        # ③ Exclusive reference: scan other live sections' references in this Canvas; never delete visuals referenced elsewhere.
        async with db.execute(
            "SELECT id, payload FROM canvas_sections WHERE canvas_id=? AND id!=?",
            (canvas_id, str(host_row["id"])),
        ) as cur:
            others = await cur.fetchall()
        referenced_elsewhere: set[str] = set()
        for other in others:
            if str(other["id"]) in candidate_rows:
                continue  # Candidate visuals themselves do not count as references elsewhere
            try:
                other_payload = json.loads(other["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            referenced_elsewhere.update(_inline_visual_refs(other_payload))
        return [candidate_rows[ref] for ref in referenced if ref in candidate_rows and ref not in referenced_elsewhere]

    async def restore_deleted_sections(
        self,
        *,
        canvas_id: str,
        revision_id: str,
    ) -> list[str] | None:
        """方案A (report-edit-audit dogfood P1): 从 delete revision 的恢复 envelope
        重建已删 section, 兑现删除确认框「删除后可从历史版本恢复」的承诺。

        - 整组恢复: revision 带 batch (级联删除时宿主+子图同一 batch) 则一次把组内
          **尚缺**的全部恢复——只还宿主会让 `[figure:id]` 锚悬空。
        - 原 id 原样回插 (锚引用按 id 关联, 换 id = 白恢复); 原 seq 空闲则回原位,
          被占则接到 canvas 尾部 (MAX(seq)+1 起), 不挤动现有行。
        - 已存在的跳过 (幂等); 每个恢复写一条 op='append' revision (drawer 已会渲染,
          不新增 op 枚举)。
        - Returns: 恢复的 id 列表; `[]` = revision 有效但全员已在场 (调用方可报
          already_restored); `None` = revision 不存在 / 不属于该 canvas / 非 delete
          (统一视同不存在, 不泄漏跨 canvas 信息, 与 A0-4 同口径)。
        - Raises CanvasRestoreUnsupportedError: 旧格式 delete revision (无 envelope)。
        """
        now = _now()
        restored: list[tuple[str, dict[str, Any]]] = []
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM canvas_revisions WHERE id=?",
                (revision_id,),
            ) as cur:
                rev_row = await cur.fetchone()
            if rev_row is None or rev_row["canvas_id"] != canvas_id or rev_row["op"] != "delete":
                return None
            envelope = _parse_restore_envelope(rev_row["prev_payload"])
            if envelope is None:
                raise CanvasRestoreUnsupportedError()
            # Collect delete revisions sharing batch, including this one; host first. Envelopes without batch
            # restore only themselves (None==None would wrongly group every batchless record).
            batch = envelope.get("batch")
            targets: list[tuple[str, dict[str, Any]]] = []
            if batch is None:
                targets.append((str(rev_row["section_id"]), envelope))
            else:
                async with db.execute(
                    "SELECT section_id, prev_payload FROM canvas_revisions WHERE canvas_id=? AND op='delete' ORDER BY created_at",
                    (canvas_id,),
                ) as cur:
                    for r in await cur.fetchall():
                        e = _parse_restore_envelope(r["prev_payload"])
                        if e is not None and e.get("batch") == batch and r["section_id"]:
                            targets.append((str(r["section_id"]), e))
            targets.sort(key=lambda t: (t[0] != t[1].get("batch_host"), int(t[1].get("seq") or 0)))
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM canvas_sections WHERE canvas_id=?",
                (canvas_id,),
            ) as cur:
                next_free_seq = int((await cur.fetchone())[0]) + 1
            for sid, env in targets:
                async with db.execute(
                    "SELECT 1 FROM canvas_sections WHERE id=?",
                    (sid,),
                ) as cur:
                    if await cur.fetchone() is not None:
                        continue  # Already present (repeated click/manually restored)
                original_seq = int(env.get("seq") or 0)
                async with db.execute(
                    "SELECT 1 FROM canvas_sections WHERE canvas_id=? AND seq=?",
                    (canvas_id, original_seq),
                ) as cur:
                    seq_taken = await cur.fetchone() is not None
                seq = original_seq if original_seq > 0 and not seq_taken else next_free_seq
                # Original seq can exceed next_free if other rows were cleared after deletion; advance next_free
                # past it to avoid later fallback collisions (the final assertion catches them, but fix the cause rather than relying on it).
                if seq >= next_free_seq:
                    next_free_seq = seq + 1
                meta = env.get("row_meta") or {}
                payload = env.get("payload") or {}
                if env.get("kind") == "chart":
                    validate_stored_composition(payload.get("spec"))
                await db.execute(
                    "INSERT INTO canvas_sections("
                    "id, canvas_id, seq, kind, payload, rev, created_at, updated_at, "
                    "quarantined, quarantine_reason, hidden_for_chat_mode, surface_rationale, "
                    "surface_target, "
                    "schema_version, owner, superseded, superseded_by_turn, turn_id"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        canvas_id,
                        seq,
                        env.get("kind"),
                        json.dumps(payload, ensure_ascii=False),
                        int(meta.get("rev") or 1),
                        meta.get("created_at") or now,
                        now,
                        int(meta.get("quarantined") or 0),
                        meta.get("quarantine_reason"),
                        int(meta.get("hidden_for_chat_mode") or 0),
                        meta.get("surface_rationale"),
                        meta.get("surface_target"),
                        meta.get("schema_version"),
                        meta.get("owner"),
                        int(meta.get("superseded") or 0),
                        meta.get("superseded_by_turn"),
                        meta.get("turn_id"),
                    ),
                )
                await db.execute(
                    "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
                    "VALUES (?, ?, ?, 'append', NULL, ?, NULL, ?)",
                    (_nid("rv"), canvas_id, sid, json.dumps(payload, ensure_ascii=False), now),
                )
                await self._fts_sync(db, canvas_id=canvas_id, section_id=sid, payload=payload)
                restored.append((sid, payload))
            if restored:
                async with db.execute(
                    "SELECT seq FROM canvas_sections WHERE canvas_id=? GROUP BY seq HAVING COUNT(*) > 1",
                    (canvas_id,),
                ) as cur:
                    dup = await cur.fetchall()
                if dup:
                    # Same final assertion as reorder: collisions roll back the whole group rather than committing a partial set.
                    await db.rollback()
                    raise RuntimeError(f"restore produced duplicate seq: {[r[0] for r in dup]}")
                await db.execute(
                    "UPDATE canvases SET updated_at=? WHERE id=?",
                    (now, canvas_id),
                )
            await db.commit()
        for sid, payload in restored:
            await self._fire_anchor_invalidation("payload_changed", canvas_id, sid, payload)
        return [sid for sid, _ in restored]

    async def split_section(
        self,
        *,
        section_id: str,
        offset: int,
        by_turn_id: str | None = None,
    ) -> tuple[Section, Section] | None:
        """
        B-7 / W11-9: split text-based (narrative/deck/callout) section 在 markdown 字符 offset 处分两段.
        - 原 section.markdown ← markdown[:offset]
        - 新 section.markdown ← markdown[offset:],seq = 原 + 1, 其他字段拷贝
        - 后续 section seq++ 让位
        - **source_refs 两段整份继承**（citation-audit M3 起）— 刻意的过度声明。
          原行为是「按 markdown 真出现的 [ref:..] / [num:..src:..] 重新提取, 每段只保
          命中 ID」，但那个提取器认的是 gen2 标记语法；gen3 的 `[N]` 是**编号**不是
          source id，提取器对洗净后的库恒返空 → 必然落到整份继承分支，故 M3 直接删掉
          提取器、留单一判据（行为等价，见模块级注释）。
          可做但未做：`[N]` → artifact_id 其实可逆（sources entries 带 artifact_id，
          `sources_resolver` 建 id_to_index），split_section 手上有 canvas_id，多查一次
          sources 段即可按 index→id 精确过滤。是取舍（多一次查询）不是死路——用户面
          影响 = 两半各自多声明来源（保守、来源面板变吵），M1/M2 起即如此。
        - 写两条 revision (patch + append)
        Returns (updated_original, new_after) or None.
        """
        now = _now()
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM canvas_sections WHERE id=?",
                (section_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            kind = row["kind"]
            if kind not in {"narrative", "deck", "callout"}:
                # Split is text-based only; atomic unsupported
                return None
            prev = json.loads(row["payload"])
            md = str(prev.get("markdown") or "")
            # r3 §5.2: boundary offsets leave one half empty; reject instead of clamping and creating shells.
            o = int(offset)
            if o <= 0 or o >= len(md):
                raise CanvasSplitDegenerateError(offset=o, length=len(md))
            first = md[:o]
            second = md[o:]
            canvas_id = row["canvas_id"]
            orig_seq = int(row["seq"])
            # Increase all subsequent seq by one; insert new at orig_seq+1.
            await db.execute(
                "UPDATE canvas_sections SET seq=seq+1 WHERE canvas_id=? AND seq>?",
                (canvas_id, orig_seq),
            )
            # Original section becomes first; filter source_refs by IDs actually used in first.
            prev_source_refs = prev.get("source_refs") or []
            new_payload: dict[str, Any] = {**prev, "markdown": first}
            if isinstance(prev_source_refs, list) and prev_source_refs:
                # gen3 [N] cannot yield source IDs (indices carry no identity); any numbered citation inherits all refs.
                first_kept = list(prev_source_refs) if _NUMBERED_CITE_RE.search(first) else []
                if first_kept:
                    new_payload["source_refs"] = first_kept
                else:
                    # No remaining citations → remove source_refs key to avoid an empty list
                    new_payload.pop("source_refs", None)
            new_rev = int(row["rev"]) + 1
            await db.execute(
                "UPDATE canvas_sections SET payload=?, rev=?, updated_at=? WHERE id=?",
                (json.dumps(new_payload, ensure_ascii=False), new_rev, now, section_id),
            )
            # New section: same kind, copy only source_refs actually used in second, plus unverified and label.
            new_id = _nid("sec")
            new_seq = orig_seq + 1
            new_section_payload: dict[str, Any] = {"markdown": second}
            if isinstance(prev_source_refs, list) and prev_source_refs:
                # Same as first: gen3 [N] sections inherit all refs.
                second_kept = list(prev_source_refs) if _NUMBERED_CITE_RE.search(second) else []
                if second_kept:
                    new_section_payload["source_refs"] = second_kept
            # Move unverified_total with unverified_numbers: moving only the list gives the new half the list while
            # the old half retains the total, inflating the combined denominator (re-review observed 20→40).
            for carry_key in ("unverified_numbers", "unverified_total", "label"):
                if carry_key in prev:
                    new_section_payload[carry_key] = prev[carry_key]
            await db.execute(
                "INSERT INTO canvas_sections(id, canvas_id, seq, kind, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id, canvas_id, new_seq, kind, json.dumps(new_section_payload, ensure_ascii=False), now, now),
            )
            # R4: reindex shortened original and index new section, or the split-off lower half becomes unsearchable.
            await self._fts_sync(db, canvas_id=canvas_id, section_id=section_id, payload=new_payload)
            await self._fts_sync(db, canvas_id=canvas_id, section_id=new_id, payload=new_section_payload)
            # Update Canvas updated_at and write two revisions.
            await db.execute(
                "UPDATE canvases SET updated_at=? WHERE id=?",
                (now, canvas_id),
            )
            await db.execute(
                "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
                "VALUES (?, ?, ?, 'patch', ?, ?, ?, ?)",
                (
                    _nid("rv"),
                    canvas_id,
                    section_id,
                    json.dumps(prev, ensure_ascii=False),
                    json.dumps(new_payload, ensure_ascii=False),
                    by_turn_id,
                    now,
                ),
            )
            await db.execute(
                "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
                "VALUES (?, ?, ?, 'append', NULL, ?, ?, ?)",
                (_nid("rv"), canvas_id, new_id, json.dumps(new_section_payload, ensure_ascii=False), by_turn_id, now),
            )
            await db.commit()
            updated = Section(
                id=section_id,
                canvas_id=canvas_id,
                seq=orig_seq,
                kind=kind,
                payload=new_payload,
                rev=new_rev,
                created_at=row["created_at"],
                updated_at=now,
            )
            new_sec = Section(
                id=new_id,
                canvas_id=canvas_id,
                seq=new_seq,
                kind=kind,
                payload=new_section_payload,
                rev=1,
                created_at=now,
                updated_at=now,
            )
        # memory-revive S1 (discovery §6.4 missing-hook fix): shortened original markdown means changed payload;
        # recheck its memory anchors by hash (new section has no anchors, so no event needed).
        await self._fire_anchor_invalidation("payload_changed", canvas_id, section_id, new_payload)
        return updated, new_sec

    async def merge_with_prev(
        self,
        *,
        section_id: str,
        by_turn_id: str | None = None,
    ) -> Section | None:
        """W12-6: 把 section 与紧前一个同 kind 的 text-based section 合并 (单事务).

        - 找 prev section (canvas 内 seq < target.seq 的最大 seq)
        - 验:两边都是 text-based (narrative / deck / callout) 且 **kind 相同**
        - prev.payload.markdown ← prev.markdown + "\\n\\n" + target.markdown
        - prev.payload.source_refs ← dedupe(prev.source_refs ∪ target.source_refs)
        - DELETE target + UPDATE prev + 2 revision (patch + delete) — 单事务原子
        - 不动 seq (有 gap 不影响, sections.seq ASC 显示)

        失败返 None: section 不存在 / 无 prev / kind 不匹配.
        """
        now = _now()
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM canvas_sections WHERE id=?",
                (section_id,),
            ) as cur:
                target_row = await cur.fetchone()
            if target_row is None:
                return None
            kind = target_row["kind"]
            if kind not in {"narrative", "deck", "callout"}:
                return None
            canvas_id = target_row["canvas_id"]
            target_seq = int(target_row["seq"])
            # Find previous section: same Canvas, maximal seq below target_seq.
            async with db.execute(
                "SELECT * FROM canvas_sections WHERE canvas_id=? AND seq<? ORDER BY seq DESC LIMIT 1",
                (canvas_id, target_seq),
            ) as cur:
                prev_row = await cur.fetchone()
            if prev_row is None:
                return None  # Already the first section
            if prev_row["kind"] != kind:
                return None  # Kind mismatch; reject merge

            prev_payload = json.loads(prev_row["payload"])
            target_payload = json.loads(target_row["payload"])
            prev_md = str(prev_payload.get("markdown") or "")
            target_md = str(target_payload.get("markdown") or "")
            # Separate the sections with a blank line; avoid duplicating existing trailing whitespace in prev.
            sep = "" if prev_md.endswith("\n\n") else ("\n" if prev_md.endswith("\n") else "\n\n")
            new_md = prev_md + sep + target_md if (prev_md or target_md) else ""

            # Merge source_refs: keep prev order, append target refs not already in prev.
            prev_refs = prev_payload.get("source_refs") or []
            target_refs = target_payload.get("source_refs") or []
            if not isinstance(prev_refs, list):
                prev_refs = []
            if not isinstance(target_refs, list):
                target_refs = []
            seen = set(prev_refs)
            merged_refs = list(prev_refs) + [r for r in target_refs if r not in seen]

            new_prev_payload = {**prev_payload, "markdown": new_md}
            if merged_refs:
                new_prev_payload["source_refs"] = merged_refs
            else:
                new_prev_payload.pop("source_refs", None)
            new_rev = int(prev_row["rev"]) + 1

            # Single transaction: UPDATE prev + DELETE target + two revisions + canvas.updated_at.
            await db.execute(
                "UPDATE canvas_sections SET payload=?, rev=?, updated_at=? WHERE id=?",
                (json.dumps(new_prev_payload, ensure_ascii=False), new_rev, now, prev_row["id"]),
            )
            await db.execute(
                "DELETE FROM canvas_sections WHERE id=?",
                (section_id,),
            )
            # R4: reindex merged prev content and remove target from index.
            await self._fts_sync(db, canvas_id=canvas_id, section_id=prev_row["id"], payload=new_prev_payload)
            await self._fts_remove(db, section_id)
            await db.execute(
                "UPDATE canvases SET updated_at=? WHERE id=?",
                (now, canvas_id),
            )
            await db.execute(
                "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
                "VALUES (?, ?, ?, 'patch', ?, ?, ?, ?)",
                (
                    _nid("rv"),
                    canvas_id,
                    prev_row["id"],
                    json.dumps(prev_payload, ensure_ascii=False),
                    json.dumps(new_prev_payload, ensure_ascii=False),
                    by_turn_id,
                    now,
                ),
            )
            await db.execute(
                "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
                "VALUES (?, ?, ?, 'delete', ?, NULL, ?, ?)",
                (_nid("rv"), canvas_id, section_id, json.dumps(target_payload, ensure_ascii=False), by_turn_id, now),
            )
            await db.commit()
            merged = Section(
                id=prev_row["id"],
                canvas_id=canvas_id,
                seq=int(prev_row["seq"]),
                kind=kind,
                payload=new_prev_payload,
                rev=new_rev,
                created_at=prev_row["created_at"],
                updated_at=now,
            )
        # memory-revive S1 (discovery §6.4 missing-hook fix): merge changes prev payload and deletes target;
        # cascade anchor invalidation at both locations.
        await self._fire_anchor_invalidation("payload_changed", canvas_id, prev_row["id"], new_prev_payload)
        await self._fire_anchor_invalidation("section_deleted", canvas_id, section_id, None)
        return merged

    async def clear_sections(
        self,
        *,
        canvas_id: str,
        by_turn_id: str | None = None,
    ) -> int:
        """P4.1: 清空 canvas 所有 sections, 保留 canvas 壳 (id / title / status 不变).
        返删除数. 不写 revision (avoid 单 turn 100+ rev), title 改成 "" 让 sidebar refresh.
        """
        now = _now()
        async with open_shared_ctx(self._path) as db:
            async with db.execute(
                "SELECT id FROM canvas_sections WHERE canvas_id=?",
                (canvas_id,),
            ) as cur:
                id_rows = await cur.fetchall()
            cleared_ids = [r[0] for r in id_rows]
            n = len(cleared_ids)
            await db.execute("DELETE FROM canvas_sections WHERE canvas_id=?", (canvas_id,))
            if self._fts_enabled:
                try:
                    await db.execute("DELETE FROM canvas_fts WHERE canvas_id=?", (canvas_id,))  # R4
                except Exception as e:
                    logger.warning("fts clear_sections failed canvas=%s err=%s", canvas_id, e)
            await db.execute(
                "UPDATE canvases SET updated_at=? WHERE id=?",
                (now, canvas_id),
            )
            await db.commit()
        # memory-revive S1 (discovery §6.4 largest missing hook: frontend clear Canvas): cascade per section,
        # with accurate reason semantics (Canvas shell remains; do not emit canvas_deleted).
        for cleared_id in cleared_ids:
            await self._fire_anchor_invalidation("section_deleted", canvas_id, cleared_id, None)
        return n

    async def delete_canvas(self, *, canvas_id: str) -> bool:
        """P4.1: 整 canvas 删 (含 sections + revisions cascade).
        返 True 如命中. revision history 也删 (cascade) — 用户接受不可恢复.
        """
        async with open_shared_ctx(self._path) as db:
            deleted = await self.delete_canvas_in_transaction(db, canvas_id=canvas_id)
            if not deleted:
                return False
            await db.commit()
        await self.notify_canvas_deleted(canvas_id)
        return True

    async def delete_canvas_in_transaction(
        self,
        db: aiosqlite.Connection,
        *,
        canvas_id: str,
        expected_session_id: str | None = None,
        expected_user_id: str | None = None,
    ) -> bool:
        """Delete one Canvas using the caller-owned transaction.

        This seam lets the Terminal Protocol lifecycle coordinator commit the
        linked Canvas deletion, Session visibility fence, and idempotency
        result atomically.  It never commits and never fires post-commit hooks.
        """

        async with db.execute(
            "SELECT session_id,user_id FROM canvases WHERE id=?",
            (canvas_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        if expected_session_id is not None and row[0] != expected_session_id:
            raise ValueError("canvas/session lifecycle identity mismatch")
        if expected_user_id is not None and row[1] != expected_user_id:
            raise ValueError("canvas/user lifecycle identity mismatch")
        await db.execute("DELETE FROM canvas_sections WHERE canvas_id=?", (canvas_id,))
        await db.execute("DELETE FROM canvas_revisions WHERE canvas_id=?", (canvas_id,))
        if self._fts_enabled:
            try:
                await db.execute("DELETE FROM canvas_fts WHERE canvas_id=?", (canvas_id,))  # R4
            except Exception as exc:
                logger.warning("fts delete_canvas failed canvas=%s err=%s", canvas_id, exc)
        deleted = await db.execute("DELETE FROM canvases WHERE id=?", (canvas_id,))
        return deleted.rowcount == 1

    async def notify_canvas_deleted(self, canvas_id: str) -> None:
        """Run idempotent post-commit invalidation for any Canvas delete owner."""

        # V4 Sprint 2 PRD-D-4-1: hook errors are logged, never raised.  The
        # read-time fallback remains authoritative if delivery is interrupted.
        await self._fire_anchor_invalidation("canvas_deleted", canvas_id, None, None)

    async def list_for_user(
        self,
        *,
        user_id: str,
        workspace_id: str | None = None,
        q: str | None = None,
        sort: str = "updated_desc",
        limit: int = 50,
        include_archived: bool = True,
        session_ids: list[str] | None = None,
        min_sections: int = 0,
        min_visible_sections: int = 0,
        workspace_ids: list[str] | None = None,
        exclude_user_id: str | None = None,
        after: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """P4.2: 跨 session 列用户所有 canvas (带 sections_count + last_activity).
        sort: updated_desc / sections_desc / title_asc.

        chat-page-polish B1: 每条还返 first_section_title — 取最早 (seq ASC) section
        的 payload 里第一个非空 heading/title 字段 (h1 → h2 → h3 → title → text 优先级).
        前端在 c.title 为 null 时拿这个做 fallback, 避免显 raw cv_xxx ID.

        R2: 每条返 status(存储值) + quarantined_count + effective_status(显示用,
        收编 quarantine 为 stale). include_archived=False → 排除 archived(AI/KB 检索面用).
        R4: q 走 FTS5 全文(≥3 字, trigram 子串匹配 section 正文) + title/id LIKE 兜底.

        content-permissions S5: ``session_ids`` 给定时改按 session 集合查（shared-with-me:
        调用者经 report grants 反查到的 session_id 列表,owner 维度不再过滤）,复用同一套
        enrich;[] → 直接返空。IN 上限远低于 SQLITE_MAX_VARIABLE_NUMBER。

        empty-canvas 清理批: ``min_sections>0`` 在 SQL 里过滤掉空壳——必须落在
        ORDER BY / LIMIT **之前**。调用方取完 limit 条再自己过滤会被空壳吃掉名额
        (首页最近产出实测 8 取 5), 判据下沉到这里, 消费方不再各判一次。默认 0 =
        行为不变。

        报告库的 ``min_visible_sections`` 只按 detail 口径过滤
        (``hidden_for_chat_mode=0``)，不额外排除 superseded / quarantined / meta。
        它同样必须在 ORDER BY / LIMIT 前执行，避免 chat-only 空壳吃掉报告名额。

        报告库一致性（116 v1.1.53）：``sections_count`` 与
        ``first_section_title`` 只看 ``hidden_for_chat_mode=0``，必须和
        Canvas detail 的用户可见集合一致。D6 隐藏行仍保留作聊天内联/审计，不能
        再被列表包装成可打开、可下载的报告。

        Report-library pagination: ``workspace_ids`` restricts rows to the Spaces the
        caller may read (an empty list returns nothing), ``exclude_user_id`` drops the
        caller's own rows from the shared-with-me partition, and ``after`` holds the
        sort-key values from :func:`report_list_cursor_values` of the last row of the
        previous page. Every sort ends with ``id`` as a tie-break so pages never skip
        or repeat rows that share a sort value.
        """
        if session_ids is not None and not session_ids:
            return []
        if workspace_ids is not None and not workspace_ids:
            return []
        where_sql, params = self._report_list_where(
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_ids=workspace_ids,
            exclude_user_id=exclude_user_id,
            q=q,
            include_archived=include_archived,
            session_ids=session_ids,
            min_sections=min_sections,
            min_visible_sections=min_visible_sections,
        )
        keys = _REPORT_LIST_SORT_KEYS[normalize_report_list_sort(sort)]
        inner = f"""
            SELECT c.id, c.session_id, c.workspace_id, c.user_id, c.title,
                   c.status, c.created_at, c.updated_at,
                   COALESCE(c.title, '') AS sort_title,
                   (SELECT COUNT(*) FROM canvas_sections s
                    WHERE s.canvas_id = c.id AND s.hidden_for_chat_mode = 0) AS sec_count,
                   (SELECT COUNT(*) FROM canvas_sections sq
                    WHERE sq.canvas_id = c.id AND sq.quarantined = 1) AS quar_count
            FROM canvases c
            WHERE {where_sql} AND {await self._account_visibility_sql("c")}
            """
        sql_parts = [f"SELECT * FROM ({inner}) AS r"]
        if after is not None:
            # Keyset pagination: rows strictly after the cursor in the full (tie-broken) sort order.
            if len(after) != len(keys):
                raise ValueError("report list cursor does not match the sort keys")
            alternatives: list[str] = []
            for i, (column, direction) in enumerate(keys):
                op = "<" if direction == "DESC" else ">"
                terms = [f"r.{prev_col} = ?" for prev_col, _ in keys[:i]]
                terms.append(f"r.{column} {op} ?")
                alternatives.append("(" + " AND ".join(terms) + ")")
                params.extend(after[:i])
                params.append(after[i])
            sql_parts.append("WHERE (" + " OR ".join(alternatives) + ")")
        sql_parts.append("ORDER BY " + ", ".join(f"r.{column} {direction}" for column, direction in keys))
        sql_parts.append("LIMIT ?")
        params.append(limit)
        sql = " ".join(sql_parts)
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
            # B1: fetch earliest section payload for every Canvas in one query (per-Canvas seq ASC LIMIT 1).
            # Avoid JOIN in the main SQL: Python must parse JSON and select fields; a second query is simpler and small.
            canvas_ids = [r["id"] for r in rows]
            first_title_map: dict[str, str | None] = {}
            if canvas_ids:
                placeholders = ",".join("?" for _ in canvas_ids)
                # Simulate ROW_NUMBER in SQLite: direct ORDER + LIMIT per Canvas is cumbersome, so fetch
                # every section for these Canvases in order and select first matches in Python.
                # Around 100 Canvases × a few sections performs well; switch to LATERAL at 1000+ in production.
                section_sql = (
                    "SELECT canvas_id, payload FROM canvas_sections "
                    f"WHERE canvas_id IN ({placeholders}) AND hidden_for_chat_mode = 0 "
                    "ORDER BY canvas_id, seq ASC"
                )
                async with db.execute(section_sql, canvas_ids) as cur:
                    sec_rows = await cur.fetchall()
                seen: set[str] = set()
                for sr in sec_rows:
                    cid = sr["canvas_id"]
                    if cid in seen:
                        continue
                    try:
                        payload = json.loads(sr["payload"] or "{}")
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    # Select the first nonempty string-like field by priority.
                    for key in ("h1", "h2", "h3", "title", "heading", "text"):
                        v = payload.get(key)
                        if isinstance(v, str) and v.strip():
                            first_title_map[cid] = v.strip()[:200]
                            seen.add(cid)
                            break
                    else:
                        # No title-like field here; advance to the next section without marking seen.
                        continue
        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "workspace_id": r["workspace_id"],
                "title": r["title"],
                "user_id": r["user_id"],  # S5: owner; shared_by source for shared-with-me list
                "status": r["status"],
                "quarantined_count": int(r["quar_count"] or 0),
                "effective_status": _effective_status(r["status"], int(r["quar_count"] or 0)),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "sections_count": int(r["sec_count"] or 0),
                "first_section_title": first_title_map.get(r["id"]),
            }
            for r in rows
        ]

    async def count_by_workspace_for_user(
        self,
        *,
        user_id: str,
        workspace_id: str | None = None,
        q: str | None = None,
        include_archived: bool = True,
        session_ids: list[str] | None = None,
        min_sections: int = 0,
        min_visible_sections: int = 0,
        exclude_user_id: str | None = None,
    ) -> dict[str, int]:
        """Count report-library rows per Space with exactly the filters of :meth:`list_for_user`.

        The gateway applies Space visibility per ``workspace_id``; grouping here lets it
        sum only the Spaces the caller may read, so ``total`` never counts rows the
        list would hide.
        """
        if session_ids is not None and not session_ids:
            return {}
        where_sql, params = self._report_list_where(
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_ids=None,
            exclude_user_id=exclude_user_id,
            q=q,
            include_archived=include_archived,
            session_ids=session_ids,
            min_sections=min_sections,
            min_visible_sections=min_visible_sections,
        )
        sql = (
            f"SELECT c.workspace_id, COUNT(*) FROM canvases c WHERE {where_sql} AND {await self._account_visibility_sql('c')} GROUP BY c.workspace_id"
        )
        async with open_shared_ctx(self._path) as db, db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    async def list_workspace_ids_for_user(
        self,
        *,
        user_id: str,
        session_ids: list[str] | None = None,
        exclude_user_id: str | None = None,
    ) -> list[str]:
        """Distinct Spaces of the report-library base set (owner or shared sessions), unfiltered.

        Used to decide Space visibility for every page without running the filtered count.
        """
        if session_ids is not None and not session_ids:
            return []
        where_sql, params = self._report_list_where(
            user_id=user_id,
            workspace_id=None,
            workspace_ids=None,
            exclude_user_id=exclude_user_id,
            q=None,
            include_archived=True,
            session_ids=session_ids,
            min_sections=0,
            min_visible_sections=0,
        )
        sql = f"SELECT DISTINCT c.workspace_id FROM canvases c WHERE {where_sql} AND {await self._account_visibility_sql('c')}"
        async with open_shared_ctx(self._path) as db, db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    def _report_list_where(
        self,
        *,
        user_id: str,
        workspace_id: str | None,
        workspace_ids: list[str] | None,
        exclude_user_id: str | None,
        q: str | None,
        include_archived: bool,
        session_ids: list[str] | None,
        min_sections: int,
        min_visible_sections: int,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE clause shared by report-library listing and counting."""
        params: list[Any]
        if session_ids is not None:
            _ph = ",".join("?" for _ in session_ids)
            clauses = [f"c.session_id IN ({_ph})"]
            params = list(session_ids)
        else:
            clauses = ["c.user_id = ?"]
            params = [user_id]
        if exclude_user_id:
            clauses.append("AND c.user_id != ?")
            params.append(exclude_user_id)
        if workspace_ids is not None:
            _wph = ",".join("?" for _ in workspace_ids)
            clauses.append(f"AND c.workspace_id IN ({_wph})")
            params.extend(workspace_ids)
        if workspace_id:
            clauses.append("AND c.workspace_id = ?")
            params.append(workspace_id)
        if not include_archived:
            clauses.append("AND c.status != 'archived'")  # R2: archived excluded from retrieval
        if min_visible_sections > 0:
            clauses.append("AND (SELECT COUNT(*) FROM canvas_sections sv WHERE sv.canvas_id = c.id AND sv.hidden_for_chat_mode = 0) >= ?")
            params.append(min_visible_sections)
        if min_sections > 0:
            # empty-canvas cleanup: filter before LIMIT so empty shells consume no slots.
            # ★116 dogfood 1.0.93 P3: count only **visible report content**. All sections in chat-mode turns
            # have hidden_for_chat_mode=1 (users have no report); the previous raw count made pure chat sessions
            # appear in recent outputs as empty Untitled report entries.
            # superseded/quarantined/meta likewise do not count as content.
            clauses.append(
                "AND (SELECT COUNT(*) FROM canvas_sections s3 WHERE s3.canvas_id = c.id"
                " AND s3.hidden_for_chat_mode = 0 AND s3.superseded = 0"
                " AND s3.quarantined = 0 AND s3.kind != 'meta') >= ?"
            )
            params.append(min_sections)
        if q and q.strip():
            qn = q.strip()
            like = f"%{qn}%"
            if self._fts_enabled:
                # R4: full text (section body) ∪ title/id LIKE.
                # ≥3 characters → trigram MATCH (indexed, fast);
                # <3 characters → substring LIKE scan on ordinary canvas_sections.payload (two-character CJK words are common).
                # feedback-0713 #11: never run the <3-character LIKE fallback on the canvas_fts virtual table:
                # FTS5 trigram tables take LIKE constraints into their index; when patterns under three characters
                # cannot yield complete trigrams, production SQLite 3.40.1 silently returns an empty result instead of
                # falling back to linear scan (dev/CI 3.51 works correctly, masking this in tests). Payload is persisted
                # with ensure_ascii=False, so CJK text supports LIKE directly; a few hundred sections
                # pose no performance problem for linear scanning.
                body_clause: str | None
                body_param: Any = None
                if len(qn) >= 3:
                    body_clause = "c.id IN (SELECT canvas_id FROM canvas_fts WHERE canvas_fts MATCH ?)"
                    body_param = _fts_match_query(qn)
                elif any(ord(ch) > 0x7F for ch in qn):
                    # <3 characters with non-ASCII (two-character CJK words: stores/profit etc.) → scan ordinary-table body.
                    body_clause = "c.id IN (SELECT canvas_id FROM canvas_sections WHERE payload LIKE ?)"
                    body_param = like
                else:
                    # feedback-0713 F5-#11 narrowing: LIKE on raw JSON payload for one/two ASCII characters
                    # matches key names/structural text (id/kg etc.), generating many uninformative false positives.
                    # Fall back to title/id LIKE, like the FTS-unavailable path; CJK unaffected.
                    body_clause = None
                if body_clause is not None:
                    clauses.append(f"AND (c.title LIKE ? OR c.id LIKE ? OR {body_clause})")
                    params.extend([like, like, body_param])
                else:
                    clauses.append("AND (c.title LIKE ? OR c.id LIKE ?)")
                    params.extend([like, like])
            else:
                clauses.append("AND (c.title LIKE ? OR c.id LIKE ?)")
                params.extend([like, like])
        return " ".join(clauses), params

    # ─── quarantine (i18n v1 R4) ────────────────────────────
    async def set_quarantine(
        self,
        *,
        section_id: str,
        quarantined: bool,
        reason: str | None = None,
    ) -> bool:
        """标 / 解除 quarantine. 返 True 如果命中一行.

        admin 调（quarantine_legacy_canvas.py 跑批 + 人工 review 后）.
        export pipeline 看到 quarantined=1 拒绝导出.
        """
        async with open_shared_ctx(self._path) as db:
            cur = await db.execute(
                "UPDATE canvas_sections SET quarantined=?, quarantine_reason=?, updated_at=? WHERE id=?",
                (1 if quarantined else 0, reason, _now(), section_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def count_quarantined(self, canvas_id: str) -> int:
        """R2: 数 canvas 内 quarantined section(报告头 effective_status 派生用, 比 list 轻)."""
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT COUNT(*) FROM canvas_sections WHERE canvas_id=? AND quarantined=1",
                (canvas_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_quarantined_for_canvas(self, canvas_id: str) -> list[Section]:
        """列 canvas 内所有 quarantined section. export pipeline 用来 reject 决策."""
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM canvas_sections WHERE canvas_id=? AND quarantined=1 ORDER BY seq",
                (canvas_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_section(r) for r in rows]

    # ─── revisions ─────────────────────────────────────────
    async def list_revisions(
        self,
        *,
        canvas_id: str,
        limit: int = 100,
    ) -> list[Revision]:
        """最近 ``limit`` 条修改（新→旧），每条带整份报告内的 AI 轮次序号 ``turn_ordinal``。

        序号在全部修改记录上算（不受 ``limit`` 窗口影响），与窗口查询同一连接。
        聚合排在窗口查询之后：窗口里出现的轮次一定已提交、必在聚合结果里。
        """
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM canvas_revisions WHERE canvas_id=? ORDER BY created_at DESC LIMIT ?",
                (canvas_id, limit),
            ) as cur:
                rows = await cur.fetchall()
            async with db.execute(
                _TURN_FIRST_WRITE_SQL,
                (canvas_id, len(MACHINE_TURN_PREFIX), MACHINE_TURN_PREFIX),
            ) as cur:
                turn_rows = await cur.fetchall()
        ordinals = _turn_ordinals((row["by_turn_id"], row["first_at"]) for row in turn_rows)
        revisions = [_row_to_revision(r) for r in rows]
        for revision in revisions:
            if revision.by_turn_id:
                revision.turn_ordinal = ordinals.get(revision.by_turn_id)
        return revisions

    async def list_sections_appended_by_turn(
        self,
        turn_id: str,
        *,
        kinds: list[str] | None = None,
        include_patches: bool = False,
    ) -> list[Section]:
        """Phase 1.6 G4 — return sections this turn appended, optionally
        filtered by kind.

        Used by `lead_orchestrator._collect_inline_artifacts` to surface the
        chart / kpi_row / data_table that the turn produced in the chat
        bubble (ChatBubbleNew.artifacts) alongside the prose final answer.

        Joins through `canvas_revisions` (ledger of ops) to find appends tied
        to this turn — `canvas_sections` itself has no per-turn column. Only
        `op='append'` is considered; patches / reorders / deletes don't
        surface as new artifacts.

        `include_patches` also returns sections revised this turn for source warnings.
        Empty `turn_id` or no matches → []. Section.seq order preserved.
        """
        if not turn_id:
            return []
        operation_filter = "r.op IN ('append','patch')" if include_patches else "r.op = 'append'"
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                async with db.execute(
                    "SELECT DISTINCT s.* FROM canvas_sections s "
                    "JOIN canvas_revisions r ON r.section_id = s.id "
                    f"WHERE r.by_turn_id = ? AND {operation_filter} "
                    f"AND s.kind IN ({placeholders}) "
                    "ORDER BY s.seq",
                    (turn_id, *kinds),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    "SELECT DISTINCT s.* FROM canvas_sections s "
                    "JOIN canvas_revisions r ON r.section_id = s.id "
                    f"WHERE r.by_turn_id = ? AND {operation_filter} "
                    "ORDER BY s.seq",
                    (turn_id,),
                ) as cur:
                    rows = await cur.fetchall()
        return [_row_to_section(r) for r in rows]

    async def list_revisions_by_turn(self, turn_id: str) -> list[tuple[str, dict[str, Any]]]:
        """批 B1-0：本轮 patch 行的 (section_id, prev_payload)，按 `created_at, rowid` 升序——同秒多次 patch 靠 rowid 定序。
        修订收尾器据此取每段**首条**行的 prev（= 本轮改之前的正文），算被丢弃的 figure 引用 / 被改掉的旧值。只读。"""
        if not turn_id:
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT section_id, prev_payload FROM canvas_revisions WHERE by_turn_id=? AND op='patch' AND section_id IS NOT NULL "
                "ORDER BY created_at, rowid",
                (turn_id,),
            ) as cur:
                async for row in cur:
                    try:
                        prev = json.loads(row["prev_payload"] or "{}")
                    except Exception:
                        prev = {}
                    out.append((str(row["section_id"]), prev if isinstance(prev, dict) else {}))
        return out

    async def section_ids_touched_by_turn(self, turn_id: str) -> set[str]:
        """RC-8 (report final gate) — section ids this turn appended OR patched.

        「本轮重算过没有」的判据:`Section.turn_id` 冻结在 append 轮(patch 不更新),
        真值在 canvas_revisions 台账。op∈{append,patch} 都算 touched —— D6 dedup
        的 kpi→line upgrade 走 patch,同样是本轮重算。reorder/delete 不算(没动数据)。

        NB 这是「本轮动过」的 proxy:title-only patch 也计入(可接受的 FN,见
        rc8-final-gate-design.md P2-4)。空 turn_id → 空集。
        """
        if not turn_id:
            return set()
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT DISTINCT section_id FROM canvas_revisions WHERE by_turn_id = ? AND op IN ('append','patch')",
                (turn_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return {str(r[0]) for r in rows if r[0]}

    async def count_report_sections_saved_for_turn(self, turn_id: str) -> int:
        """本轮落库、刷新后报告正文里能看到的节数（终态 ``done.report_sections_saved``）。

        口径与前端刷新后的渲染一致（by-session 回包 → ``selectReportSurfaceSections``
        → ReportView ``currentSections`` / ``preprocessSections``）：

        - 只数 ``canvas_sections.turn_id`` 是本轮的段（与前端「本轮写过这份报告」同一判据）；
        - 去掉 chat-mode 隐藏（by-session 不返）、已折叠进历史（``superseded``）的段；
        - 去掉 SQL / 代码 / 裸表（``code_block`` / ``data_table``，报告面不显示）；
        - 去掉被未折叠 ``insight_card`` 的证据引用、内嵌在卡片里的 chart / image（不单独成节）。

        只流给前端、没落库的段（Lead 流式预览、被剪掉的半截）不在库里，自然不计。
        """
        if not turn_id:
            return 0
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            async with db.execute(
                "SELECT DISTINCT canvas_id FROM canvas_revisions WHERE by_turn_id = ? AND canvas_id IS NOT NULL",
                (turn_id,),
            ) as cur:
                canvas_ids = [str(row[0]) for row in await cur.fetchall() if row[0]]
            rows: list[aiosqlite.Row] = []
            for canvas_id in canvas_ids:
                async with db.execute(
                    "SELECT * FROM canvas_sections WHERE canvas_id = ? AND hidden_for_chat_mode = 0 AND superseded = 0 ORDER BY seq, id",
                    (canvas_id,),
                ) as cur:
                    rows.extend(await cur.fetchall())
            await db.commit()
        sections = [_row_to_section(row) for row in rows]
        embedded: set[str] = set()
        for section in sections:
            if section.kind != "insight_card":
                continue
            evidence = section.payload.get("evidence")
            if not isinstance(evidence, list):
                continue
            for item in evidence:
                if isinstance(item, dict) and isinstance(item.get("chart_section_id"), str):
                    embedded.add(item["chart_section_id"])
        return sum(
            1
            for section in sections
            if section.turn_id == turn_id
            and section.kind not in _REPORT_SURFACE_EXCLUDED_KINDS
            and not (section.kind in ("chart", "image") and section.id in embedded)
        )

    async def count_sections_for_turn(self, turn_id: str) -> int:
        """Sprint ρ Phase A — count sections appended by a specific turn.

        Used by `_compute_response_kind_v2` at the post-flush authoritative
        chokepoint (lead_orchestrator.py callsite 4). Counts append-op
        revisions tied to `turn_id` via `canvas_revisions.by_turn_id` —
        the `canvas_sections` table itself has no per-turn column, so we
        join through the revisions ledger.

        Counts only `op='append'` to honor "did this turn write anything
        new to canvas?" semantics; patches / reorders / deletes don't
        create new sections.

        Single SQL, ≤1ms at chokepoint.
        """
        if not turn_id:
            return 0
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT COUNT(*) FROM canvas_revisions WHERE by_turn_id = ? AND op = 'append'",
                (turn_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0


def _row_to_canvas(row: aiosqlite.Row) -> Canvas:
    return Canvas(
        id=row["id"],
        session_id=row["session_id"],
        workspace_id=row["workspace_id"],
        user_id=row["user_id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _normalize_legacy_kind(kind: str, payload: dict[str, Any], sid: str) -> tuple[str, dict[str, Any]]:
    """读侧历史别名归一 —— 目前只有 `sql_block` → `code_block`。

    `sql_block` 的注释从 Sprint ι 起就写着「legacy alias for code_block，90 天 grace」
    （PRD §4.A.2），但**读侧别名一直没人写**，结果并行养了一整个 kind：一套 payload、
    一个 NodeView、四张导出映射表、一条 surface policy。语义上它就是 `lang="sql"` 的
    code_block（后端两者的 SurfaceKindPolicy 逐字相同），2026-09-05 把 grace 做完。

    ⚠ 归一发生在**读**，不改库。副作用是：若用户编辑同一画布并整篇回写，这一段会以
    `code_block` 落库（顺带完成迁移）。这是想要的行为，但它是隐式的，记在这里。

    ⚠ 产出侧早已没人写 `sql_block`：`realtime_stream` 从 Sprint ι 起走
    `_map_code_block`；116 全库 8904 段 + 10233 条修订里 0 条。
    """
    if kind != "sql_block":
        return kind, payload
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return "code_block", {
        # code_block requires call_id; synthesize a traceable one if legacy sql_block lacks it.
        "call_id": (meta or {}).get("call_id") or f"legacy_sql_{sid}",
        "lang": "sql",
        "code": payload.get("sql") or "",
        **({"meta": meta} if meta else {}),
    }


def _row_to_section(row: aiosqlite.Row) -> Section:
    keys = row.keys() if hasattr(row, "keys") else []
    _kind, _payload = _normalize_legacy_kind(row["kind"], json.loads(row["payload"]), row["id"])
    return Section(
        id=row["id"],
        canvas_id=row["canvas_id"],
        seq=int(row["seq"]),
        kind=_kind,
        payload=_payload,
        rev=int(row["rev"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        # i18n v1 R4: old rows without quarantined default to False (old-database compatibility).
        quarantined=bool(row["quarantined"]) if "quarantined" in keys else False,
        quarantine_reason=(row["quarantine_reason"] if "quarantine_reason" in keys else None),
        # D6: hidden flag defaults to False for old rows
        hidden_for_chat_mode=(bool(row["hidden_for_chat_mode"]) if "hidden_for_chat_mode" in keys else False),
        surface_rationale=(row["surface_rationale"] if "surface_rationale" in keys else None),
        surface_target=(row["surface_target"] if "surface_target" in keys else None),
        # report-engine Phase-2 WP-A: missing owner column in old rows → None (claimable)
        owner=(row["owner"] if "owner" in keys else None),
        # α v1: collapse-in-place superseded marker; old rows without it → False
        superseded=(bool(row["superseded"]) if "superseded" in keys else False),
        superseded_by_turn=(row["superseded_by_turn"] if "superseded_by_turn" in keys else None),
        # α v1: denormalized originating turn; old rows without it → None
        turn_id=(row["turn_id"] if "turn_id" in keys else None),
    )


def _parse_restore_envelope(prev_payload: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """方案A: delete revision 的 prev_payload 若是恢复 envelope 则返回 dict, 否则
    None (旧格式裸 payload / 解析失败)。同时接受已解析的 dict (list_revisions 路)。"""
    if prev_payload is None:
        return None
    if isinstance(prev_payload, dict):
        data: dict[str, Any] | None = prev_payload
    else:
        try:
            parsed = json.loads(prev_payload)
            data = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            data = None
    if data is not None and data.get("restore_schema") == 1:
        return data
    return None


def _row_to_revision(row: aiosqlite.Row) -> Revision:
    return Revision(
        id=row["id"],
        canvas_id=row["canvas_id"],
        section_id=row["section_id"],
        op=row["op"],
        prev_payload=json.loads(row["prev_payload"]) if row["prev_payload"] else None,
        new_payload=json.loads(row["new_payload"]) if row["new_payload"] else None,
        by_turn_id=row["by_turn_id"],
        created_at=row["created_at"],
    )
