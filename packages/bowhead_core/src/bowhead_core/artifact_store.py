"""Artifact store — 统一存 canvas export / 用户 upload / skill 产物文件.

**架构**: 元数据落 SQLite `artifacts` 表；文件实体走 pluggable `ArtifactBackend`.

Backend 两种实现：
    - ``LocalBackend``  — 写本地盘，下载走 FileResponse（dev 默认）
    - ``S3Backend``     — 写 MinIO / S3，下载走 302 presigned URL（prod / 共享存储）

选哪个由 gateway 装配时决定（env ``BOWHEAD_ARTIFACT_BACKEND=local|s3``）；
``ArtifactStore`` 本身不关心存哪。

**为什么 path 是 str 不是 Path**：
    Local 下 path 是绝对文件路径，S3 下 path 是 bucket key —— 同一字段承载两种语义，
    调用方通过 ``store.open_read(art)`` / ``store.download_url(art)`` 消费，不直接解释.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import IO, TYPE_CHECKING, Any, ClassVar, Final, Literal, Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)

_STAGED_EXPORT_BLOB_MARKER = ".bowhead-export-staged-"
_STAGED_EXPORT_OBJECT_PREFIX = "staged-export/"
_STAGED_EXPORT_ORPHAN_GRACE = timedelta(hours=1)
_IMMUTABLE_UPLOAD_PREFIX = "immutable-upload/"


class ArtifactReadLimitError(ValueError):
    """A bounded backend read exceeded its caller's byte budget."""


import aiosqlite
import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence

# CREATE TABLE only, without indexes on new columns (CREATE INDEX fails on old DBs before ALTER).
# Create indexes on new columns after initialize()'s ALTER migrations.
SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    mime            TEXT NOT NULL,
    size            INTEGER NOT NULL,
    source          TEXT NOT NULL,            -- 'upload' | 'canvas_export' | 'sandbox_output'
    path            TEXT NOT NULL,            -- local: abs path; s3: bucket key
    canvas_id       TEXT,
    created_by      TEXT,
    created_at      TEXT NOT NULL,
    deleted_at      TEXT,                     -- 软删；NULL=active，有值=回收站
    last_used_at    TEXT,                     -- 最后一次被 attach 到 turn 的时间
    category        TEXT,                     -- 业务分类（LLM / 用户赋 / rule）
    category_source TEXT,                     -- 'llm' | 'user' | 'rule'
    row_count       INTEGER,                  -- 表格类产物的行数；非表格为 NULL
    workspace_id    TEXT                      -- 产物所属空间；NULL=未归属（**不是**跨空间）
);
CREATE INDEX IF NOT EXISTS idx_artifacts_canvas  ON artifacts(canvas_id, created_at DESC);
"""

# Sprint ι (PRD §4.B.1): sibling table holds full sandbox stdout (≤8KB CHECK
# guards against runaway log dumps). Joined lazily on detail expand. FK CASCADE
# auto-deletes stdout when parent artifact hard-deleted (requires PRAGMA
# foreign_keys=ON via _open_db() helper — see §1.3.1).
_STDOUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_stdout (
    artifact_id TEXT PRIMARY KEY,
    stdout      TEXT NOT NULL CHECK (length(stdout) <= 8192),
    created_at  TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_artifact_stdout_created
    ON artifact_stdout(created_at);
"""

_UPLOAD_BINDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_upload_bindings (
    artifact_id  TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT '',
    user_id      TEXT NOT NULL,
    session_id   TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_artifact_upload_bindings_workspace
    ON artifact_upload_bindings(workspace_id, artifact_id);
"""

_DESIGN_FLATTEN_EXPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS design_flatten_exports (
    artifact_id            TEXT PRIMARY KEY,
    canvas_id              TEXT NOT NULL,
    section_id             TEXT NOT NULL,
    section_rev            INTEGER NOT NULL,
    size_preset            TEXT NOT NULL,
    background_artifact_id TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_design_flatten_exports_section
    ON design_flatten_exports(canvas_id, section_id, section_rev);
"""


# Filenames may become Local paths or part of S3 keys; strip separators / control characters / nulls
# to prevent path injection and keep S3 keys valid. Preserve Chinese characters, common symbols and extensions.
_UNSAFE_NAME_RE = re.compile(r"[/\\\x00-\x1f\x7f]+")


def _safe_filename(name: str, max_len: int = 200) -> str:
    cleaned = _UNSAFE_NAME_RE.sub("_", (name or "").strip()) or "upload"
    # Strip leading/trailing dots (prevent edge cases such as ".htaccess" / "...")
    cleaned = cleaned.strip(".")
    if not cleaned:
        cleaned = "upload"
    return cleaned[:max_len]


def _now() -> str:
    # Results snapshot pagination uses (created_at, artifact_id) as its immutable
    # high-water.  Second precision lets a later random ID sort behind an
    # existing same-second ID and leak into an old snapshot.  Microseconds make
    # the persisted order track creation order; membership digest checks remain
    # the fail-closed guard for the exceptional exact-timestamp collision.
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


# ─────────────────────────────────────────────────────────────
# Asset model (Sprint pptx-from-template M1, RD-6)
# ─────────────────────────────────────────────────────────────
# (inject, trust) defaults per kind. Stored explicitly on the row so a
# user/agent can override later without a code change.
_KIND_DEFAULTS: dict[str, tuple[str, str]] = {
    "user_upload": ("full", "trusted"),
    "tool_data": ("list", "trusted"),
    "sandbox_output": ("stub", "trusted"),
    "deliverable": ("stub", "trusted"),
    "canvas_export": ("stub", "trusted"),
    "web_clip": ("stub", "unverified"),
    # Team-branding assets are presentation metadata, never agent context.
    "branding_asset": ("none", "trusted"),
}

# Legacy `source` → `kind` backfill map. Derived from the ACTUAL written source
# set (grep of every put_bytes/put_file call site), NOT the documented enum.
# CI contract test (test_asset_kind_map_complete) asserts every distinct source
# produced in the codebase has an entry here, so a new producer can't silently
# land legacy rows in the wrong injection class.
_SOURCE_TO_KIND: dict[str, str] = {
    "upload": "user_upload",
    "user_upload": "user_upload",
    "kb_upload": "user_upload",
    "bi_fetch_rows": "tool_data",
    "template_clone_section_data": "tool_data",
    "python_sandbox": "sandbox_output",
    "sandbox_output": "sandbox_output",
    "canvas_export": "canvas_export",
    "canvas_export_clone": "canvas_export",
    "canvas_export_skill": "deliverable",
    "community_document_skill": "deliverable",
    "web_search": "web_clip",
    "web_fetch": "web_clip",
    "web_extract": "web_clip",
    "web_crawl": "web_clip",
    "web_deep_research": "web_clip",
    "image_generate": "tool_data",
    "design_flatten": "canvas_export",
    "consultant_deck": "deliverable",
    # ZIP bundle roots are user-owned inputs in Files, but Results filters the
    # grouping carrier itself and presents only session-bound input children.
    "kb_bundle_upload": "user_upload",
    "team_branding": "branding_asset",
}
# Unknown / new source → list-only tool_data (safest: never floods context).
_DEFAULT_KIND = "tool_data"

# PRAGMA user_version marker so the one-shot backfill runs exactly once.
_ARTIFACT_USER_VERSION = 1
# file-management-v2 P1: second one-time backfill (correct sections.json kind).
_ARTIFACT_SECTIONS_JSON_VERSION = 2


def _kind_for_source(source: str) -> str:
    return _SOURCE_TO_KIND.get(source or "", _DEFAULT_KIND)


def _defaults_for_kind(kind: str) -> tuple[str, str]:
    """Return (inject, trust) defaults for a kind."""
    return _KIND_DEFAULTS.get(kind, ("list", "trusted"))


@contextlib.asynccontextmanager
async def _open_db(db_path: str) -> AsyncIterator[aiosqlite.Connection]:
    """Sprint ι (P1 #1): unified aiosqlite connect helper that:
    - sets PRAGMA foreign_keys=ON per-connection (required for FK CASCADE on
      artifact_stdout, see §1.3) — SQLite default is OFF and is per-connection.
    - reserves single hook for future per-conn settings (busy_timeout, etc.).

    All call sites in artifact_store.py route through this. Direct
    `aiosqlite.connect(self.db_path)` in this file is now a lint violation
    (contract test in test_iota_aiosqlite_contract.py enforces).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


#: Four-level provenance inference for artifact Space ownership, as a SQL expression (caller supplies artifacts rows aliased as ``a``).
#:
#: ★ The repository's sole SQL ladder: backfill and measurement scripts use this. `files.py:_artifact_workspace_ids`
#:   is its Python equivalent for authorization; single-item `_artifact_workspace_id` is a special case,
#:   batched for global search on 2026-09-24 without changing tier semantics. `test_artifact_workspace_id_migration.py` enforces equivalence.
#:
#: Match `files.py` tier by tier; this is not a redesign:
#:   ① ``artifact_upload_bindings.workspace_id``: explicitly registered ownership at upload;
#:   ②③④ resolve one session_id (created_by_turn → canvas_id → earliest pinned
#:       session_attachment), then derive ownership from ``sessions.workspace_id``.
#:
#: ★ Normalize all empty strings to NULL: ``artifact_upload_bindings.workspace_id`` is ``NOT NULL DEFAULT ''``;
#:   team-level KB uploads write empty strings (`kb_presign.py`: ``ctx.workspace_id if scope != "team" else ""``),
#:   which are not Space IDs. Equivalent to `files.py:_upload_binding_workspaces`'s ``if workspace_id``.
#:
#: ★ ②③④ share one session lookup, rather than separately COALESCE to workspace: if session_id resolves
#:   but its sessions row is absent, `files.py` returns "" without falling back to the next tier. Match this here,
#:   or backfill distributions differ from today's authorization inference and batch 4 enables gates using mismatched numbers.
ARTIFACT_WORKSPACE_PROVENANCE_SQL = """
COALESCE(
    (SELECT NULLIF(b.workspace_id, '') FROM artifact_upload_bindings b WHERE b.artifact_id = a.id),
    (SELECT NULLIF(s.workspace_id, '') FROM sessions s WHERE s.id = COALESCE(
        (SELECT t.session_id FROM turns t WHERE t.id = a.created_by_turn),
        (SELECT c.session_id FROM canvases c WHERE c.id = a.canvas_id),
        (SELECT sa.session_id FROM session_attachments sa
          WHERE sa.artifact_id = a.id ORDER BY sa.pinned_at ASC LIMIT 1)
    ))
)
"""

#: Explicit Space sentinel for team-level artifacts (Space isolation batch 4 ①).
#:
#: An explicit value is needed because batch 4 makes NULL `workspace_id` fail closed, while team artifacts
#: (branding assets, team-level KB uploads) inherently belong to no single Space. Keeping them NULL
#: invalidates the criterion that NULL indicates a missed write site, also blocking team resources.
#: ★ Implement ① before ③; reversing the order blocks branding assets and team KB files together.
#:
#: Three prerequisites (`artifact-workspace-backfill.md` §7.2):
#:   1. No collisions: real Space IDs are always `_id("ws")` → `ws_<hex>`; a sentinel without the `ws_` prefix cannot collide.
#:   2. Cannot be forged, but not because client strings cannot enter: that explanation was wrong, corrected in review on 2026-09-17.
#:      `files_presign.py` does write the `X-Space-Id` header into this column. The real defense is authorization before writing:
#:      nonempty header workspaces must pass `build_access_context` (team ownership + Space membership + lifecycle);
#:      unknown/nonmember workspaces return 403, so callers cannot persist IDs they do not belong to, much less `__team__`
#:      (not a real Space; no ownership can be found).
#:      ⚠ This is reasoning, not an enforced mechanism. Enforcement would assert at write time that IDs start with `ws_` or equal the sentinel;
#:      not done in this batch (legacy DBs have numeric IDs such as `1`/`355`; strict assertions need separate impact assessment).
#:   3. ❌ Do not reuse `""`: it already denotes team/personal scope in `kb_files` / `artifact_upload_bindings`,
#:      but is falsy and `files.py`'s `if not workspace_id: return` silently
#:      treats it as fail-open. `__team__` is truthy and requires explicit handling.
TEAM_SCOPE_WORKSPACE_ID: Final = "__team__"

#: Singleton table containing the cutover watermark (migration `0099_artifact_workspace_cutover`):
#: records when `artifacts.workspace_id` became trustworthy on this instance.
ARTIFACT_WORKSPACE_CUTOVER_TABLE: Final = "artifact_workspace_schema_meta"


async def read_artifact_workspace_cutover(db: aiosqlite.Connection) -> str | None:
    """切点水位；**没有水位就返回 None**（调用方据此保持今天的行为）。

    ⚠ 表不存在时返回 None 而不是抛：本表**故意不进** `initialize()` 的 ALTER 循环
    （`数据库迁移.md`「不得新增 Store initialize() DDL」，理由见 `0099` 头注），
    所以 store-only 老库、以及任何尚未升到 `0099` 的库都没有它。
    缺失 ⇒ 没有水位 ⇒ 一切判为「切点前」⇒ **逐字节今天的行为**，这是安全方向。
    """
    try:
        async with db.execute(
            f"SELECT activated_at FROM {ARTIFACT_WORKSPACE_CUTOVER_TABLE} WHERE singleton=1",  # noqa: S608
        ) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return str(row[0]) if row and row[0] else None


async def read_artifact_workspace_cutover_at(db_path: str) -> str | None:
    """`read_artifact_workspace_cutover` 的开库版（给没有现成连接的调用方 / 测试用）。"""
    try:
        async with _open_db(db_path) as db:
            return await read_artifact_workspace_cutover(db)
    except aiosqlite.OperationalError:
        return None


def _parse_iso_utc(value: str) -> datetime | None:
    """ISO8601 → aware datetime；解析不出来返回 None（调用方按「切点前」处理）。"""
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Date-only legacy rows (`2026-09-01`) parse as naive; interpret as UTC to avoid local-timezone drift.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def artifact_created_after_cutover(created_at: str | None, cutover_at: str | None) -> bool:
    """这条产物是不是**切点之后**创建的？判据只有这一份实现。

    ★ **必须真解析，不能拿字符串比大小**：库里的 `created_at` 格式并不统一
    （`2026-09-01` / `...+08:00` / `...Z` 都有），而水位是 `...Z`。字面比较会把
    `2026-09-17T09:00:00+08:00`（= 01:00Z）判成晚于 `2026-09-17T02:00:00Z`，**正好判反**
    ——那会把一条切点前的老产物 fail-close 掉。真值表与正对照见
    `test_artifact_workspace_cutover_migration.py`。

    ★ 三种「不知道」一律返回 False（⇒ 切点前 ⇒ fail-open）：没有水位、没有创建时间、
    解析不出来。批4 的口径是「不为历史买单」，**拿不准就不拦**是唯一安全的方向。
    """
    if not cutover_at or not created_at:
        return False
    created = _parse_iso_utc(created_at)
    cutover = _parse_iso_utc(cutover_at)
    if created is None or cutover is None:
        return False
    return created >= cutover


async def _resolve_write_workspace_id(
    db: aiosqlite.Connection,
    *,
    session_id: str | None,
    created_by_turn: str | None,
    canvas_id: str | None,
) -> str | None:
    """写入那一刻的空间归属；推不出返回 None（**不猜**）。

    与上面的回填梯子同源，但**级次略有不同且必须如此**：写入时
    ``artifact_upload_bindings`` / ``session_attachments`` 两张表还没有本产物的行
    （前者由 presign 路由另写，后者在本次调用稍后才写），所以写入侧最直接的信号是
    调用方手上的 ``session_id``；turn / canvas 作为后备，口径与回填一致。

    ⇒ **不要求调用方传 ``workspace_id``**。这是有意的：本仓吃过「可选参数=静默降级到
    最弱档」的亏（5 个调用点 4 个传了 1 个漏）。这里漏传的调用方仍会由 session/turn/canvas
    推出归属，而不是静默落 NULL；显式 ``workspace_id`` 只是给**确实更知情**的调用方
    （presign 等有 ctx 但没有 session 的路）留的覆盖口。
    """
    try:
        resolved_session = (session_id or "").strip()
        if not resolved_session and created_by_turn:
            async with db.execute("SELECT session_id FROM turns WHERE id=?", (created_by_turn,)) as cur:
                row = await cur.fetchone()
            resolved_session = str(row[0] or "") if row else ""
        if not resolved_session and canvas_id:
            async with db.execute("SELECT session_id FROM canvases WHERE id=?", (canvas_id,)) as cur:
                row = await cur.fetchone()
            resolved_session = str(row[0] or "") if row else ""
        if not resolved_session:
            return None
        async with db.execute("SELECT workspace_id FROM sessions WHERE id=?", (resolved_session,)) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        # SessionStore / CanvasStore create `sessions` / `turns` / `canvases`, sharing the artifacts DB in production
        # (main.py:402-403/505 use the same db_path), but these tables may be absent in legacy store-only DBs and
        # tests running only `ArtifactStore.initialize()`.
        # Failed ownership inference must not fail the write; as in files.py:646, degrade to unassigned.
        return None
    return (str(row[0]) or None) if row and row[0] else None


@dataclass
class Artifact:
    id: str
    filename: str
    mime: str
    size: int
    source: str
    path: str  # local abs path OR s3 key
    canvas_id: str | None
    created_by: str | None
    created_at: str
    deleted_at: str | None = None
    last_used_at: str | None = None
    category: str | None = None
    category_source: str | None = None
    # Sprint ι (PRD §4.B.1): populated when source ∈ {python_sandbox, sandbox_output}.
    code_lang: str | None = None
    # Sprint pptx-from-template M1 (asset model): typed semantics per artifact.
    # kind = coarse semantic class (source-of-truth going forward); inject =
    # context-injection policy (full/stub/list); trust = trusted|unverified;
    # scope = session|user; producer_kind supersedes the overloaded `source`;
    # created_by_turn / derived_from = provenance + version chain;
    # manifest_artifact_id = section↔slide map for a deliverable deck (CR-1).
    kind: str = "tool_data"
    scope: str = "session"
    inject: str = "list"
    trust: str = "trusted"
    producer_kind: str | None = None
    created_by_turn: str | None = None
    derived_from: str | None = None
    manifest_artifact_id: str | None = None
    # Upload visibility fence. Legacy rows and direct puts are ready; staged
    # presign rows remain pending_upload until confirm commits the metadata.
    status: str = "ready"
    # file-management-v2 D7: long_term | one_off | None(undetermined → no automatic indexing)
    retention_hint: str | None = None
    # The original query records which question produced this dataset, including its
    # qualifiers; a filename alone cannot establish scope. NULL means unrecorded
    # (existing rows or non-query outputs), not an empty query.
    origin_query: str | None = None
    #: Row count for tabular artifacts. Available only at write time: `tool_registry` already has
    #: `len(csv_rows)` when persisting BI artifacts, previously discarded after prompt text. Non-tabular artifacts use None.
    row_count: int | None = None
    # The Artifact's Space. NULL means ownership cannot be inferred; it does not
    # mean the empty Space or public access. This field stores provenance; callers
    # must apply the authorization policy when deciding whether it can be read.
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResultsArtifactSnapshot:
    high_water_created_at: str | None
    high_water_artifact_id: str | None
    membership_cutoff_at: str | None = None

    def __post_init__(self) -> None:
        if (self.high_water_created_at is None) != (self.high_water_artifact_id is None):
            raise ValueError("results snapshot high-water fields must be paired")


@dataclass(frozen=True, slots=True)
class ResultsArtifactEntry:
    artifact: Artifact
    position: int
    sort_tuple: tuple[int | str | None, ...]


@dataclass(frozen=True, slots=True)
class ResultsArtifactGroupCounts:
    deliverables: int
    evidence: int
    data: int
    code: int
    input: int


@dataclass(frozen=True, slots=True)
class ResultsArtifactQueryPage:
    entries: tuple[ResultsArtifactEntry, ...]
    authorized_total: int
    matched_total: int
    group_counts: ResultsArtifactGroupCounts
    has_more_in_direction: bool


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    """Blob written to the backend but not yet visible in artifact metadata."""

    artifact: Artifact
    session_id: str | None


@dataclass(frozen=True)
class DesignFlattenProvenance:
    artifact_id: str
    canvas_id: str
    section_id: str
    section_rev: int
    size_preset: str
    background_artifact_id: str
    created_at: str


# Sprint file-upload-presign (2026-05-27): presigned-POST envelope for direct
# browser→backend upload bypassing gateway. See tech-design §5.1 / §6.2.
@dataclass(frozen=True)
class PresignPostResult:
    url: str  # POST endpoint (bucket root, path-style)
    fields: dict[str, str]  # signed policy fields incl. key, Content-Type, x-amz-*
    path: str  # backend key the object WILL be stored at
    expires_at: datetime


@dataclass(frozen=True)
class ObjectHead:
    size: int
    etag: str | None
    mime: str | None
    identity: str | None = None


# mime → business family (frontend icons / filters)
def mime_to_family(mime: str) -> str:
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m in {"video/mp4", "video/webm", "video/ogg"}:
        return "video"
    if m == "application/pdf":
        return "pdf"
    if "presentationml" in m or "presentation" in m:
        return "presentation"
    if "spreadsheetml" in m or "spreadsheet" in m or m in {"text/csv", "text/tab-separated-values", "application/vnd.ms-excel"}:
        return "spreadsheet"
    if "wordprocessingml" in m or "word" in m:
        return "document"
    if m.startswith("text/") or m in ("application/json", "application/x-ndjson"):
        return "text"
    return "other"


# ─────────────────────────────────────────────────────────────
# Backend abstraction
# ─────────────────────────────────────────────────────────────
class ArtifactBackend(Protocol):
    """存 blob 的底层实现。Store 只关心逻辑 id / 元数据；blob I/O 全委托 backend."""

    name: str  # "local" | "s3"; for debugging

    async def put(
        self,
        *,
        aid: str,
        content: bytes,
        filename: str,
        mime: str,
    ) -> str:
        """写 blob；返回 path 字段（Local: abs filesystem path; S3: object key）."""
        ...

    async def read(self, path: str) -> bytes:
        """读 blob 内容（用于 agent 注入 / skill 分析）."""
        ...

    async def read_bounded(self, path: str, *, limit: int) -> bytes:
        """Read at most limit+1 bytes; missing objects raise FileNotFoundError."""
        ...

    def immutable_upload_path(self, token: str) -> str:
        """Reserve a server-only key; token is a service-generated UUID hex."""
        ...

    async def put_immutable_upload(self, path: str, *, content: bytes, mime: str) -> bool:
        """Create if absent; True means created, False means already exists."""
        ...

    async def download_url(
        self,
        *,
        path: str,
        filename: str,
        mime: str,
        expires_in: int = 3600,
        disposition: str = "attachment",
    ) -> str | None:
        """返回 presigned URL 供浏览器直连下载；None 表示不支持（调用方改用 read + 代理）.

        disposition="inline"（multimodal-v1）→ Content-Disposition 裸 ``inline``
        （**无 filename 段**，防 header 注入面），供 <img> 内联展示；默认
        "attachment" 行为逐字节不变。raster mime 白名单由 caller（gateway
        files.py）把关 —— backend 不做 mime 判断。
        """
        ...

    async def delete(self, path: str) -> None:
        """删 blob（软删 or 硬删 由 store 决定；这里只负责物理动作）."""
        ...

    # ── Sprint file-upload-presign (2026-05-27) ───────────────────
    async def presign_post(
        self,
        *,
        aid: str,
        filename: str,
        mime: str,
        size_bytes: int,
        expires_in: int = 3600,
    ) -> PresignPostResult | None:
        """Signed POST-form upload envelope (Conditions-bound Content-Type +
        content-length-range). Returns None if backend doesn't support presign
        (caller can fall back to small-upload path).
        """
        ...

    async def head(self, path: str) -> ObjectHead | None:
        """size/etag/mime of an object, or None if missing. Used by confirm
        endpoint to verify the browser PUT completed and re-validate MIME.
        """
        ...

    def read_stream(self, path: str) -> contextlib.AbstractAsyncContextManager[IO[bytes]]:
        """Yield a seekable file-like for the blob.

        **NOT awaitable**: this method returns an async context manager directly.
        Callers MUST use ``async with backend.read_stream(path) as f:``,
        NOT ``f = await backend.read_stream(path)``. The yielded ``f`` is a
        sync file handle (seekable, ``.read()``/``.seek()``), suitable for
        ``zipfile.ZipFile(f)``.

        S3 backend downloads to ``NamedTemporaryFile`` (cleanup on context exit
        including on consumer exception). Local backend opens the on-disk file
        directly.

        Bundle handler uses this to feed ``zipfile.ZipFile(stream)`` without
        ``io.BytesIO(raw)`` heap blowup under the 2 GB cap.
        """
        ...

    def iter_range(
        self,
        path: str,
        *,
        start: int,
        end: int,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        """Yield ``[start, end)`` bytes without buffering the whole object."""
        ...


# ─────────────────────────────────────────────────────────────
# Local implementation: writes to local disk
# ─────────────────────────────────────────────────────────────
# Sprint file-upload-presign: aid regex used by presign envelope + receiver
# route. Matches the uuid4().hex[:12] generation in put_bytes() (line 351).
_PRESIGN_AID_RE = re.compile(r"^art_[a-f0-9]{12}$")
_PRESIGN_JWT_AUD = "local-presign"

# cca P3 F-B — artifact-fetch token: a GATEWAY-signed, narrowly-SCOPED bearer the
# python_sandbox host-side runner carries on its artifact GET so the download owner-check
# runs as the REAL caller (not a dropped header / world-readable fetch). Signed with a
# DEDICATED secret (NOT the main JWT secret) + `aud="artifact-fetch"` → it is INVALID on
# every main-JWT per-user route, so it is genuinely scoped (review d3/S-1), never a
# full-user bearer. Short TTL (minted fresh per sandbox call).
_ARTIFACT_FETCH_JWT_AUD = "artifact-fetch"
# Minted once per python_sandbox call and reused across the WHOLE runner lifecycle —
# input materialization (fetch, front) AND output upload (POST, after the docker run). So
# the TTL must cover materialization + the sandbox wall-clock (BOWHEAD_SANDBOX_MAX_TIMEOUT,
# default 120s, prod-bumped) + upload. 600s gives comfortable headroom; the token is still
# narrowly scoped (aud=artifact-fetch + sub + dedicated secret → only the caller's OWN
# artifacts), so a turn-length TTL is acceptable (TTL bounds replay-after-leak, the scope
# is the real boundary). [S6 review: bumped from the spec's ~60s for full-lifecycle + multi-input.]
_ARTIFACT_FETCH_DEFAULT_TTL = 600


def mint_artifact_fetch_token(secret: str, user_id: str, *, ttl_sec: int = _ARTIFACT_FETCH_DEFAULT_TTL) -> str:
    """cca P3 F-B: mint a short-lived artifact-fetch-scoped HS256 token (aud + sub + exp).
    `secret` is the dedicated artifact-fetch secret (NOT the main JWT secret)."""
    import time

    import jwt

    now = int(time.time())
    payload = {"aud": _ARTIFACT_FETCH_JWT_AUD, "sub": user_id, "iat": now, "exp": now + int(ttl_sec)}
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token.decode("utf-8") if isinstance(token, bytes) else token


def verify_artifact_fetch_token(secret: str, token: str) -> str | None:
    """cca P3 F-B: verify an artifact-fetch token (signature + aud + exp); return its `sub`
    (the user_id to run the owner-check as), or None on ANY failure (expired / wrong aud /
    wrong secret / malformed). A main-JWT user token fails here (wrong secret + aud)."""
    import jwt

    if not secret or not token:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], audience=_ARTIFACT_FETCH_JWT_AUD)
    except Exception:
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None


def artifact_fetch_document_inspection(secret: str, token: str) -> dict[str, Any] | None:
    """Honor restrictions on unexpired legacy tokens; new tokens omit this claim."""
    import jwt

    payload = jwt.decode(token, secret, algorithms=["HS256"], audience=_ARTIFACT_FETCH_JWT_AUD)
    scope = payload.get("document_inspection")
    if scope is None:
        return None
    if (
        not isinstance(scope, dict)
        or set(scope) != {"team_id", "user_id", "workspace_id", "session_id", "turn_id", "execution_generation", "execution_owner_id"}
        or type(scope["execution_generation"]) is not int
        or scope["execution_generation"] < 1
        or any(not isinstance(value, str) or not value for key, value in scope.items() if key != "execution_generation")
        or scope["user_id"] != payload.get("sub")
    ):
        raise ValueError("document_inspection_scope_invalid")
    return scope


@dataclass
class LocalBackend:
    root_dir: Path
    name: str = "local"
    # Sprint file-upload-presign Phase 1.2: dev presigned-POST receiver config.
    # All three MUST be set (and allow_local_receiver=True) for presign_post to
    # return an envelope; otherwise returns None → caller falls back to small
    # upload path. Prod (S3Backend) never uses these.
    gateway_base_url: str | None = None
    local_presign_secret: str | None = field(default=None, repr=False)
    allow_local_receiver: bool = False

    def __post_init__(self) -> None:
        self.root_dir = Path(self.root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def path_for_artifact(self, aid: str, filename: str) -> str:
        namespace = _STAGED_EXPORT_OBJECT_PREFIX if filename.startswith(_STAGED_EXPORT_BLOB_MARKER) else ""
        return str((self.root_dir / namespace / f"{aid}_{filename}").resolve())

    async def put(
        self,
        *,
        aid: str,
        content: bytes,
        filename: str,
        mime: str,
    ) -> str:
        # Filename is sanitized; aid prefix makes collisions impossible
        if filename.startswith(_STAGED_EXPORT_BLOB_MARKER):
            path = self.root_dir / _STAGED_EXPORT_OBJECT_PREFIX / f"{aid}_{filename}"
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = self.root_dir / f"{aid}_{filename}"
        path.write_bytes(content)
        return str(path.resolve())

    async def read(self, path: str) -> bytes:
        return Path(path).read_bytes()

    async def read_bounded(self, path: str, *, limit: int) -> bytes:
        def read() -> bytes:
            with Path(path).open("rb") as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise ArtifactReadLimitError("artifact_read_limit")
            return data

        return await asyncio.to_thread(read)

    def immutable_upload_path(self, token: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ValueError("invalid_immutable_upload_token")
        return str((self.root_dir / _IMMUTABLE_UPLOAD_PREFIX / token).resolve())

    async def put_immutable_upload(self, path: str, *, content: bytes, mime: str) -> bool:
        target = Path(path)
        if path != self.immutable_upload_path(target.name):
            raise ValueError("invalid_immutable_upload_path")

        def write() -> bool:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Same-filesystem hardlink publishes only complete, flushed bytes.
            # A crash can leave a .tmp-* file, never a partial candidate.
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".tmp-") as temp:
                temp.write(content)
                temp.flush()
                os.fsync(temp.fileno())
                try:
                    target.hardlink_to(temp.name)
                except FileExistsError:
                    return False
                return True

        return await asyncio.to_thread(write)

    async def cleanup_immutable_upload_temps(self, cutoff: datetime) -> None:
        def clean() -> None:
            directory = self.root_dir / _IMMUTABLE_UPLOAD_PREFIX
            if directory.is_dir():
                for path in directory.glob(".tmp-*"):
                    if path.is_file() and path.stat().st_mtime < cutoff.timestamp():
                        path.unlink(missing_ok=True)

        await asyncio.to_thread(clean)

    async def download_url(
        self,
        *,
        path: str,
        filename: str,
        mime: str,
        expires_in: int = 3600,
        disposition: str = "attachment",
    ) -> str | None:
        # Local disk has no presigned URLs; gateway uses FileResponse (also setting inline headers)
        return None

    async def delete(self, path: str) -> None:
        p = Path(path)
        if p.is_file():
            p.unlink()

    async def erase(self, path: str) -> None:
        """Account erasure requires confirmed absence, not just a delete call."""
        if Path(path).is_symlink():
            raise OSError("Artifact erasure cannot follow a symbolic link")
        await self.delete(path)
        if Path(path).exists():
            raise OSError("Artifact erasure is not confirmed")

    async def list_staged_export_paths_older_than(self, cutoff: datetime) -> list[str]:
        """List only the private staged-export namespace, never normal uploads."""

        def scan() -> list[str]:
            cutoff_ts = cutoff.timestamp()
            staged_root = self.root_dir / _STAGED_EXPORT_OBJECT_PREFIX
            if not staged_root.is_dir():
                return []
            return [
                str(path.resolve())
                for path in staged_root.iterdir()
                if path.is_file() and _STAGED_EXPORT_BLOB_MARKER in path.name and path.stat().st_mtime <= cutoff_ts
            ]

        return await asyncio.to_thread(scan)

    # ── Sprint file-upload-presign ────────────────────────────────
    async def presign_post(
        self,
        *,
        aid: str,
        filename: str,
        mime: str,
        size_bytes: int,
        expires_in: int = 3600,
    ) -> PresignPostResult | None:
        """Phase 1.2: JWT-signed envelope addressed to the gateway local-receiver.

        Returns None unless allow_local_receiver=True AND both gateway_base_url
        and local_presign_secret are configured (so callers cleanly fall back to
        the small-upload path in dev environments not opted in).
        """
        if not self.allow_local_receiver:
            return None
        if not self.gateway_base_url or not self.local_presign_secret:
            return None
        if not _PRESIGN_AID_RE.fullmatch(aid):
            raise ValueError(f"presign_post: bad aid shape: {aid!r}")

        import time

        import jwt

        safe = _safe_filename(filename)
        now = int(time.time())
        payload = {
            "aud": _PRESIGN_JWT_AUD,
            "aid": aid,
            "filename": safe,
            "mime": mime,
            "size_max": int(size_bytes) + 1024,  # 1KB slack matches S3 Conditions
            "iat": now,
            "exp": now + int(expires_in),
        }
        token = jwt.encode(payload, self.local_presign_secret, algorithm="HS256")
        # PyJWT >= 2.0 returns str; old versions returned bytes → coerce.
        if isinstance(token, bytes):
            token = token.decode("utf-8")

        url = f"/api/v1/files/local-receiver/{aid}"
        # Token in form field (also accepted in Authorization header by receiver),
        # NOT in URL query — closes review P0-S-2 leakage.
        # Phase 2 review P1-6: dropped `key` field — receiver derives target
        # from aid + JWT (see files_local_receiver.py:91, 123), and the FE
        # upload helper sets every fields entry as a request header for the
        # LocalBackend path, so `key` would leak filename in headers.
        fields = {
            "Content-Type": mime,
            "Authorization": f"Bearer {token}",
        }
        target_path = (self.root_dir / f"{aid}_{safe}").resolve()
        return PresignPostResult(
            url=url,
            fields=fields,
            path=str(target_path),
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=expires_in),
        )

    async def head(self, path: str) -> ObjectHead | None:
        def stat_file() -> ObjectHead | None:
            p = Path(path)
            if not p.is_file():
                return None
            stat = p.stat()
            identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
            return ObjectHead(size=stat.st_size, etag=None, mime=None, identity=identity)

        # Enterprise local mounts can stall stat(); keep the gateway event loop
        # responsive so callers can classify a timed-out probe as unknown.
        return await asyncio.to_thread(stat_file)

    @contextlib.asynccontextmanager
    async def read_stream(self, path: str) -> AsyncIterator[IO[bytes]]:
        p = Path(path)
        # Sync open() in to_thread to avoid blocking the loop on fs-slow paths;
        # the file handle is sync (seekable, suitable for ZipFile).
        f = await asyncio.to_thread(p.open, "rb")
        try:
            yield f
        finally:
            await asyncio.to_thread(f.close)

    async def iter_range(
        self,
        path: str,
        *,
        start: int,
        end: int,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        f = await asyncio.to_thread(Path(path).open, "rb")
        try:
            await asyncio.to_thread(f.seek, start)
            remaining = max(0, end - start)
            while remaining:
                chunk = await asyncio.to_thread(f.read, min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(f.close)


# ─────────────────────────────────────────────────────────────
# S3 / MinIO implementation
# ─────────────────────────────────────────────────────────────
@dataclass
class S3Backend:
    """Run a synchronous boto3 client through asyncio.to_thread.

    Configuration supplies an S3-compatible endpoint, bucket, access credentials,
    signing region, and an artifact-key prefix. For example, an independently
    configured endpoint can use https://objects.example.com:9000/.
    """

    endpoint_url: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    prefix: str = "artifacts/"
    _client: object = field(default=None, repr=False)  # lazy boto3 client
    name: str = "s3"

    def _get_client(self) -> object:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=Config(signature_version="s3v4"),
            )
        return self._client

    def _key(self, aid: str, filename: str) -> str:
        namespace = _STAGED_EXPORT_OBJECT_PREFIX if filename.startswith(_STAGED_EXPORT_BLOB_MARKER) else ""
        return f"{self.prefix}{namespace}{aid}_{filename}"

    def path_for_artifact(self, aid: str, filename: str) -> str:
        return self._key(aid, filename)

    def immutable_upload_path(self, token: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ValueError("invalid_immutable_upload_token")
        return f"{self.prefix}{_IMMUTABLE_UPLOAD_PREFIX}{token}"

    async def put_immutable_upload(self, path: str, *, content: bytes, mime: str) -> bool:
        from botocore.exceptions import ClientError

        if path != self.immutable_upload_path(path.rsplit("/", 1)[-1]):
            raise ValueError("invalid_immutable_upload_path")
        try:
            await asyncio.to_thread(
                self._get_client().put_object,
                Bucket=self.bucket,
                Key=path,
                Body=content,
                ContentType=mime,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise  # Unsupported conditional writes must NOT become plain PUT.
            return False
        return True

    async def read_bounded(self, path: str, *, limit: int) -> bytes:
        from botocore.exceptions import ClientError

        def read() -> bytes:
            try:
                response = self._get_client().get_object(Bucket=self.bucket, Key=path)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound", "404"}:
                    raise FileNotFoundError("artifact_not_found") from None
                raise
            stream = response["Body"]
            try:
                data = stream.read(limit + 1)
            finally:
                stream.close()
            if len(data) > limit:
                raise ArtifactReadLimitError("artifact_read_limit")
            return data

        return await asyncio.to_thread(read)

    async def put(
        self,
        *,
        aid: str,
        content: bytes,
        filename: str,
        mime: str,
    ) -> str:
        key = self._key(aid, filename)
        client = self._get_client()
        await asyncio.to_thread(
            client.put_object,  # type: ignore[attr-defined]
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=mime,
        )
        return key

    async def read(self, path: str) -> bytes:
        client = self._get_client()
        resp = await asyncio.to_thread(
            client.get_object,
            Bucket=self.bucket,
            Key=path,  # type: ignore[attr-defined]
        )
        # boto3 returns Body as StreamingBody; read() is synchronous
        return await asyncio.to_thread(resp["Body"].read)  # type: ignore[index]

    async def download_url(
        self,
        *,
        path: str,
        filename: str,
        mime: str,
        expires_in: int = 3600,
        disposition: str = "attachment",
    ) -> str | None:
        client = self._get_client()
        if disposition == "inline":
            # multimodal-v1 inline display: bare inline, with no filename segment
            # (review SEC-P0-3: prevent filename → header injection; inline needs no filename)
            disp = "inline"
        else:
            # Browser downloads with the original filename + correct mime
            # RFC 6266 filename*=UTF-8'' encodes Chinese filenames
            from urllib.parse import quote

            disp = f"attachment; filename*=UTF-8''{quote(filename)}"
        params = {
            "Bucket": self.bucket,
            "Key": path,
            "ResponseContentType": mime,
            "ResponseContentDisposition": disp,
        }
        url = await asyncio.to_thread(
            client.generate_presigned_url,  # type: ignore[attr-defined]
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )
        return str(url)

    async def delete(self, path: str) -> None:
        """Delete the object at ``path``. Treats 404 as success (idempotent
        per S3 spec on AWS, but some S3-compat backends raise ClientError;
        we swallow those uniformly).

        Sprint file-upload-presign Phase 1.5 review P0-2: structured 404
        detection via Error.Code + HTTPStatusCode so the sweeper can rely
        on `delete()` either succeeding or raising a REAL failure.
        """
        client = self._get_client()
        try:
            await asyncio.to_thread(
                client.delete_object,
                Bucket=self.bucket,
                Key=path,  # type: ignore[attr-defined]
            )
        except Exception as e:
            from botocore.exceptions import ClientError

            if isinstance(e, ClientError):
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return
                http_status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if http_status == 404:
                    return
            raise

    async def erase(self, path: str) -> None:
        """Remove every exact-key version, including delete markers."""
        client = self._get_client()

        def erase_versions() -> None:
            paginator = client.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=path):
                versions = [entry for field in ("Versions", "DeleteMarkers") for entry in page.get(field, []) if entry["Key"] == path]
                for entry in versions:
                    client.delete_object(Bucket=self.bucket, Key=path, VersionId=entry["VersionId"])
            # An unversioned object is also covered, and a missing key is safe.
            client.delete_object(Bucket=self.bucket, Key=path)
            # Do not leave the marker created by that final versioned delete.
            for page in paginator.paginate(Bucket=self.bucket, Prefix=path):
                for entry in page.get("DeleteMarkers", []):
                    if entry["Key"] == path:
                        client.delete_object(Bucket=self.bucket, Key=path, VersionId=entry["VersionId"])
                if any(entry["Key"] == path for entry in page.get("Versions", [])):
                    raise OSError("Artifact object versions remain after erasure")

        await asyncio.to_thread(erase_versions)

    async def list_staged_export_paths_older_than(self, cutoff: datetime) -> list[str]:
        """List old objects in the staged-export filename namespace."""

        client = self._get_client()
        token: str | None = None
        paths: list[str] = []
        while True:
            kwargs: dict[str, object] = {
                "Bucket": self.bucket,
                "Prefix": f"{self.prefix}{_STAGED_EXPORT_OBJECT_PREFIX}",
            }
            if token is not None:
                kwargs["ContinuationToken"] = token
            response = await asyncio.to_thread(client.list_objects_v2, **kwargs)  # type: ignore[attr-defined]
            for item in response.get("Contents", []):
                key = item.get("Key")
                modified = item.get("LastModified")
                if isinstance(key, str) and _STAGED_EXPORT_BLOB_MARKER in key and isinstance(modified, datetime) and modified <= cutoff:
                    paths.append(key)
            if not response.get("IsTruncated"):
                break
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token:
                break
            token = next_token
        return paths

    # ── Sprint file-upload-presign ────────────────────────────────
    # Path-style addressing only — virtual-hosted breaks TLS on prod backend
    # (verified Phase 0.5 spike 2026-05-27). _get_client config above uses
    # Config(signature_version="s3v4") with no addressing_style override.
    async def presign_post(
        self,
        *,
        aid: str,
        filename: str,
        mime: str,
        size_bytes: int,
        expires_in: int = 3600,
    ) -> PresignPostResult | None:
        safe = _safe_filename(filename)
        key = f"{self.prefix}{aid}_{safe}"
        client = self._get_client()
        # Conditions are SIGNATURE-bound: Content-Type + size range. Lets us
        # reject MIME swap at PUT time + cap upload size below presign-claimed.
        # 1 KB slack for FormData boundary overhead beyond raw body length.
        conditions: list = [
            {"Content-Type": mime},
            ["content-length-range", 0, size_bytes + 1024],
        ]
        fields = {"Content-Type": mime}
        presigned = await asyncio.to_thread(
            client.generate_presigned_post,  # type: ignore[attr-defined]
            Bucket=self.bucket,
            Key=key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=expires_in,
        )
        return PresignPostResult(
            url=str(presigned["url"]),
            fields=dict(presigned["fields"]),
            path=key,
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=expires_in),
        )

    async def head(self, path: str) -> ObjectHead | None:
        client = self._get_client()
        try:
            resp = await asyncio.to_thread(
                client.head_object,
                Bucket=self.bucket,
                Key=path,  # type: ignore[attr-defined]
            )
        except Exception as e:
            from botocore.exceptions import ClientError

            if isinstance(e, ClientError):
                # HEAD has no response body so botocore can't fill Error.Code from
                # XML; different S3-compat backends populate it differently. Check
                # both the symbolic code AND the raw HTTP status. Verified shape
                # against prod backend in Phase 0.5 spike, but UT must cover both.
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return None
                http_status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if http_status == 404:
                    return None
            raise
        return ObjectHead(
            size=int(resp["ContentLength"]),
            etag=resp.get("ETag"),
            mime=resp.get("ContentType"),
            identity=f"{resp.get('VersionId', '')}:{resp.get('ETag', '')}:{resp.get('LastModified', '')}:{resp['ContentLength']}",
        )

    @contextlib.asynccontextmanager
    async def read_stream(self, path: str) -> AsyncIterator[IO[bytes]]:
        client = self._get_client()
        tmp = tempfile.NamedTemporaryFile(suffix=".s3stream", delete=True)  # noqa: SIM115
        try:
            await asyncio.to_thread(
                client.download_fileobj,
                self.bucket,
                path,
                tmp,  # type: ignore[attr-defined]
            )
            tmp.seek(0)
            yield tmp
        finally:
            tmp.close()

    async def iter_range(
        self,
        path: str,
        *,
        start: int,
        end: int,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        if end <= start:
            return
        client = self._get_client()
        response = await asyncio.to_thread(
            client.get_object,
            Bucket=self.bucket,
            Key=path,  # type: ignore[attr-defined]
            Range=f"bytes={start}-{end - 1}",
        )
        body = response["Body"]
        remaining = end - start
        try:
            while remaining:
                chunk = await asyncio.to_thread(
                    body.read,
                    min(chunk_size, remaining),
                )
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(body.close)


# ─────────────────────────────────────────────────────────────
# Store: metadata management + backend delegation
# ─────────────────────────────────────────────────────────────
def build_artifact_backend_from_env(
    db_path: str | os.PathLike[str],
    *,
    gateway_base_url: str | None = None,
    local_presign_secret: str | None = None,
    allow_local_receiver: bool = False,
) -> ArtifactBackend:
    """按 ``BOWHEAD_ARTIFACT_BACKEND`` 装配 blob backend。网关与运维命令共用这一处，避免两份装配规则漂移。

    local 根目录 = 主库所在目录的上一级下的 ``artifacts``；显式配置无效时拒绝启动。
    """
    kind = (os.environ.get("BOWHEAD_ARTIFACT_BACKEND") or "local").strip().lower()
    local_root = Path(db_path).parent.parent / "artifacts"

    def _local() -> LocalBackend:
        return LocalBackend(
            root_dir=local_root,
            gateway_base_url=gateway_base_url,
            local_presign_secret=local_presign_secret,
            allow_local_receiver=allow_local_receiver,
        )

    if kind == "local":
        return _local()
    if kind != "s3":
        raise ValueError("BOWHEAD_ARTIFACT_BACKEND must be local or s3")
    endpoint = os.environ.get("BOWHEAD_S3_ENDPOINT")
    bucket = os.environ.get("BOWHEAD_S3_BUCKET")
    ak = os.environ.get("BOWHEAD_S3_ACCESS_KEY")
    sk = os.environ.get("BOWHEAD_S3_SECRET_KEY")
    if not (endpoint and bucket and ak and sk):
        raise ValueError("BOWHEAD_ARTIFACT_BACKEND=s3 requires endpoint, bucket, access key and secret key")
    return S3Backend(
        endpoint_url=endpoint,
        bucket=bucket,
        access_key=ak,
        secret_key=sk,
        region=os.environ.get("BOWHEAD_S3_REGION") or "us-east-1",
        prefix=os.environ.get("BOWHEAD_S3_PREFIX") or "artifacts/",
    )


@dataclass
class ArtifactStore:
    db_path: str
    backend: ArtifactBackend
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # M1: cached presence of the asset-model columns (feature-detect once, then
    # put_bytes picks the extended vs legacy INSERT — defense-in-depth behind
    # the ALTER-RAISE so a degraded box still writes legacy rows, not 500s).
    _asset_cols_present: bool | None = field(default=None, repr=False)
    # Batch 2a: separate feature detection for origin_query. Do not include it in _asset_cols_present:
    # another path at :1187 uses that flag, so adding a column would tighten legacy deployments' entire criterion.
    _origin_col_present: bool | None = field(default=None, repr=False)
    #: Independent detection for the same reason: exclude from `_asset_cols_present` to avoid tightening legacy deployments' entire criterion.
    _row_count_col_present: bool | None = field(default=None, repr=False)
    #: Space isolation batch 3: independent detection for the same reason.
    _workspace_col_present: bool | None = field(default=None, repr=False)
    _account_deletion_schema_present: bool | None = field(default=None, repr=False)
    # M1: loud flag set when the one-shot backfill fails to refine legacy rows
    # (DEFAULTs keep them valid; this is observable via /health).
    backfill_degraded: bool = field(default=False, repr=False)

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with _open_db(self.db_path) as db:
            await db.executescript(SCHEMA)
            # Sprint ι: sibling table for sandbox stdout. Idempotent.
            await db.executescript(_STDOUT_SCHEMA)
            # Space lifecycle: persist the target of pending presigned uploads.
            await db.executescript(_UPLOAD_BINDING_SCHEMA)
            # Multimodal V2A: artifact-side provenance for flattened design PNGs.
            await db.executescript(_DESIGN_FLATTEN_EXPORT_SCHEMA)
            # Incremental migration: ALTER adds columns absent from legacy artifacts tables.
            cur = await db.execute("PRAGMA table_info(artifacts)")
            cols = {row[1] for row in await cur.fetchall()}
            for col, decl in (
                ("deleted_at", "TEXT"),
                ("last_used_at", "TEXT"),
                ("category", "TEXT"),
                ("category_source", "TEXT"),
                # Sprint ι (PRD §4.B.1): NULL by default; populated when
                # source ∈ {python_sandbox, sandbox_output}.
                ("code_lang", "TEXT"),
                # Sprint file-upload-presign Phase 1.3 (tech-design §6.5.1):
                # status='ready' default so existing rows are unaffected.
                # 'pending_upload' covers the gap between presign and confirm;
                # sweeper expires stale ones.
                ("status", "TEXT NOT NULL DEFAULT 'ready'"),
                # ISO8601 TEXT (sqlite convention); NULL except for pending rows.
                ("expires_at", "TEXT"),
                # Sprint pptx-from-template M1 (asset model, RD-6). These columns
                # are FOUNDATIONAL: a missing `inject`/`kind` silently flips every
                # upload to the `list` default = re-introduces the lost-context
                # bug. So this ALTER loop RAISES on failure (no try/except) — NOT
                # a degradable condition (unlike the backfill below).
                ("kind", "TEXT NOT NULL DEFAULT 'tool_data'"),
                ("scope", "TEXT NOT NULL DEFAULT 'session'"),
                ("inject", "TEXT NOT NULL DEFAULT 'list'"),
                ("trust", "TEXT NOT NULL DEFAULT 'trusted'"),
                ("producer_kind", "TEXT"),
                ("created_by_turn", "TEXT"),
                ("derived_from", "TEXT"),
                ("manifest_artifact_id", "TEXT"),
                # file-management-v2 D7 (PRD §4.4.1-B3): long_term | one_off。
                # Determines whether to automatically index uploads in the knowledge base. The criterion is semantic
                # (will this material be consulted as evidence later?), not mime: an xlsx
                # quotation is long_term; an xlsx of this month's sales details is one_off.
                # NULL = unclassified / classification failed → fail-safe treats as one_off; no indexing.
                ("retention_hint", "TEXT"),
                # artifact-addressing batch 2a: original query that produced this dataset.
                # Maintained through two paths (this ALTER + migration 0021_artifact_origin_query),
                # like plan_steps' 0011 precedent; both must be idempotent.
                ("origin_query", "TEXT"),
                # Maintained through two paths (this ALTER + migration 0051_artifact_row_count).
                ("row_count", "INTEGER"),
                # Space isolation batch 3: artifact Space ownership. Nullable; NULL means ownership cannot be inferred,
                # neither belonging to Space '' nor readable across Spaces. Maintained through two paths
                # (this ALTER + migration 0096_artifact_workspace_id), both idempotent.
                ("workspace_id", "TEXT"),
            ):
                if col not in cols:
                    await db.execute(f"ALTER TABLE artifacts ADD COLUMN {col} {decl}")
            # Create indexes referencing new columns only once all columns exist
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_user ON artifacts(created_by, deleted_at, created_at DESC)",
            )
            # Sprint file-upload-presign Phase 1.3: sweeper partial index.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_pending ON artifacts(status, expires_at) WHERE status='pending_upload'",
            )
            # M1: kind-keyed listing index (after the ALTERs that add `kind`).
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind, deleted_at, created_at DESC)",
            )
            # M3 (RD-7): covering index for turn_has_deliverable's existence check
            # (created_by_turn = ? AND kind IN (...) AND deleted_at IS NULL). Built
            # now (before M3w.2 populates created_by_turn) so the keep-signal query
            # is indexed the moment it activates, not a full table scan.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_created_by_turn ON artifacts(created_by_turn, kind, deleted_at)",
            )
            # Space isolation batch 3: this index supports Space filtering / NULL measurement (batch 4 read decisions use it too).
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_workspace ON artifacts(workspace_id, deleted_at, created_at DESC)",
            )
            # Columns are guaranteed present now → cache it for put_bytes.
            self._asset_cols_present = True
            await db.commit()

            # M1 (RD-6): one-shot backfill of kind/inject/trust/producer_kind for
            # legacy rows, gated on PRAGMA user_version so it runs EXACTLY ONCE
            # (not a full-table scan every boot). The NOT NULL DEFAULTs already
            # make every legacy row valid (tool_data/list); the backfill only
            # REFINES mis-typed rows — so it is best-effort and DEGRADES on
            # failure (opposite posture to the ALTERs above): a failure logs +
            # sets backfill_degraded (observable via /health) and leaves
            # user_version unbumped so the idempotent UPDATEs retry next boot.
            await self._maybe_backfill_kinds(db)
            # file-management-v2 P1 (PRD §4.2.4): independent second backfill gated by
            # user_version 2. Decoupled from the preceding backfill, with its own failure/retry semantics;
            # touches only one precisely specified filename+source pair.
            await self._maybe_backfill_sections_json_kind(db)
        await self.cleanup_staged_export_orphans()

    async def cleanup_staged_export_orphans(self) -> int:
        """Delete crash-left staged blobs after a grace window.

        Published artifacts retain the same private backend filename but are
        protected by their committed ``artifacts.path`` row.  The one-hour
        window prevents another process's in-flight stage from being collected.
        Backends without the narrow listing capability degrade safely to no-op.
        """

        list_paths = getattr(self.backend, "list_staged_export_paths_older_than", None)
        if not callable(list_paths):
            return 0
        cutoff = datetime.now(tz=UTC) - _STAGED_EXPORT_ORPHAN_GRACE
        try:
            candidates = await list_paths(cutoff)
            async with _open_db(self.db_path) as db, db.execute("SELECT path FROM artifacts") as cursor:
                live_paths = {str(row[0]) for row in await cursor.fetchall()}
            orphans = [path for path in candidates if path not in live_paths]
            results = await asyncio.gather(
                *(self.backend.delete(path) for path in orphans),
                return_exceptions=True,
            )
        except Exception:
            logger.warning("artifact_store.staged_export_cleanup_failed", exc_info=True)
            return 0
        removed = 0
        for _path, result in zip(orphans, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "artifact_store.staged_export_orphan_delete_failed path_kind=%s err=%s",
                    self.backend.name,
                    type(result).__name__,
                )
            else:
                removed += 1
        if removed:
            logger.info("artifact_store.staged_export_orphans_removed count=%d", removed)
        return removed

    async def _maybe_backfill_sections_json_kind(self, db: aiosqlite.Connection) -> None:
        """把历史 `sections.json`（deck 渲染中间输入）的 kind 从 canvas_export 修为 tool_data。

        它是 deck_render_inputs 写的**中间产物**，却因 source='canvas_export' 继承了
        canvas_export kind → 会混进 `/files` 资产 tab 显示成用户的「导出」文件。
        生产侧已改为显式 kind（deck_render_inputs.py），这里修历史行。

        WHERE 同时锁 source + filename，不会误伤真正的导出成品（它们文件名是
        用户可见的 pptx/xlsx/pdf）。幂等：再跑一次匹配 0 行。
        """
        cur = await db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        current = int(row[0]) if row else 0
        if current >= _ARTIFACT_SECTIONS_JSON_VERSION:
            return
        try:
            res = await db.execute(
                "UPDATE artifacts SET kind='tool_data' WHERE source='canvas_export' AND filename='sections.json' AND kind='canvas_export'",
            )
            await db.execute(f"PRAGMA user_version = {_ARTIFACT_SECTIONS_JSON_VERSION}")
            await db.commit()
            if res.rowcount:
                logger.info("artifact_store.backfill_sections_json_kind rows=%d", res.rowcount)
        except Exception as e:
            self.backfill_degraded = True
            logger.error(
                "artifact_store.backfill_sections_json_kind FAILED (degraded; retry next boot) err=%s",
                e,
            )

    async def _maybe_backfill_kinds(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        current = int(row[0]) if row else 0
        if current >= _ARTIFACT_USER_VERSION:
            return
        try:
            # kind/inject/trust per the source→kind map — ONLY for genuine legacy
            # rows. `producer_kind IS NULL` is the precise pre-migration
            # discriminator: every NEW put_bytes sets producer_kind, so this
            # never clobbers an explicit-kind row (e.g. a deliverable written
            # with source='python_sandbox' but kind='deliverable'). MUST run
            # BEFORE the producer_kind fill below, else the guard matches nothing.
            for src, kind in _SOURCE_TO_KIND.items():
                inj, tr = _defaults_for_kind(kind)
                await db.execute(
                    "UPDATE artifacts SET kind=?, inject=?, trust=? WHERE source=? AND producer_kind IS NULL",
                    (kind, inj, tr, src),
                )
            # producer_kind := old source for every legacy row (fills last, so it
            # also re-arms idempotency: a re-run finds producer_kind set and the
            # guarded UPDATE above matches nothing → no clobber).
            await db.execute(
                "UPDATE artifacts SET producer_kind = source WHERE producer_kind IS NULL",
            )
            await db.execute(f"PRAGMA user_version = {_ARTIFACT_USER_VERSION}")
            await db.commit()
            self.backfill_degraded = False
        except Exception as e:
            self.backfill_degraded = True
            logger.error(
                "artifact_store.backfill_kinds FAILED (degraded; legacy rows keep their NOT NULL DEFAULTs, retry next boot) err=%s",
                e,
            )

    async def stage_export_bytes(
        self,
        *,
        content: bytes,
        filename: str,
        mime: str,
        canvas_id: str | None,
        created_by: str,
        session_id: str | None,
        producer_kind: str,
        manifest_artifact_id: str | None = None,
        source: str = "canvas_export",
        created_by_turn: str | None = None,
    ) -> StagedArtifact:
        """Write an export blob without publishing its metadata.

        Canvas Export V2 uses this before its single output/manifest/job
        metadata transaction. The returned object is not readable through
        ``ArtifactStore.get`` until ``insert_staged_artifacts_in_transaction``
        commits.
        """
        if source not in {"canvas_export", "consultant_deck"}:
            raise ValueError("Unsupported staged presentation/export source")
        aid = f"art_{uuid4().hex[:12]}"
        safe = _safe_filename(filename)
        path = await self.put_account_owned_bytes(
            created_by=created_by,
            aid=aid,
            content=content,
            filename=f"{_STAGED_EXPORT_BLOB_MARKER}{safe}",
            mime=mime,
        )
        return StagedArtifact(
            artifact=Artifact(
                id=aid,
                filename=filename,
                mime=mime,
                size=len(content),
                source=source,
                path=path,
                canvas_id=canvas_id,
                created_by=created_by,
                created_at=_now(),
                kind="deliverable",
                scope="session",
                inject="stub",
                trust="trusted",
                producer_kind=producer_kind,
                manifest_artifact_id=manifest_artifact_id,
                created_by_turn=created_by_turn,
            ),
            session_id=session_id,
        )

    async def stage_export_file(
        self,
        *,
        src_path: Path,
        filename: str,
        mime: str,
        canvas_id: str,
        created_by: str,
        session_id: str | None,
        producer_kind: str,
        manifest_artifact_id: str | None = None,
    ) -> StagedArtifact:
        return await self.stage_export_bytes(
            content=src_path.read_bytes(),
            filename=filename,
            mime=mime,
            canvas_id=canvas_id,
            created_by=created_by,
            session_id=session_id,
            producer_kind=producer_kind,
            manifest_artifact_id=manifest_artifact_id,
        )

    async def insert_staged_artifacts_in_transaction(
        self,
        db: aiosqlite.Connection,
        staged_artifacts: tuple[StagedArtifact, ...],
    ) -> None:
        """Publish staged metadata on the caller-owned SQLite transaction."""
        if not staged_artifacts:
            raise ValueError("at least one staged artifact is required")
        if self._asset_cols_present is None:
            cursor = await db.execute("PRAGMA table_info(artifacts)")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            self._asset_cols_present = "kind" in columns and "inject" in columns and "manifest_artifact_id" in columns
        if not self._asset_cols_present:
            raise RuntimeError("artifact asset-model schema is unavailable")
        artifact_ids = [staged.artifact.id for staged in staged_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("duplicate staged artifact id")
        for staged in staged_artifacts:
            artifact = staged.artifact
            if artifact.source not in {"canvas_export", "consultant_deck"} or artifact.kind != "deliverable" or artifact.deleted_at is not None:
                raise ValueError("invalid staged export artifact")
            await db.execute(
                "INSERT INTO artifacts("
                "id, filename, mime, size, source, path, canvas_id, "
                "created_by, created_at, kind, scope, inject, trust, "
                "producer_kind, created_by_turn, derived_from, "
                "manifest_artifact_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact.id,
                    artifact.filename,
                    artifact.mime,
                    artifact.size,
                    artifact.source,
                    artifact.path,
                    artifact.canvas_id,
                    artifact.created_by,
                    artifact.created_at,
                    artifact.kind,
                    artifact.scope,
                    artifact.inject,
                    artifact.trust,
                    artifact.producer_kind,
                    artifact.created_by_turn,
                    artifact.derived_from,
                    artifact.manifest_artifact_id,
                ),
            )
            if self._account_deletion_schema_present:
                await db.execute("DELETE FROM account_owned_artifact_writes WHERE write_id=?", (artifact.id,))
            # Space isolation batch 3: ownership of exported deliverables. Same guarded UPDATE as put_bytes,
            # preserving this path's degradation: missing columns on legacy deployments omit ownership, never fail the whole export.
            workspace_id = await _resolve_write_workspace_id(
                db,
                session_id=staged.session_id,
                created_by_turn=artifact.created_by_turn,
                canvas_id=artifact.canvas_id,
            )
            if workspace_id:
                if self._workspace_col_present is None:
                    _wcur = await db.execute("PRAGMA table_info(artifacts)")
                    self._workspace_col_present = "workspace_id" in {str(r[1]) for r in await _wcur.fetchall()}
                if self._workspace_col_present:
                    artifact.workspace_id = workspace_id
                    await db.execute(
                        "UPDATE artifacts SET workspace_id=? WHERE id=?",
                        (workspace_id, artifact.id),
                    )
            if staged.session_id:
                await db.execute(
                    "INSERT INTO session_attachments("
                    "session_id, artifact_id, pinned_at, detached_at"
                    ") VALUES (?, ?, ?, NULL) "
                    "ON CONFLICT(session_id, artifact_id) "
                    "DO UPDATE SET detached_at=NULL",
                    (
                        staged.session_id,
                        artifact.id,
                        _now(),
                    ),
                )

    async def commit_staged_artifacts(
        self,
        staged_artifacts: tuple[StagedArtifact, ...],
    ) -> None:
        """Publish staged metadata atomically when no job row shares the transaction."""
        async with _open_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self.insert_staged_artifacts_in_transaction(db, staged_artifacts)
                commit_task = asyncio.create_task(db.commit())
                try:
                    await asyncio.shield(commit_task)
                except asyncio.CancelledError:
                    await commit_task
            except BaseException:
                await db.rollback()
                raise

    async def discard_staged_artifacts(
        self,
        staged_artifacts: tuple[StagedArtifact, ...],
    ) -> None:
        """Best-effort deletion for blobs whose metadata was not committed."""
        results = await asyncio.gather(
            *(self.backend.delete(staged.artifact.path) for staged in staged_artifacts),
            return_exceptions=True,
        )
        for staged, result in zip(
            staged_artifacts,
            results,
            strict=True,
        ):
            if isinstance(result, BaseException):
                logger.warning(
                    "artifact_store.discard_staged_failed art=%s err=%s",
                    staged.artifact.id,
                    type(result).__name__,
                )

    async def put_bytes(
        self,
        *,
        content: bytes,
        filename: str,
        mime: str,
        created_by: str | None,
        source: str = "upload",
        canvas_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        inject: str | None = None,
        trust: str | None = None,
        producer_kind: str | None = None,
        created_by_turn: str | None = None,
        derived_from: str | None = None,
        manifest_artifact_id: str | None = None,
        origin_query: str | None = None,
        row_count: int | None = None,
        workspace_id: str | None = None,
        bind_chat_upload: bool = False,
        authorize_in_connection: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
    ) -> Artifact:
        """Persist artifact + (Sprint µ N4 fix) optionally pin to session.

        ``session_id`` non-empty → also INSERT into session_attachments so the
        artifact surfaces in /api/v1/sessions/{id}/artifacts (Assets tab). Per
        discovery.md design issue #N4: bi_fetch_rows / sandbox produce csv
        artifacts were created but never linked to session, so the tab was
        empty for entire session of t_059e6921.

        Sprint pptx-from-template M1 (asset model): ``kind`` defaults from
        ``source`` via the backfill map; ``inject``/``trust`` default from the
        kind; ``producer_kind`` defaults to ``source``. ``derived_from`` is
        validated (no self-ref, parent must be live) — an invalid value is
        dropped + logged rather than failing the write.

        ``created_by`` is required (no default) so every writer decides who owns the
        artifact: the run's accountable user, or ``None`` only when there is none. An
        owner-less artifact stays readable in its Space but nobody may mutate it.
        """
        if bind_chat_upload and (source != "upload" or not created_by):
            raise ValueError("Chat uploads require an upload source and an accountable user")
        aid = f"art_{uuid4().hex[:12]}"
        # Resolve asset-model fields from the kind defaults (RD-6).
        _kind = kind or _kind_for_source(source)
        _inj_def, _trust_def = _defaults_for_kind(_kind)
        _inject = inject or _inj_def
        _trust = trust or _trust_def
        _scope = scope or "session"
        _producer = producer_kind or source
        safe = _safe_filename(filename)
        owned_write = False
        if created_by:
            async with _open_db(self.db_path) as ownership_db:
                await self._retired_owner_filter(ownership_db)
                if self._account_deletion_schema_present:
                    path_builder = getattr(self.backend, "path_for_artifact", None)
                    if path_builder is not None:
                        await ownership_db.execute(
                            "INSERT INTO account_owned_artifact_writes(write_id,user_id,path,created_at) VALUES (?,?,?,?)",
                            (aid, created_by, path_builder(aid, safe), _now()),
                        )
                        await ownership_db.commit()
                        owned_write = True
        path = (
            ""
            if authorize_in_connection is not None
            else await self.put_account_owned_bytes(
                created_by=created_by,
                reserved=owned_write,
                aid=aid,
                content=content,
                filename=safe,
                mime=mime,
            )
        )
        art = Artifact(
            id=aid,
            filename=filename,
            mime=mime,
            size=len(content),
            source=source,
            path=path,
            canvas_id=canvas_id,
            created_by=created_by,
            created_at=_now(),
            kind=_kind,
            scope=_scope,
            inject=_inject,
            trust=_trust,
            producer_kind=_producer,
            created_by_turn=created_by_turn,
            derived_from=derived_from,
            manifest_artifact_id=manifest_artifact_id,
            origin_query=(origin_query or "").strip() or None,
            row_count=row_count if isinstance(row_count, int) and row_count >= 0 else None,
        )
        async with _open_db(self.db_path) as db:
            if authorize_in_connection is not None:
                await db.execute("BEGIN IMMEDIATE")
                await authorize_in_connection(db)
                art.path = await self.put_account_owned_bytes(
                    created_by=created_by, reserved=owned_write, aid=aid, content=content, filename=safe, mime=mime
                )
            # Feature-detect the asset-model columns once (cached). ALTER-RAISE in
            # initialize() means these are present in practice; the legacy-INSERT
            # fallback only matters for a degraded box that never migrated.
            if self._asset_cols_present is None:
                cur = await db.execute("PRAGMA table_info(artifacts)")
                _cols = {r[1] for r in await cur.fetchall()}
                self._asset_cols_present = "kind" in _cols and "inject" in _cols
            # derived_from validation: drop self-ref / dangling / soft-deleted parent.
            if art.derived_from:
                if art.derived_from == aid:
                    art.derived_from = None
                else:
                    pcur = await db.execute(
                        "SELECT 1 FROM artifacts WHERE id=? AND deleted_at IS NULL",
                        (art.derived_from,),
                    )
                    if await pcur.fetchone() is None:
                        logger.warning(
                            "artifact_store.put_bytes: dropping invalid derived_from=%s (missing/soft-deleted) for art=%s",
                            art.derived_from,
                            aid,
                        )
                        art.derived_from = None
            if self._asset_cols_present:
                await db.execute(
                    "INSERT INTO artifacts(id, filename, mime, size, source, path, canvas_id, created_by, created_at, "
                    "kind, scope, inject, trust, producer_kind, created_by_turn, derived_from, manifest_artifact_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        art.id,
                        art.filename,
                        art.mime,
                        art.size,
                        art.source,
                        art.path,
                        art.canvas_id,
                        art.created_by,
                        art.created_at,
                        art.kind,
                        art.scope,
                        art.inject,
                        art.trust,
                        art.producer_kind,
                        art.created_by_turn,
                        art.derived_from,
                        art.manifest_artifact_id,
                    ),
                )
            else:
                # Degraded box (columns absent): legacy 9-col INSERT, don't 500.
                await db.execute(
                    "INSERT INTO artifacts(id, filename, mime, size, source, path, canvas_id, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (art.id, art.filename, art.mime, art.size, art.source, art.path, art.canvas_id, art.created_by, art.created_at),
                )
            # Batch 2a: origin_query uses an independent guarded UPDATE, outside the two INSERTs above.
            # These are hot paths with existing legacy/extended branches; another variant would mean
            # three column lists to synchronize. Add a statement only when origin is supplied; silently skip absent columns on legacy deployments
            # (lost provenance must not fail the entire data delivery; same degradation direction as derived_from).
            if art.origin_query:
                if self._origin_col_present is None:
                    _ocur = await db.execute("PRAGMA table_info(artifacts)")
                    self._origin_col_present = "origin_query" in {r[1] for r in await _ocur.fetchall()}
                if self._origin_col_present:
                    await db.execute(
                        "UPDATE artifacts SET origin_query=? WHERE id=?",
                        (art.origin_query, art.id),
                    )
                else:
                    logger.warning(
                        "artifact_store.put_bytes: origin_query column absent, provenance dropped for art=%s",
                        aid,
                    )
            # row_count uses the same guarded UPDATE (identical rationale to origin_query: the hot-path INSERTs
            # already have legacy/extended branches; another variant adds a third column list to synchronize).
            if art.row_count is not None:
                if self._row_count_col_present is None:
                    _rcur = await db.execute("PRAGMA table_info(artifacts)")
                    self._row_count_col_present = "row_count" in {r[1] for r in await _rcur.fetchall()}
                if self._row_count_col_present:
                    await db.execute(
                        "UPDATE artifacts SET row_count=? WHERE id=?",
                        (art.row_count, art.id),
                    )
                else:
                    logger.warning(
                        "artifact_store.put_bytes: row_count column absent, dropped for art=%s",
                        aid,
                    )
            # Space isolation batch 3: ownership uses the same guarded UPDATE (identical rationale to origin_query / row_count:
            # the two INSERTs above already have legacy/extended branches; another variant adds a third
            # column list to synchronize). Leave NULL when inference fails; do not guess. Batch 4 measures NULL,
            # and any fallback value would erase that measurement.
            art.workspace_id = (workspace_id or "").strip() or await _resolve_write_workspace_id(
                db,
                session_id=session_id,
                created_by_turn=art.created_by_turn,
                canvas_id=art.canvas_id,
            )
            if art.workspace_id:
                if self._workspace_col_present is None:
                    _wcur = await db.execute("PRAGMA table_info(artifacts)")
                    self._workspace_col_present = "workspace_id" in {r[1] for r in await _wcur.fetchall()}
                if self._workspace_col_present:
                    await db.execute(
                        "UPDATE artifacts SET workspace_id=? WHERE id=?",
                        (art.workspace_id, art.id),
                    )
                else:
                    logger.warning(
                        "artifact_store.put_bytes: workspace_id column absent, ownership dropped for art=%s",
                        aid,
                    )
            if bind_chat_upload:
                await db.execute(
                    "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (art.id, art.workspace_id or "", created_by, session_id, art.created_at),
                )
            # Sprint µ N4: pin to session if provided
            if session_id:
                try:
                    await db.execute(
                        "INSERT INTO session_attachments(session_id, artifact_id, pinned_at, detached_at) "
                        "VALUES (?, ?, ?, NULL) "
                        "ON CONFLICT(session_id, artifact_id) DO UPDATE SET detached_at=NULL",
                        (session_id, aid, _now()),
                    )
                except Exception as _e:
                    logger.warning(
                        "artifact_store.put_bytes: session_attachments insert failed session=%s art=%s err=%s",
                        session_id,
                        aid,
                        _e,
                    )
            if owned_write:
                await db.execute("DELETE FROM account_owned_artifact_writes WHERE write_id=?", (aid,))
            await db.commit()
        return art

    async def put_file(
        self,
        *,
        src_path: Path,
        created_by: str | None,
        filename: str | None = None,
        mime: str = "application/octet-stream",
        source: str = "canvas_export",
        canvas_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        created_by_turn: str | None = None,
        derived_from: str | None = None,
        manifest_artifact_id: str | None = None,
    ) -> Artifact:
        content = src_path.read_bytes()
        return await self.put_bytes(
            content=content,
            filename=filename or src_path.name,
            mime=mime,
            source=source,
            canvas_id=canvas_id,
            created_by=created_by,
            session_id=session_id,
            kind=kind,
            created_by_turn=created_by_turn,
            derived_from=derived_from,
            manifest_artifact_id=manifest_artifact_id,
        )

    async def walk_version_chain(
        self,
        artifact_id: str,
        *,
        max_depth: int = 50,
    ) -> list[Artifact]:
        """Walk the `derived_from` parent chain newest→oldest (M1-owned, CR-1).

        Bounded at ``max_depth`` with a revisit-set so a cycle/orphan (which
        write-time validation should already prevent) can never hang a consumer
        (M4 NL-edit targeting, M5 version history). Returns the chain starting
        at ``artifact_id`` (inclusive); stops at the first missing/soft-deleted
        ancestor.
        """
        chain: list[Artifact] = []
        seen: set[str] = set()
        cur_id: str | None = artifact_id
        while cur_id and cur_id not in seen and len(chain) < max_depth:
            seen.add(cur_id)
            art = await self.get(cur_id)
            if art is None:
                break
            chain.append(art)
            cur_id = art.derived_from
        return chain

    async def put_design_flatten_provenance(
        self,
        *,
        artifact_id: str,
        canvas_id: str,
        section_id: str,
        section_rev: int,
        size_preset: str,
        background_artifact_id: str,
        created_at: str | None = None,
    ) -> DesignFlattenProvenance:
        """Persist artifact-side provenance for a flattened design PNG."""
        row = DesignFlattenProvenance(
            artifact_id=artifact_id,
            canvas_id=canvas_id,
            section_id=section_id,
            section_rev=section_rev,
            size_preset=size_preset,
            background_artifact_id=background_artifact_id,
            created_at=created_at or _now(),
        )
        async with _open_db(self.db_path) as db:
            await db.execute(
                "INSERT INTO design_flatten_exports("
                "artifact_id, canvas_id, section_id, section_rev, size_preset, "
                "background_artifact_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(artifact_id) DO UPDATE SET "
                "canvas_id=excluded.canvas_id, "
                "section_id=excluded.section_id, "
                "section_rev=excluded.section_rev, "
                "size_preset=excluded.size_preset, "
                "background_artifact_id=excluded.background_artifact_id, "
                "created_at=excluded.created_at",
                (
                    row.artifact_id,
                    row.canvas_id,
                    row.section_id,
                    row.section_rev,
                    row.size_preset,
                    row.background_artifact_id,
                    row.created_at,
                ),
            )
            await db.commit()
        return row

    async def get_design_flatten_provenance(
        self,
        artifact_id: str,
    ) -> DesignFlattenProvenance | None:
        """Return provenance for a flattened design PNG, if present."""
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM design_flatten_exports WHERE artifact_id=?",
                (artifact_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return DesignFlattenProvenance(
            artifact_id=str(row["artifact_id"]),
            canvas_id=str(row["canvas_id"]),
            section_id=str(row["section_id"]),
            section_rev=int(row["section_rev"]),
            size_preset=str(row["size_preset"]),
            background_artifact_id=str(row["background_artifact_id"]),
            created_at=str(row["created_at"]),
        )

    async def get_manifest_for_deck(self, deck_artifact_id: str) -> Artifact | None:
        """Return the section↔slide manifest asset for a deliverable deck (CR-1).

        M3 writes ``manifest_artifact_id`` on the deck row at render success;
        M4/M5 read it here. Returns None if the deck has no manifest yet.
        """
        deck = await self.get(deck_artifact_id)
        if deck is None or not deck.manifest_artifact_id:
            return None
        return await self.get(deck.manifest_artifact_id)

    async def turn_has_deliverable(self, turn_id: str) -> bool:
        """True iff the turn produced a live ``deliverable`` or ``deck_preview``
        artifact (pptx-from-template M3 RD-7 / CR-6 keep-signal).

        Used by ``is_report_worthy``'s two done-emit gates so a deck-generate or
        content-edit-only turn (which may have no insight_card section) is never
        chat-mode-hidden. ``created_by_turn`` is the M3-threaded owning Lead turn
        (set by ``runner._upload_artifact`` in M3w.2). Existence check only —
        cheap LIMIT 1; per-turn artifact counts are tiny. Returns False on a
        missing column / pre-M1 DB (best-effort; falls back to today's behavior).
        """
        if not turn_id:
            return False
        try:
            async with (
                _open_db(self.db_path) as db,
                db.execute(
                    "SELECT 1 FROM artifacts WHERE created_by_turn = ? AND kind IN ('deliverable', 'deck_preview') AND deleted_at IS NULL LIMIT 1",
                    (turn_id,),
                ) as cur,
            ):
                return await cur.fetchone() is not None
        except Exception as e:
            # pre-M1 DB without kind/created_by_turn columns (OperationalError) or
            # any other DB hiccup → degrade to "no deliverable" rather than break
            # the done-emit hide decision the callers wrap us in.
            logger.debug("turn_has_deliverable degraded turn=%s err=%s", turn_id, e)
            return False

    async def find_turn_artifact_ids_by_filename(self, turn_id: str, filenames: list[str]) -> dict[str, str]:
        """同一 turn 里按文件名找最近一份未删除的 artifact：``{filename: artifact_id}``。

        给沙箱入口用：模型常把**上一次沙箱运行写出的文件名**（`category_metrics.csv`）
        直接写进这一次的代码，而每次运行都是全新工作目录。那份文件其实已经作为
        artifact 落库（`created_by_turn` = 本 turn），按名字能找回来。
        只查本 turn，找不到就不返回——宁可让下游照常报 FileNotFoundError 也不跨 turn 猜。
        """
        names = [n for n in dict.fromkeys(filenames) if n]
        if not turn_id or not names:
            return {}
        marks = ",".join("?" for _ in names)
        try:
            async with (
                _open_db(self.db_path) as db,
                db.execute(
                    f"SELECT id, filename FROM artifacts WHERE created_by_turn = ? AND filename IN ({marks}) "  # noqa: S608 — placeholders only
                    "AND deleted_at IS NULL ORDER BY created_at ASC, rowid ASC",
                    (turn_id, *names),
                ) as cur,
            ):
                rows = await cur.fetchall()
        except Exception as e:
            logger.debug("find_turn_artifact_ids_by_filename degraded turn=%s err=%s", turn_id, e)
            return {}
        # ASC traversal with later overwrites ⇒ newest artifact for duplicate names (version from the last run).
        return {str(name): str(aid) for aid, name in rows}

    async def list_deliverables_for_turn(self, turn_id: str) -> list[Artifact]:
        """Return the live ``deliverable`` / ``deck_preview`` artifacts a turn
        produced, newest-first (subagent-unification P5a).

        Sibling of ``turn_has_deliverable``: that one is the cheap existence
        check the report-worthy keep gate uses; this returns the records so the
        lead-final inline-artifact collector can emit a download chip for a deck
        deliverable. Same WHERE clause (and the same ``idx_artifacts_created_by_turn``
        index) → identical keep-signal semantics. Best-effort: returns ``[]`` on a
        pre-M1 DB (missing column) or any DB hiccup, mirroring the sibling.
        """
        if not turn_id:
            return []
        try:
            async with _open_db(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM artifacts "
                    "WHERE created_by_turn = ? "
                    "AND kind IN ('deliverable', 'deck_preview') "
                    "AND deleted_at IS NULL "
                    "ORDER BY created_at DESC",
                    (turn_id,),
                ) as cur:
                    rows = await cur.fetchall()
            return [_row_to_artifact(r) for r in rows]
        except Exception as e:
            logger.debug(
                "list_deliverables_for_turn degraded turn=%s err=%s",
                turn_id,
                e,
            )
            return []

    async def list_result_artifacts_for_turn(self, turn_id: str) -> list[Artifact]:
        """Return user-visible stable artifacts produced by one turn.

        Unlike ``list_deliverables_for_turn``, this includes sandbox/data
        outputs that Results exposes as files. User uploads and internal export
        dependencies do not count as evidence for a generated-file claim.
        """
        if not turn_id:
            return []
        try:
            async with _open_db(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM artifacts WHERE created_by_turn = ? AND COALESCE(kind, 'tool_data') != 'user_upload' AND deleted_at IS NULL ORDER BY created_at DESC",
                    (turn_id,),
                ) as cur:
                    rows = await cur.fetchall()
            artifacts = [_row_to_artifact(row) for row in rows]

            from bowhead_core.export_copy_contract import CURRENT_OUTPUT_PRODUCER_VALUES
            from bowhead_core.results_artifact_policy import is_results_internal_artifact

            by_id = {artifact.id: artifact for artifact in artifacts}
            manifest_ids: set[str] = set()
            for artifact in artifacts:
                manifest_id = artifact.manifest_artifact_id
                manifest = by_id.get(manifest_id) if manifest_id else None
                if manifest is not None and artifact.producer_kind in CURRENT_OUTPUT_PRODUCER_VALUES and artifact.created_by == manifest.created_by:
                    manifest_ids.add(manifest_id)
            return [
                artifact
                for artifact in artifacts
                if not is_results_internal_artifact(
                    artifact,
                    manifest_artifact_ids=manifest_ids,
                )
            ]
        except Exception as e:
            logger.debug(
                "list_result_artifacts_for_turn degraded turn=%s err=%s",
                turn_id,
                e,
            )
            raise

    async def put_account_owned_bytes(
        self,
        *,
        created_by: str | None,
        aid: str,
        content: bytes,
        filename: str,
        mime: str,
        reserved: bool = False,
    ) -> str:
        """Write a server-named blob, retaining its locator until metadata commits."""
        path_builder = getattr(self.backend, "path_for_artifact", None)
        path = path_builder(aid, filename) if path_builder is not None else None
        return await self._write_account_blob(
            created_by=created_by,
            path=path,
            write_id=aid,
            reserved=reserved,
            write=lambda: self.backend.put(aid=aid, content=content, filename=filename, mime=mime),
        )

    async def put_account_owned_immutable(self, *, user_id: str, path: str, content: bytes, mime: str) -> bool:
        """The domain must commit this exact locator before immutable blob I/O."""
        return await self._write_account_blob(
            created_by=user_id,
            path=path,
            write_id=path,
            reserved=True,
            write=lambda: self.backend.put_immutable_upload(path, content=content, mime=mime),
        )

    async def _write_account_blob(
        self,
        *,
        created_by: str | None,
        path: str | None,
        write_id: str,
        write: Callable[[], Awaitable[Any]],
        reserved: bool,
    ) -> Any:
        if not created_by:
            return await write()
        async with _open_db(self.db_path) as db:
            await self._retired_owner_filter(db)
            if self._account_deletion_schema_present and not reserved and path is not None:
                await db.execute(
                    "INSERT OR IGNORE INTO account_owned_artifact_writes(write_id,user_id,path,created_at) VALUES (?,?,?,?)",
                    (write_id, created_by, path, _now()),
                )
                await db.commit()
        if not self._account_deletion_schema_present:
            return await write()
        from bowhead_core.account_artifact_lock import account_artifact_lock

        async with account_artifact_lock(self.db_path, created_by):
            async with _open_db(self.db_path) as db, db.execute("SELECT 1 FROM account_deletions WHERE user_id=?", (created_by,)) as cur:
                if await cur.fetchone() is not None:
                    raise aiosqlite.IntegrityError("account is deleted")
            # Keep the OS lock until the actual provider task ends, including
            # when its caller is cancelled while a blocking S3 write continues.
            upload = asyncio.create_task(write())
            try:
                return await asyncio.shield(upload)
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    while not upload.done():
                        try:
                            await asyncio.shield(upload)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        upload.result()
                raise

    async def _retired_owner_filter(self, db: aiosqlite.Connection) -> str:
        if self._account_deletion_schema_present is None:
            async with db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='account_deletions'") as cur:
                self._account_deletion_schema_present = await cur.fetchone() is not None
        if not self._account_deletion_schema_present:
            return "1=1"
        return (
            "(scope='team' OR id IN (SELECT artifact_id FROM team_brand_asset_metadata) "
            "OR NOT EXISTS(SELECT 1 FROM account_deletions d WHERE d.user_id=artifacts.created_by))"
        )

    async def get(
        self,
        artifact_id: str,
        *,
        include_deleted: bool = False,
        check_created_by: str | None = None,
        reject_null_owner: bool = False,
    ) -> Artifact | None:
        """按 id 拿 artifact. 默认过滤已删除.

        #54 长期修 (2026-04-29): `check_created_by` 非 None → 校验 art.created_by 必须等.
        canvas_tools.add_chart 等跨 turn 引用 artifact 的入口必传 `ctx.user_id`, 防 LLM 记着
        别 user / 别 session 的 artifact_id 越权读 (踩 'artifact 河北/黑龙江 404' bug).
        失败统一返 None (不返 PermissionError, 防侧信道泄露 artifact 存在性).

        cca P3 F-B: `reject_null_owner` (default False — current behavior preserved) →
        an `created_by=NULL` (owner-less) artifact is ALSO denied when `check_created_by`
        is set. Used ONLY on the python_sandbox runner-fetch path (a token-authenticated
        GET): the sandbox only ever needs the caller's OWN artifacts, so an owner-less
        artifact must not be world-readable to a member-authored sandbox script (the
        cross-user/owner-less exfil vector). Regular per-user downloads keep NULL artifacts
        readable (legit shared/system artifacts), so this stays opt-in.
        """
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            q = "SELECT * FROM artifacts WHERE id=? AND COALESCE(producer_kind,'')<>'template_document_internal_v1'"
            q += " AND " + await self._retired_owner_filter(db)
            if not include_deleted:
                q += " AND deleted_at IS NULL"
            async with db.execute(q, (artifact_id,)) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        art = _row_to_artifact(row)
        if check_created_by is not None:
            if art.created_by is not None and art.created_by != check_created_by:
                # Treat as absent; do not reveal unauthorized existence (security through silent denial)
                return None
            if art.created_by is None and reject_null_owner:
                # cca P3 F-B: owner-less artifact is not fetchable on the scoped runner path
                return None
        return art

    async def get_many(
        self,
        artifact_ids: list[str],
        *,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        """Batch metadata lookup with stable caller order and bounded SQL variables."""
        ordered = list(dict.fromkeys(artifact_id for artifact_id in artifact_ids if artifact_id))
        if not ordered:
            return []
        by_id: dict[str, Artifact] = {}
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            retired_owner_filter = await self._retired_owner_filter(db)
            for start in range(0, len(ordered), 400):
                chunk = ordered[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                query = f"SELECT * FROM artifacts WHERE id IN ({placeholders}) AND COALESCE(producer_kind,'')<>'template_document_internal_v1'"
                query += " AND " + retired_owner_filter
                if not include_deleted:
                    query += " AND deleted_at IS NULL"
                async with db.execute(query, chunk) as cur:
                    for row in await cur.fetchall():
                        artifact = _row_to_artifact(row)
                        by_id[artifact.id] = artifact
        return [by_id[artifact_id] for artifact_id in ordered if artifact_id in by_id]

    async def get_ready_uploads_for_chat(
        self,
        artifact_ids: list[str],
        *,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Artifact]:
        """Resolve completed, caller-owned uploads inside one Space.

        The typed Composer contract uses this as an authorization and metadata
        fence before it persists a canonical user source.  A caller cannot
        smuggle another user's or another Space's artifact ID into a Turn, and
        an upload still in ``pending_upload`` is not sendable content.
        """
        ids = list(dict.fromkeys(artifact_id for artifact_id in artifact_ids if artifact_id))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        query = (
            "SELECT a.* FROM artifacts a "
            "JOIN artifact_upload_bindings b ON b.artifact_id=a.id "
            f"WHERE a.id IN ({placeholders}) "  # noqa: S608 - placeholders only
            "AND a.created_by=? AND a.deleted_at IS NULL AND a.status='ready' "
            "AND COALESCE(a.producer_kind,'')<>'template_document_internal_v1' "
            "AND b.user_id=? AND b.workspace_id=?"
        )
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                query,
                (*ids, user_id, user_id, workspace_id),
            ) as cursor:
                rows = await cursor.fetchall()
        return {str(row["id"]): _row_to_artifact(row) for row in rows}

    async def chat_bundle_contains(
        self,
        *,
        bundle_artifact_id: str,
        child_artifact_ids: list[str],
    ) -> bool:
        """Verify server-recorded bundle membership for every claimed child."""
        child_ids = list(dict.fromkeys(item for item in child_artifact_ids if item))
        if not child_ids:
            return False
        placeholders = ",".join("?" for _ in child_ids)
        async with (
            _open_db(self.db_path) as db,
            db.execute(
                f"SELECT COUNT(*) FROM chat_attachment_bundle_members WHERE bundle_artifact_id=? AND child_artifact_id IN ({placeholders})",  # noqa: S608 - placeholders only
                (bundle_artifact_id, *child_ids),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return bool(row and int(row[0]) == len(child_ids))

    # ─── List / filtering ──────────────────────────────────────
    async def list_for_user(
        self,
        *,
        user_id: str,
        q: str | None = None,
        mime_families: list[str] | None = None,
        sources: list[str] | None = None,
        kinds: list[str] | None = None,
        since: str | None = None,  # ISO date
        until: str | None = None,
        sort: str = "created_desc",  # created_desc | size_desc | last_used_desc
        limit: int = 50,
        offset: int = 0,
        include_deleted: bool = False,
        include_ids: Collection[str] | None = None,
        exclude_ids: Collection[str] | None = None,
        category: str | None = None,
    ) -> tuple[list[Artifact], int, int]:
        """返 (items, total_matching, size_total_bytes). user_id 强制过滤 created_by.

        file-management-v2 P1: `kinds` 是资产库三 tab 的过滤主键（资产 =
        {user_upload, deliverable, canvas_export}；运行数据 = {tool_data,
        sandbox_output, web_clip}）。走 idx_artifacts_kind。与 `sources`
        是 AND 关系，两者都可省略。
        """
        # Branding assets have a dedicated team-admin/public read surface and
        # must never leak into Files, unified search, or agent-oriented lists.
        where: list[str] = ["created_by = ?", "kind != 'branding_asset'", "COALESCE(producer_kind,'')<>'template_document_internal_v1'"]
        # Exclude rows whose original files are lost (reconciled as missing) from file lists and counts
        where.append("status <> 'missing'")
        params: list = [user_id]
        if not include_deleted:
            where.append("deleted_at IS NULL")
        else:
            where.append("deleted_at IS NOT NULL")
        if q:
            # V3 unified-search review: deep links carry only the stable id,
            # never a potentially sensitive filename in the URL. Filename
            # search stays fuzzy; artifact id search is exact and owner-scoped.
            where.append("(filename LIKE ? OR id = ?)")
            params.extend((f"%{q}%", q))
        if sources:
            ph = ",".join("?" for _ in sources)
            where.append(f"source IN ({ph})")
            params.extend(sources)
        if kinds:
            ph = ",".join("?" for _ in kinds)
            where.append(f"kind IN ({ph})")
            params.extend(kinds)
        # mime_families: translate family back into mime prefixes / complete names
        if mime_families:
            mime_clauses: list[str] = []
            for fam in mime_families:
                if fam == "image":
                    mime_clauses.append("mime LIKE 'image/%'")
                elif fam == "pdf":
                    mime_clauses.append("mime = 'application/pdf'")
                elif fam == "presentation":
                    mime_clauses.append("mime LIKE '%presentationml%'")
                elif fam == "spreadsheet":
                    mime_clauses.append(
                        "(mime LIKE '%spreadsheetml%' OR mime = 'text/csv' "
                        "OR mime = 'text/tab-separated-values' "
                        "OR mime = 'application/vnd.ms-excel')"
                    )
                elif fam == "document":
                    mime_clauses.append("mime LIKE '%wordprocessingml%'")
                elif fam == "text":
                    mime_clauses.append("(mime LIKE 'text/%' OR mime = 'application/json' OR mime = 'application/x-ndjson')")
                elif fam == "other":
                    mime_clauses.append(
                        "mime NOT LIKE 'image/%' AND mime != 'application/pdf' "
                        "AND mime NOT LIKE '%presentationml%' "
                        "AND mime NOT LIKE '%spreadsheetml%' "
                        "AND mime != 'text/csv' "
                        "AND mime NOT LIKE '%wordprocessingml%' "
                        "AND mime NOT LIKE 'text/%' "
                        "AND mime != 'application/json'"
                    )
            if mime_clauses:
                where.append("(" + " OR ".join(mime_clauses) + ")")
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if until:
            where.append("created_at < ?")
            params.append(until)
        # Restrict by ID set before paging (file category filter: another DB determines KB membership; caller supplies the set)
        if include_ids is not None:
            inc = sorted({i for i in include_ids if i})
            if not inc:
                return [], 0, 0
            where.append(f"id IN ({','.join('?' for _ in inc)})")
            params.extend(inc)
        if exclude_ids:
            exc = sorted({i for i in exclude_ids if i})
            where.append(f"id NOT IN ({','.join('?' for _ in exc)})")
            params.extend(exc)
        if category:
            where.append("category = ?")
            params.append(category)

        sort_sql = {
            "created_desc": "created_at DESC",
            "size_desc": "size DESC",
            "last_used_desc": "COALESCE(last_used_at, created_at) DESC",
        }.get(sort, "created_at DESC")

        where_sql = " AND ".join(where)
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # total count + size_sum
            async with db.execute(
                f"SELECT COUNT(*) as n, COALESCE(SUM(size), 0) as sz FROM artifacts WHERE {where_sql}",
                params,
            ) as cur:
                stat = await cur.fetchone()
            total = int(stat["n"]) if stat else 0
            size_sum = int(stat["sz"]) if stat else 0
            # page
            async with db.execute(
                f"SELECT * FROM artifacts WHERE {where_sql} ORDER BY {sort_sql} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ) as cur:
                rows = await cur.fetchall()
        items = [_row_to_artifact(r) for r in rows]
        return items, total, size_sum

    # ─── Results complete read model ───────────────────────────
    @staticmethod
    def _results_membership_cte() -> str:
        return """
        WITH session_artifact_ids AS (
            SELECT DISTINCT ta.artifact_id
            FROM turn_attachments ta
            JOIN turns t ON t.id = ta.turn_id
            WHERE t.session_id = ?
            UNION
            SELECT sa.artifact_id
            FROM session_attachments sa
            WHERE sa.session_id = ? AND sa.detached_at IS NULL
            UNION
            SELECT a.id
            FROM artifacts a
            JOIN canvases c ON c.id = a.canvas_id
            WHERE c.session_id = ?
            UNION
            SELECT a.id
            FROM artifacts a
            JOIN turns t ON t.id = a.created_by_turn
            WHERE t.session_id = ?
        )
        """

    @classmethod
    def _results_scope_sql(
        cls,
        *,
        session_id: str,
        user_id: str,
        authorized_artifact_ids: Sequence[str] | None,
    ) -> tuple[str, list[object], str, list[object]]:
        """Build one authorized artifact-ID relation for owner or reportOnly.

        ``None`` means the exact owner session membership plus owner fence.
        A concrete sequence means the already-authorized report closure; it is
        passed through JSON1 rather than a dynamic ``IN`` list and intentionally
        does not re-interpret the viewing principal as artifact owner.
        """

        if authorized_artifact_ids is None:
            return (
                cls._results_membership_cte(),
                [session_id, session_id, session_id, session_id],
                "AND a.created_by = ?",
                [user_id],
            )
        ordered_ids = list(
            dict.fromkeys(artifact_id for artifact_id in authorized_artifact_ids if artifact_id),
        )
        return (
            """
            WITH session_artifact_ids AS (
                SELECT DISTINCT CAST(value AS TEXT) AS artifact_id
                FROM json_each(?)
                WHERE type = 'text' AND CAST(value AS TEXT) <> ''
            )
            """,
            [json.dumps(ordered_ids, ensure_ascii=True, separators=(",", ":"))],
            "",
            [],
        )

    @staticmethod
    async def _register_results_sql_functions(db: aiosqlite.Connection) -> None:
        from bowhead_core.results_read_model import (
            artifact_group_ranks,
            artifact_search_rank,
        )

        def _artifact(
            filename: object,
            mime: object,
            source: object,
            kind: object,
            producer_kind: object,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                id="",
                filename=str(filename or ""),
                mime=str(mime or ""),
                source=str(source or ""),
                created_at="",
                kind=str(kind or ""),
                trust="trusted",
                producer_kind=(str(producer_kind) if producer_kind else None),
            )

        def _search_rank(
            filename: object,
            mime: object,
            source: object,
            kind: object,
            producer_kind: object,
            query: object,
        ) -> int:
            rank = artifact_search_rank(
                _artifact(filename, mime, source, kind, producer_kind),
                str(query or ""),
            )
            return 99 if rank is None else rank

        def _group_rank(
            position: int,
            filename: object,
            mime: object,
            source: object,
            kind: object,
            producer_kind: object,
        ) -> int:
            return artifact_group_ranks(
                _artifact(filename, mime, source, kind, producer_kind),
            )[position]

        await db.create_function(
            "bowhead_results_search_rank",
            6,
            _search_rank,
            deterministic=True,
        )
        for name, position in (
            ("bowhead_results_primary_rank", 0),
            ("bowhead_results_main_rank", 1),
            ("bowhead_results_secondary_rank", 2),
        ):
            await db.create_function(
                name,
                5,
                lambda filename, mime, source, kind, producer_kind, p=position: _group_rank(
                    p,
                    filename,
                    mime,
                    source,
                    kind,
                    producer_kind,
                ),
                deterministic=True,
            )

    async def results_high_water(
        self,
        *,
        session_id: str,
        user_id: str,
        authorized_artifact_ids: Sequence[str] | None = None,
    ) -> ResultsArtifactSnapshot:
        """Freeze the newest authorized, user-visible artifact identity."""

        from bowhead_core.results_artifact_policy import results_internal_artifact_sql

        internal_sql, internal_params = results_internal_artifact_sql("a")
        scope_sql, scope_params, owner_sql, owner_params = self._results_scope_sql(
            session_id=session_id,
            user_id=user_id,
            authorized_artifact_ids=authorized_artifact_ids,
        )
        sql = (
            scope_sql
            + f"""
        SELECT a.created_at, a.id
        FROM artifacts a
        JOIN session_artifact_ids s ON s.artifact_id = a.id
        WHERE a.deleted_at IS NULL
          AND a.status = 'ready'
          {owner_sql}
          AND NOT {internal_sql}
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT 1
        """
        )
        params = [*scope_params, *owner_params, *internal_params]
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                row = await cursor.fetchone()
        return ResultsArtifactSnapshot(
            high_water_created_at=(str(row["created_at"]) if row else None),
            high_water_artifact_id=(str(row["id"]) if row else None),
        )

    async def results_snapshot_membership_digest(
        self,
        *,
        session_id: str,
        user_id: str,
        snapshot: ResultsArtifactSnapshot,
        authorized_artifact_ids: Sequence[str] | None = None,
    ) -> str:
        """Digest authorized IDs inside high-water to detect old-item attachment.

        New artifacts beyond high-water intentionally do not affect this value;
        attaching an older existing artifact or an exact-timestamp collision
        does, so the caller can stale the snapshot instead of inserting a row in
        the middle of already loaded pages.
        """

        from bowhead_core.results_artifact_policy import results_internal_artifact_sql

        internal_sql, internal_params = results_internal_artifact_sql("a")
        if snapshot.high_water_created_at is None:
            high_water_sql = " AND 0"
            high_water_params: list[object] = []
        else:
            high_water_sql = " AND (a.created_at < ? OR (a.created_at = ? AND a.id <= ?))"
            high_water_params = [
                snapshot.high_water_created_at,
                snapshot.high_water_created_at,
                snapshot.high_water_artifact_id,
            ]
        if snapshot.membership_cutoff_at is None:
            deletion_sql = "a.deleted_at IS NULL"
            deletion_params: list[object] = []
        else:
            # Keep IDs deleted after snapshot issuance in the digest. Deletion
            # may shrink a page without reordering the survivors. An artifact
            # already deleted when the snapshot was issued is absent, so a
            # later restore still changes the digest and safely stales paging.
            deletion_sql = "(a.deleted_at IS NULL OR a.deleted_at >= ?)"
            deletion_params = [snapshot.membership_cutoff_at]
        scope_sql, scope_params, owner_sql, owner_params = self._results_scope_sql(
            session_id=session_id,
            user_id=user_id,
            authorized_artifact_ids=authorized_artifact_ids,
        )
        sql = (
            scope_sql
            + f"""
        SELECT a.id
        FROM artifacts a
        JOIN session_artifact_ids s ON s.artifact_id = a.id
        WHERE {deletion_sql}
          AND a.status = 'ready'
          {owner_sql}
          AND NOT {internal_sql}
          {high_water_sql}
        ORDER BY a.id
        """
        )
        params: list[object] = [
            *scope_params,
            *deletion_params,
            *owner_params,
            *internal_params,
            *high_water_params,
        ]
        digest = hashlib.sha256()
        async with _open_db(self.db_path) as db, db.execute(sql, params) as cursor:
            for row in await cursor.fetchall():
                digest.update(str(row[0]).encode())
                digest.update(b"\0")
        return digest.hexdigest()

    async def get_result_artifact_for_scope(
        self,
        *,
        artifact_id: str,
        session_id: str,
        user_id: str,
        authorized_artifact_ids: Sequence[str] | None = None,
    ) -> Artifact | None:
        """Resolve one ready, visible artifact inside an authorized scope.

        This is the exact-key companion to the paged query.  Quick/recent and
        Locate must use the same owner/closure/internal fences as count/page;
        a direct ``get`` would turn those POST fields into an enumeration path.
        """

        from bowhead_core.results_artifact_policy import results_internal_artifact_sql

        internal_sql, internal_params = results_internal_artifact_sql("a")
        scope_sql, scope_params, owner_sql, owner_params = self._results_scope_sql(
            session_id=session_id,
            user_id=user_id,
            authorized_artifact_ids=authorized_artifact_ids,
        )
        sql = (
            scope_sql
            + f"""
        SELECT a.*
        FROM artifacts a
        JOIN session_artifact_ids s ON s.artifact_id = a.id
        WHERE a.id = ?
          AND a.deleted_at IS NULL
          AND a.status = 'ready'
          {owner_sql}
          AND NOT {internal_sql}
        LIMIT 1
        """
        )
        params: list[object] = [
            *scope_params,
            artifact_id,
            *owner_params,
            *internal_params,
        ]
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                row = await cursor.fetchone()
        return _row_to_artifact(row) if row is not None else None

    async def query_results_for_session(
        self,
        *,
        session_id: str,
        user_id: str,
        snapshot: ResultsArtifactSnapshot,
        normalized_query: str,
        primary: Literal["all", "deliverables", "evidence"] = "all",
        secondary: Literal["all", "data", "code", "input"] = "all",
        authorized_artifact_ids: Sequence[str] | None = None,
        direct_report_artifact_ids: Sequence[str] = (),
        relation_search_artifact_ids: Sequence[str] = (),
        report_direct_only: bool = False,
        cursor_sort_tuple: tuple[int | str | None, ...] | None = None,
        direction: Literal["next", "previous"] = "next",
        limit: int = 50,
    ) -> ResultsArtifactQueryPage:
        """Return exact counts plus a mixed-direction keyset artifact page.

        Membership, deletion, internal visibility and snapshot high-water are
        applied before count/search/windowing.  This method intentionally has
        no offset path: callers page only with the encrypted unified tuple.
        """

        if not 1 <= limit <= 64:
            raise ValueError("results artifact page limit must be 1..64")
        if cursor_sort_tuple is not None and len(cursor_sort_tuple) != 7:
            raise ValueError("invalid results artifact cursor tuple")
        from bowhead_core.results_artifact_policy import results_internal_artifact_sql

        internal_sql, internal_params = results_internal_artifact_sql("a")
        high_water_sql: str
        high_water_params: list[object]
        if snapshot.high_water_created_at is None:
            high_water_sql = " AND 0"
            high_water_params = []
        else:
            high_water_sql = " AND (a.created_at < ? OR (a.created_at = ? AND a.id <= ?))"
            high_water_params = [
                snapshot.high_water_created_at,
                snapshot.high_water_created_at,
                snapshot.high_water_artifact_id,
            ]
        scope_sql, scope_params, owner_sql, owner_params = self._results_scope_sql(
            session_id=session_id,
            user_id=user_id,
            authorized_artifact_ids=authorized_artifact_ids,
        )
        direct_ids_json = json.dumps(
            list(dict.fromkeys(item for item in direct_report_artifact_ids if item)),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        relation_search_ids_json = json.dumps(
            list(
                dict.fromkeys(item for item in relation_search_artifact_ids if item),
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        projection_sql = """
        MIN(
          bowhead_results_search_rank(filename,mime,source,kind,producer_kind,?),
          CASE WHEN id IN (SELECT artifact_id FROM relation_search_ids)
               THEN 4 ELSE 99 END
        ) AS search_rank,
        bowhead_results_primary_rank(filename,mime,source,kind,producer_kind) AS filter_primary_rank,
        bowhead_results_secondary_rank(filename,mime,source,kind,producer_kind) AS filter_secondary_rank,
        CASE WHEN ? <> '' THEN 0 ELSE bowhead_results_primary_rank(filename,mime,source,kind,producer_kind) END AS primary_rank,
        CASE WHEN ? <> '' THEN 0 ELSE bowhead_results_main_rank(filename,mime,source,kind,producer_kind) END AS main_rank,
        CASE WHEN id IN (SELECT artifact_id FROM direct_report_ids) THEN 0 ELSE 1 END AS direct_rank,
        CASE WHEN ? <> '' THEN 0 ELSE bowhead_results_secondary_rank(filename,mime,source,kind,producer_kind) END AS secondary_rank,
        'artifact:' || id AS stable_key
        """
        facetable_where = ["search_rank < 99"]
        if report_direct_only:
            facetable_where.append("direct_rank = 0")
        matched_where: list[str] = []
        if primary == "deliverables":
            matched_where.append("filter_primary_rank = 0")
        elif primary == "evidence":
            matched_where.append("filter_primary_rank = 1")
        if secondary == "data":
            matched_where.append(
                "filter_primary_rank = 1 AND filter_secondary_rank = 0",
            )
        elif secondary == "code":
            matched_where.append("0")
        elif secondary == "input":
            matched_where.append(
                "filter_primary_rank = 1 AND filter_secondary_rank = 2",
            )
        order_asc = "search_rank,primary_rank,main_rank,direct_rank,secondary_rank,created_at DESC,stable_key"
        base_sql = (
            scope_sql
            + f"""
        , direct_report_ids AS (
            SELECT DISTINCT CAST(value AS TEXT) AS artifact_id
            FROM json_each(?)
            WHERE type = 'text' AND CAST(value AS TEXT) <> ''
        )
        , relation_search_ids AS (
            SELECT DISTINCT CAST(value AS TEXT) AS artifact_id
            FROM json_each(?)
            WHERE type = 'text' AND CAST(value AS TEXT) <> ''
        )
        , authorized AS MATERIALIZED (
            SELECT a.*
            FROM artifacts a
            JOIN session_artifact_ids s ON s.artifact_id = a.id
            WHERE a.deleted_at IS NULL
              AND a.status = 'ready'
              {owner_sql}
              AND NOT {internal_sql}
              {high_water_sql}
        ), projected AS MATERIALIZED (
            SELECT authorized.*, {projection_sql}
            FROM authorized
        ), facetable AS MATERIALIZED (
            SELECT * FROM projected WHERE {" AND ".join(facetable_where)}
        ), matched AS MATERIALIZED (
            SELECT * FROM facetable WHERE {" AND ".join(matched_where) if matched_where else "1"}
        ), ranked AS MATERIALIZED (
            SELECT matched.*,
                   ROW_NUMBER() OVER (ORDER BY {order_asc}) AS result_position
            FROM matched
        )
        """
        )
        base_params: list[object] = [
            *scope_params,
            direct_ids_json,
            relation_search_ids_json,
            *owner_params,
            *internal_params,
            *high_water_params,
            normalized_query,
            normalized_query,
            normalized_query,
            normalized_query,
        ]

        keyset_sql = ""
        keyset_params: list[object] = []
        if cursor_sort_tuple is not None:
            (
                search_rank,
                primary_rank,
                main_rank,
                direct_rank,
                secondary_rank,
                created_at,
                stable_key,
            ) = cursor_sort_tuple
            if not (
                isinstance(search_rank, int)
                and isinstance(primary_rank, int)
                and isinstance(main_rank, int)
                and isinstance(direct_rank, int)
                and isinstance(secondary_rank, int)
                and isinstance(created_at, str)
                and isinstance(stable_key, str)
            ):
                raise ValueError("invalid results artifact cursor values")
            rank_columns = (
                "search_rank",
                "primary_rank",
                "main_rank",
                "direct_rank",
                "secondary_rank",
            )
            rank_values = (
                search_rank,
                primary_rank,
                main_rank,
                direct_rank,
                secondary_rank,
            )
            comparisons: list[str] = []
            for index, (column, value) in enumerate(zip(rank_columns, rank_values, strict=True)):
                prefix = " AND ".join(f"{prior} = ?" for prior in rank_columns[:index])
                operator = ">" if direction == "next" else "<"
                comparisons.append(f"({prefix + ' AND ' if prefix else ''}{column} {operator} ?)")
                keyset_params.extend([*rank_values[:index], value])
            equal_ranks = " AND ".join(f"{column} = ?" for column in rank_columns)
            time_operator = "<" if direction == "next" else ">"
            key_operator = ">" if direction == "next" else "<"
            comparisons.append(f"({equal_ranks} AND created_at {time_operator} ?)")
            keyset_params.extend([*rank_values, created_at])
            comparisons.append(f"({equal_ranks} AND created_at = ? AND stable_key {key_operator} ?)")
            keyset_params.extend([*rank_values, created_at, stable_key])
            keyset_sql = "WHERE " + " OR ".join(comparisons)

        page_order = (
            order_asc
            if direction == "next"
            else "search_rank DESC,primary_rank DESC,main_rank DESC,direct_rank DESC,secondary_rank DESC,created_at,stable_key DESC"
        )
        combined_sql = (
            base_sql
            + f"""
        , page AS MATERIALIZED (
            SELECT * FROM ranked
            {keyset_sql}
            ORDER BY {page_order}
            LIMIT ?
        ), stats AS (
            SELECT
              (SELECT COUNT(*) FROM authorized) AS authorized_total,
              (SELECT COUNT(*) FROM matched) AS matched_total,
              (SELECT COUNT(*) FROM facetable WHERE filter_primary_rank = 0) AS deliverables_count,
              (SELECT COUNT(*) FROM facetable WHERE filter_primary_rank = 1) AS evidence_count,
              (SELECT COUNT(*) FROM facetable
               WHERE filter_primary_rank = 1 AND filter_secondary_rank = 0) AS data_count,
              (SELECT COUNT(*) FROM facetable
               WHERE filter_primary_rank = 1 AND filter_secondary_rank = 1) AS code_count,
              (SELECT COUNT(*) FROM facetable
               WHERE filter_primary_rank = 1 AND filter_secondary_rank = 2) AS input_count
        )
        SELECT page.*, stats.*
        FROM stats
        LEFT JOIN page ON 1=1
        ORDER BY {page_order}
        """
        )
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._register_results_sql_functions(db)
            await db.execute("BEGIN")
            try:
                async with db.execute(
                    combined_sql,
                    [*base_params, *keyset_params, limit + 1],
                ) as cursor:
                    rows = await cursor.fetchall()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        stats_row = rows[0]
        page_rows = [row for row in rows if row["id"] is not None]
        has_more = len(page_rows) > limit
        page_rows = page_rows[:limit]
        if direction == "previous":
            page_rows.reverse()
        entries = tuple(
            ResultsArtifactEntry(
                artifact=_row_to_artifact(row),
                position=int(row["result_position"]),
                sort_tuple=(
                    int(row["search_rank"]),
                    int(row["primary_rank"]),
                    int(row["main_rank"]),
                    int(row["direct_rank"]),
                    int(row["secondary_rank"]),
                    str(row["created_at"]),
                    str(row["stable_key"]),
                ),
            )
            for row in page_rows
        )
        return ResultsArtifactQueryPage(
            entries=entries,
            authorized_total=int(stats_row["authorized_total"]),
            matched_total=int(stats_row["matched_total"]),
            group_counts=ResultsArtifactGroupCounts(
                deliverables=int(stats_row["deliverables_count"]),
                evidence=int(stats_row["evidence_count"]),
                data=int(stats_row["data_count"]),
                code=int(stats_row["code_count"]),
                input=int(stats_row["input_count"]),
            ),
            has_more_in_direction=has_more,
        )

    async def count_new_results_for_session(
        self,
        *,
        session_id: str,
        user_id: str,
        snapshot: ResultsArtifactSnapshot,
        normalized_query: str,
        primary: Literal["all", "deliverables", "evidence"] = "all",
        secondary: Literal["all", "data", "code", "input"] = "all",
        authorized_artifact_ids: Sequence[str] | None = None,
        direct_report_artifact_ids: Sequence[str] = (),
        relation_search_artifact_ids: Sequence[str] = (),
        report_direct_only: bool = False,
    ) -> int:
        """Count matching live artifacts strictly newer than a snapshot."""

        from bowhead_core.results_artifact_policy import results_internal_artifact_sql

        internal_sql, internal_params = results_internal_artifact_sql("a")
        if snapshot.high_water_created_at is None:
            newer_sql = ""
            newer_params: list[object] = []
        else:
            newer_sql = " AND (a.created_at > ? OR (a.created_at = ? AND a.id > ?))"
            newer_params = [
                snapshot.high_water_created_at,
                snapshot.high_water_created_at,
                snapshot.high_water_artifact_id,
            ]
        scope_sql, scope_params, owner_sql, owner_params = self._results_scope_sql(
            session_id=session_id,
            user_id=user_id,
            authorized_artifact_ids=authorized_artifact_ids,
        )
        direct_ids_json = json.dumps(
            list(dict.fromkeys(item for item in direct_report_artifact_ids if item)),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        relation_search_ids_json = json.dumps(
            list(
                dict.fromkeys(item for item in relation_search_artifact_ids if item),
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        filters = ["search_rank < 99"]
        if report_direct_only:
            filters.append("direct_rank = 0")
        if primary == "deliverables":
            filters.append("primary_rank = 0")
        elif primary == "evidence":
            filters.append("primary_rank = 1")
        if secondary == "data":
            filters.append("primary_rank = 1 AND secondary_rank = 0")
        elif secondary == "code":
            filters.append("0")
        elif secondary == "input":
            filters.append("primary_rank = 1 AND secondary_rank = 2")
        sql = (
            scope_sql
            + f"""
        , direct_report_ids AS (
            SELECT DISTINCT CAST(value AS TEXT) AS artifact_id
            FROM json_each(?)
            WHERE type = 'text' AND CAST(value AS TEXT) <> ''
        )
        , relation_search_ids AS (
            SELECT DISTINCT CAST(value AS TEXT) AS artifact_id
            FROM json_each(?)
            WHERE type = 'text' AND CAST(value AS TEXT) <> ''
        )
        , projected AS (
            SELECT
              MIN(
                bowhead_results_search_rank(
                  a.filename,a.mime,a.source,a.kind,a.producer_kind,?
                ),
                CASE WHEN a.id IN (SELECT artifact_id FROM relation_search_ids)
                     THEN 4 ELSE 99 END
              ) AS search_rank,
              bowhead_results_primary_rank(
                a.filename,a.mime,a.source,a.kind,a.producer_kind
              ) AS primary_rank,
              bowhead_results_secondary_rank(
                a.filename,a.mime,a.source,a.kind,a.producer_kind
              ) AS secondary_rank,
              CASE WHEN a.id IN (SELECT artifact_id FROM direct_report_ids)
                   THEN 0 ELSE 1 END AS direct_rank
            FROM artifacts a
            JOIN session_artifact_ids s ON s.artifact_id = a.id
            WHERE a.deleted_at IS NULL
              AND a.status = 'ready'
              {owner_sql}
              AND NOT {internal_sql}
              {newer_sql}
        )
        SELECT COUNT(*) FROM projected WHERE {" AND ".join(filters)}
        """
        )
        params: list[object] = [
            *scope_params,
            direct_ids_json,
            relation_search_ids_json,
            normalized_query,
            *owner_params,
            *internal_params,
            *newer_params,
        ]
        async with _open_db(self.db_path) as db:
            await self._register_results_sql_functions(db)
            async with db.execute(sql, params) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # ─── Session-scoped list（#100 Data tab）───────────────────
    async def list_for_session(
        self,
        *,
        session_id: str,
        user_id: str,
        artifact_id: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
        results_visible_only: bool = False,
    ) -> list[Artifact]:
        """列本 session 触碰过的 artifact（去重 + owner 校验）.

        4 类来源:
        1. 本 session 任一 turn 的 `turn_attachments`（用户上传附件）
        2. 本 session 的 `session_attachments`（pin 到 session 层的附件）
        3. 本 session 所有 canvas 产出的 artifact（`artifacts.canvas_id → canvases.session_id`）
        4. producer 明确记录的 `artifacts.created_by_turn → turns.session_id`

        运行产物必须在写入时通过 `session_id` 建立 `session_attachments` 关系。
        禁止用同用户时间窗推断归属；同一用户可以并发运行多个 session，时间窗会把
        一个 session 的数据暴露到另一个 session 的 Results 中。

        权限：`created_by` 非 `user_id` 一律过滤.
        """
        like = f"%{q}%" if q else None
        # One UNION collects all sources; final JOIN artifacts gets details.
        sql = """
        WITH session_artifact_ids AS (
            -- 1. turn_attachments
            SELECT DISTINCT ta.artifact_id
            FROM turn_attachments ta
            JOIN turns t ON t.id = ta.turn_id
            WHERE t.session_id = ?
            UNION
            -- 2. session_attachments
            SELECT sa.artifact_id
            FROM session_attachments sa
            WHERE sa.session_id = ? AND sa.detached_at IS NULL
            UNION
            -- 3. canvas_id -> session
            SELECT a.id
            FROM artifacts a
            JOIN canvases c ON c.id = a.canvas_id
            WHERE c.session_id = ?
            UNION
            -- 4. producer turn -> session (precise legacy/runtime fallback)
            SELECT a.id
            FROM artifacts a
            JOIN turns t ON t.id = a.created_by_turn
            WHERE t.session_id = ?
        )
        SELECT a.*
        FROM artifacts a
        JOIN session_artifact_ids s ON s.artifact_id = a.id
        WHERE a.deleted_at IS NULL AND a.status <> 'missing'
          AND a.created_by = ? AND COALESCE(a.producer_kind,'')<>'template_document_internal_v1'
        """
        params: list = [
            session_id,  # turn_attachments
            session_id,  # session_attachments
            session_id,  # canvases
            session_id,  # created_by_turn
            user_id,  # final created_by guard
        ]
        if like:
            sql += " AND a.filename LIKE ?"
            params.append(like)
        if artifact_id:
            sql += " AND a.id = ?"
            params.append(artifact_id)
        if results_visible_only:
            from bowhead_core.results_artifact_policy import (
                results_internal_artifact_sql,
            )

            internal_sql, internal_params = results_internal_artifact_sql("a")
            sql += f" AND NOT {internal_sql}"
            params.extend(internal_params)
        sql += " ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            try:
                async with db.execute(sql, params) as cur:
                    rows = await cur.fetchall()
            except aiosqlite.OperationalError:
                # canvases / sessions may be absent in some dev deployments; degrade to path 3
                sql_fallback = """
                WITH session_artifact_ids AS (
                    SELECT DISTINCT ta.artifact_id
                    FROM turn_attachments ta
                    JOIN turns t ON t.id = ta.turn_id
                    WHERE t.session_id = ?
                    UNION
                    SELECT sa.artifact_id
                    FROM session_attachments sa
                    WHERE sa.session_id = ? AND sa.detached_at IS NULL
                    UNION
                    SELECT a.id
                    FROM artifacts a
                    JOIN turns t ON t.id = a.created_by_turn
                    WHERE t.session_id = ?
                )
                SELECT a.* FROM artifacts a
                JOIN session_artifact_ids s ON s.artifact_id = a.id
                WHERE a.deleted_at IS NULL AND a.status <> 'missing' AND a.created_by = ? AND COALESCE(a.producer_kind,'')<>'template_document_internal_v1'
                """
                p2: list = [session_id, session_id, session_id, user_id]
                if like:
                    sql_fallback += " AND a.filename LIKE ?"
                    p2.append(like)
                if artifact_id:
                    sql_fallback += " AND a.id = ?"
                    p2.append(artifact_id)
                if results_visible_only:
                    from bowhead_core.results_artifact_policy import (
                        results_internal_artifact_sql,
                    )

                    internal_sql, internal_params = results_internal_artifact_sql("a")
                    sql_fallback += f" AND NOT {internal_sql}"
                    p2.extend(internal_params)
                sql_fallback += " ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?"
                p2 += [limit, offset]
                async with db.execute(sql_fallback, p2) as cur:
                    rows = await cur.fetchall()
        return [_row_to_artifact(r) for r in rows]

    # ─── Soft deletion / restoration ───────────────────────────
    async def soft_delete(self, artifact_id: str, *, user_id: str) -> bool:
        """软删。user_id 必须匹配 created_by 才允许. 返 True 删了；False 没找到或无权."""
        from bowhead_core.documents.retention import DocumentArtifactInUse, active_ref_sql

        async with _open_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                f"SELECT 1 FROM artifacts WHERE id=? AND created_by=? AND {await active_ref_sql(db)}", (artifact_id, user_id)
            ) as check:
                if await check.fetchone() is not None:
                    raise DocumentArtifactInUse()
            cur = await db.execute(
                "UPDATE artifacts SET deleted_at = ? WHERE id = ? AND created_by = ? AND deleted_at IS NULL",
                (_now(), artifact_id, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def restore(self, artifact_id: str, *, user_id: str) -> bool:
        """恢复软删；已进入不可撤销 purge 的文件不恢复。其余未找到/无权同样返 False。"""
        from bowhead_core.artifact_purge import purge_schema_available

        async with _open_db(self.db_path) as db:
            guard = " AND id NOT IN (SELECT artifact_id FROM artifact_purge_claims)" if await purge_schema_available(db) else ""
            cur = await db.execute(
                "UPDATE artifacts SET deleted_at = NULL WHERE id = ? AND created_by = ? AND deleted_at IS NOT NULL AND COALESCE(producer_kind,'')<>'template_document_internal_v1'"
                + guard,
                (artifact_id, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_soft_deleted_ids(self, *, limit: int = 5000, offset: int = 0) -> list[str]:
        """回收站里的 artifact id（给知识库引用对账用，见 gateway 的 GC loop）。

        分页是必需的而非可选：调用方必须**取尽**才能安全硬删——只对账第一批，
        会让超出批次的知识库引用文件被静默永久删除。`ORDER BY id` 保证分页
        稳定（无序分页在并发写入下会漏行）。
        """
        async with (
            _open_db(self.db_path) as db,
            db.execute(
                "SELECT id FROM artifacts WHERE deleted_at IS NOT NULL ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cur,
        ):
            return [r[0] for r in await cur.fetchall()]

    async def force_restore(self, artifact_ids: list[str]) -> int:
        """无 owner 校验地恢复软删（**仅供系统对账**，不接任何用户输入）。

        file-management-v2 P2：`restore()` 要求 user_id 匹配，那是用户操作的正确
        约束；但知识库引用对账是系统行为——跑在后台任务里没有请求身份，且必须能救
        任意用户的文件。否则在 D2 防线上线前被删掉的知识库文件会在 GC 到期后
        **永久**打穿检索（超出 30 天可恢复窗口，无法补救）。
        """
        ids = [i for i in artifact_ids if i]
        if not ids:
            return 0
        ph = ",".join("?" for _ in ids)
        from bowhead_core.artifact_purge import purge_schema_available

        async with _open_db(self.db_path) as db:
            guard = " AND id NOT IN (SELECT artifact_id FROM artifact_purge_claims)" if await purge_schema_available(db) else ""
            cur = await db.execute(
                f"UPDATE artifacts SET deleted_at = NULL WHERE id IN ({ph}) AND deleted_at IS NOT NULL AND COALESCE(producer_kind,'')<>'template_document_internal_v1'"
                + guard,  # noqa: S608
                ids,
            )
            await db.commit()
            return cur.rowcount or 0

    #: P4 TTL collects only these three kinds: byproducts of agent work.
    RUN_DATA_KINDS: ClassVar[tuple[str, ...]] = ("tool_data", "sandbox_output", "web_clip")

    #: Sources never subject to TTL, regardless of kind.
    #:
    #: `kb_upload` is the original KB upload: `routes/kb.py`'s put_bytes omits kind,
    #: defaulting to `tool_data`, which kind-based classification calls run data and TTL collects.
    #: Hard deletion after 30 days loses every KB document's original bytes. Callers must also exclude
    #: IDs referenced by kb_files (see `stale_run_data_ids` docstring); this fallback prevents damage even
    #: when a caller forgets.
    TTL_EXEMPT_SOURCES: ClassVar[tuple[str, ...]] = ("kb_upload",)

    async def _reference_filter(self, db: aiosqlite.Connection) -> tuple[str, set[str]]:
        """「还在用」的判据，返 (SQL 布尔表达式, 画布正文引用到的 id 集合)。

        **候选查询与落刀必须用同一份。** 这两处各写一遍是有前科的：2026-09-07 那次
        只改了 `stale_run_data_ids`，`expire_run_data_ids` 里的旧 blanket pin 排除留着，
        于是候选列出 1,029 条、落刀一条都不放行，整个改动在生产上是**空转**的
        （候选查询单独 dry-run 全绿，因为没穿过闸本身——见
        [[feedback_verify_at_the_gate_not_around_it]]）。
        """
        from bowhead_core.artifact_references import (
            active_reference_sql,
            canvas_referenced_artifact_ids,
        )

        return await active_reference_sql(db), await canvas_referenced_artifact_ids(db)

    async def stale_run_data_ids(self, *, days: int = 30, limit: int = 5000) -> list[str]:
        """列出到期、且**本库能看出来没人引用**的运行数据候选 id。

        故意分两步（列候选 → 调用方过滤 → `expire_run_data_ids` 落刀）：
        `kb_files` 在**另一个数据库**里（`kb_files.db`），SQL join 不到，只有
        gateway 的 GC 循环同时握着两个 store。判定"还有没有人引用"必须在那里
        合流，不能指望本方法一条 SQL 判完。

        ⚠ **「还在用」的判据在 `artifact_references`，不在这里。** 此前这条 SQL 拿
        「有没有被 pin」当判据，而 pin 是 `put_bytes` 在产物出生时自动打的 ⇒ 恒为真
        ⇒ 本方法**恒返空**（116 实测：3,785 条到期运行数据被这一条挡下 3,781 条，
        换任何天数回收都是 0）。现在改成真实引用边的并集；自动 pin 不再算引用，
        **用户手动 pin 仍然算**。
        """
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        cutoff = (_dt.now(tz=UTC) - _td(days=days)).isoformat(timespec="microseconds")
        kph = ",".join("?" for _ in self.RUN_DATA_KINDS)
        sph = ",".join("?" for _ in self.TTL_EXEMPT_SOURCES)

        async with _open_db(self.db_path) as db:
            # Canvas-body references are JSON, unavailable to this SQL check; collect them once and exclude in Python after SQL.
            # Same pattern as kb_files (SQL cannot join, so merge checks afterward).
            reference_sql, canvas_referenced = await self._reference_filter(db)
            # LIMIT applies before canvas filtering, so overfetch to avoid filtering every candidate and collecting 0 this round,
            # making the mechanism appear broken again. The final cap still obeys limit (truncated below).
            probe_limit = max(limit * 4, limit)
            async with db.execute(
                f"SELECT id FROM artifacts "  # noqa: S608 - placeholders only
                f"WHERE deleted_at IS NULL AND kind IN ({kph}) AND source NOT IN ({sph}) "
                "AND created_at < ? AND canvas_id IS NULL "
                f"AND NOT {reference_sql} "
                "ORDER BY id LIMIT ?",
                (*self.RUN_DATA_KINDS, *self.TTL_EXEMPT_SOURCES, cutoff, probe_limit),
            ) as cur:
                candidates = [r[0] for r in await cur.fetchall()]
        return [aid for aid in candidates if aid not in canvas_referenced][:limit]

    async def expire_run_data_ids(self, artifact_ids: Sequence[str]) -> int:
        """file-management-v2 P4：把给定的运行数据软删进回收站。返实际软删条数。

        运行数据（agent 查数据/跑代码/抓网页的副产物）是单调增长且用户无感知的
        写入——不回收的话，几个月后回收站与磁盘都被它塞满。资产（用户上传/成品/
        导出）**不在此列**：那是用户的东西，永不自动过期。

        **软策略**（PRD §4.6）：只软删（进回收站，仍可恢复，再走既有 30 天 GC）。
        kind / 未删状态 / 豁免 source / canvas 与**引用判据**在 WHERE 里再确认一次，
        防止候选查询后新增引用被 TTL 软删。跨库 KB 对账仍由调用方负责。

        ⚠ 判据必须与 `stale_run_data_ids` 同源（都走 `_reference_filter`）——
        两处各写一遍的后果见该方法的 docstring。
        """
        ids = [i for i in artifact_ids if i]
        if not ids:
            return 0
        kph = ",".join("?" for _ in self.RUN_DATA_KINDS)
        sph = ",".join("?" for _ in self.TTL_EXEMPT_SOURCES)

        async with _open_db(self.db_path) as db:
            # Same criterion as candidate lookup (`_reference_filter`). Recheck to close the window where references
            # appear after candidate listing but before deletion, without changing standards.
            reference_sql, canvas_referenced = await self._reference_filter(db)
            targets = [i for i in ids if i not in canvas_referenced]
            if not targets:
                return 0
            iph = ",".join("?" for _ in targets)
            cur = await db.execute(
                f"UPDATE artifacts SET deleted_at = ? "  # noqa: S608 - placeholders only
                f"WHERE id IN ({iph}) AND deleted_at IS NULL "
                f"AND kind IN ({kph}) AND source NOT IN ({sph}) "
                "AND canvas_id IS NULL "
                f"AND NOT {reference_sql}",
                (_now(), *targets, *self.RUN_DATA_KINDS, *self.TTL_EXEMPT_SOURCES),
            )
            await db.commit()
            return cur.rowcount or 0

    # ─── Missing-original reconciliation (artifact-volume batch 0, docs/sprints/artifact-volume-2026-09-04/batch0-missing-tech-design.md) ──
    # Only ready ↔ missing transitions; exclude other states, soft-deleted rows and document input copies (0048 trigger forbids status changes).

    MISSING_STATUS: ClassVar[str] = "missing"

    @staticmethod
    async def _document_copy_filter(db: aiosqlite.Connection) -> str:
        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_input_copies'",
        ) as cur:
            present = await cur.fetchone() is not None
        if not present:
            return "1=1"
        return "NOT EXISTS (SELECT 1 FROM document_input_copies c WHERE c.artifact_id = artifacts.id)"

    async def list_reconcile_candidates(self, *, after_id: str = "", limit: int = 500) -> list[tuple[str, str, str]]:
        """按 id 升序分页列出待对账的行：(id, path, status)，status ∈ {ready, missing}。"""
        async with _open_db(self.db_path) as db:
            doc_filter = await self._document_copy_filter(db)
            async with db.execute(
                "SELECT id, path, status FROM artifacts "  # noqa: S608 - fixed fragment
                f"WHERE deleted_at IS NULL AND status IN ('ready', ?) AND id > ? AND {doc_filter} "
                "ORDER BY id LIMIT ?",
                (self.MISSING_STATUS, after_id, limit),
            ) as cur:
                return [(r[0], r[1], r[2]) for r in await cur.fetchall()]

    async def list_recent_ready_paths(self, *, limit: int = 20) -> list[tuple[str, str]]:
        """最近创建的 ready 行 (id, path)：对账前判断存储本身是否健康的探针。"""
        async with _open_db(self.db_path) as db:
            doc_filter = await self._document_copy_filter(db)
            async with db.execute(
                "SELECT id, path FROM artifacts "  # noqa: S608 - fixed fragment
                f"WHERE deleted_at IS NULL AND status = 'ready' AND {doc_filter} "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                return [(r[0], r[1]) for r in await cur.fetchall()]

    async def set_blob_missing(self, artifact_ids: Sequence[str], *, missing: bool) -> int:
        """ready → missing（missing=True）或 missing → ready（False）。条件写在 SQL 里，返实际改动条数。"""
        ids = [i for i in artifact_ids if i]
        if not ids:
            return 0
        old, new = ("ready", self.MISSING_STATUS) if missing else (self.MISSING_STATUS, "ready")
        changed = 0
        async with _open_db(self.db_path) as db:
            doc_filter = await self._document_copy_filter(db)
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                iph = ",".join("?" for _ in chunk)
                cur = await db.execute(
                    f"UPDATE artifacts SET status = ? "  # noqa: S608 - placeholders only
                    f"WHERE id IN ({iph}) AND status = ? AND deleted_at IS NULL AND {doc_filter}",
                    (new, *chunk, old),
                )
                changed += cur.rowcount or 0
            await db.commit()
        return changed

    async def purge_now(self, artifact_ids: list[str], *, user_id: str) -> int:
        """P4「永久删除」：用户在回收站里主动清除，不等 30 天 GC。返真正清掉的条数。

        只对**自己的、已在回收站的**行生效（两个条件都写在 SQL 里，不靠调用方守规矩）。

        **真删**（blob + FK 行 + artifact 行），而不是把 `deleted_at` 改早让 GC 代劳：
        后者会让文件在回收站里继续显示最多 24 小时（GC 间隔），更糟的是——若它被
        知识库引用，GC 前的对账会把它 `force_restore` **复活**，用户点了永久删除
        却发现文件回来了。KB 引用的拦截在路由层（与 D2 同一把锁）。

        有效 Canvas 快照也阻止永久删除。先提交不可撤销 claim，再删 blob；
        存储响应不确定时保留 claim，重试或后台 GC 继续清理，不能恢复该行。
        必须已完成 Alembic purge migration；缺 schema 时不退回先删 blob 的旧实现。
        """
        ids = [i for i in artifact_ids if i]
        if not ids:
            return 0
        from bowhead_core.artifact_purge import purge_artifacts

        result = await purge_artifacts(self.db_path, self.backend, ids, user_id=user_id)
        return result.rows_removed

    async def list_ids_for_user(self, user_id: str) -> list[str]:
        """本人未删除产物的 id（文件页按「是否已加入知识库」筛选时，交给知识库库批量判定）。"""
        async with (
            _open_db(self.db_path) as db,
            db.execute("SELECT id FROM artifacts WHERE created_by = ? AND deleted_at IS NULL", (user_id,)) as cur,
        ):
            return [r[0] for r in await cur.fetchall()]

    async def list_usage_artifacts_for_user(
        self,
        user_id: str,
    ) -> list[Artifact]:
        """Return the live rows needed for lifecycle-aware usage totals."""

        kinds = (
            "user_upload",
            "deliverable",
            "canvas_export",
            "tool_data",
            "sandbox_output",
            "web_clip",
        )
        placeholders = ",".join("?" for _ in kinds)
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM artifacts WHERE created_by=? AND deleted_at IS NULL AND status <> 'missing' AND kind IN ({placeholders}) AND COALESCE(producer_kind,'')<>'template_document_internal_v1'",  # noqa: S608
                (user_id, *kinds),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_artifact(row) for row in rows]

    async def hard_delete_old_soft_deleted(self, *, days: int = 30) -> dict[str, int]:
        """#54 长期修 (2026-04-29): nightly cron 清回收站 — 软删超 N 天的硬删 (db row +
        backend 文件). 返 {"rows_removed": N, "files_removed": M, "files_failed": K,
        "stdout_rows_removed": L}.

        设计文档 (data-tab-file-preview-design.md) 提到 "30 天软删 + nightly hard delete",
        当时未实现; 跟 artifact 404 bug 共因: GC 缺失 → 老 artifact 跨 turn 引用风险高.

        Sprint ι (PRD §4.B.1 / R-γ): also runs an independent 90-day sweep on
        artifact_stdout (audit retention TTL, decoupled from artifact 30-day
        soft-delete window). Single-statement DELETE acceptable per project
        memory `feedback_no_canary_small_userbase`.
        """
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        cutoff = (_dt.now(tz=UTC) - _td(days=days)).isoformat(timespec="microseconds")
        from bowhead_core.artifact_purge import SNAPSHOT_PIN_SQL, purge_artifacts
        from bowhead_core.documents.retention import active_ref_sql

        async with _open_db(self.db_path) as db:
            async with db.execute(
                f"SELECT COUNT(*) FROM artifacts WHERE deleted_at IS NOT NULL AND deleted_at < ? AND {SNAPSHOT_PIN_SQL}",
                (cutoff,),
            ) as cur:
                row = await cur.fetchone()
                pins_skipped = int(row[0]) if row else 0
            # UNION also recovers recent manual purges whose storage acknowledgement
            # was lost. A claim never times out into a restorable artifact.
            async with db.execute(
                "SELECT id FROM artifacts WHERE deleted_at IS NOT NULL AND deleted_at < ? "
                f"AND NOT {SNAPSHOT_PIN_SQL} AND NOT {await active_ref_sql(db)} "
                "UNION SELECT artifact_id FROM artifact_purge_claims WHERE completed_at IS NULL",
                (cutoff,),
            ) as cur:
                stale_ids = [str(row[0]) for row in await cur.fetchall()]
        result = await purge_artifacts(self.db_path, self.backend, stale_ids, user_id=None, cutoff=cutoff)

        # Sprint ι: independent 90-day stdout sweep (different TTL than artifact GC).
        stdout_cutoff = (_dt.now(tz=UTC) - _td(days=90)).isoformat(timespec="microseconds")
        stdout_removed = 0
        async with _open_db(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM artifact_stdout WHERE created_at < ?",
                (stdout_cutoff,),
            )
            stdout_removed = cur.rowcount
            await db.commit()

        return {
            "rows_removed": result.rows_removed,
            "files_removed": result.files_removed,
            "files_failed": result.files_failed,
            "stdout_rows_removed": stdout_removed,
            "pins_skipped": pins_skipped,
        }

    async def mark_internal_producer(self, artifact_id: str, producer_kind: str) -> bool:
        """Mark a persisted artifact as an internal dependency; return whether a row changed.

        The server learns the stdout ``data_role`` from the runner result after upload.
        Marking it here avoids rebuilding the image-baked runner and bumping
        ``SANDBOX_VERSION`` for information already available on the server.

        Accept only internal producers: ``producer_kind`` controls user-visible results,
        so a general setter would let callers hide arbitrary artifacts from users.
        """
        from bowhead_core.results_artifact_policy import INTERNAL_PRODUCERS

        if producer_kind not in INTERNAL_PRODUCERS:
            raise ValueError(f"producer_kind {producer_kind!r} is not an internal producer")
        if not artifact_id:
            return False
        async with _open_db(self.db_path) as db:
            cur = await db.execute(
                "UPDATE artifacts SET producer_kind = ? WHERE id = ? AND deleted_at IS NULL",
                (producer_kind, artifact_id),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def set_category(
        self,
        artifact_id: str,
        *,
        category: str,
        source: str = "user",
        retention_hint: str | None = None,
    ) -> None:
        """写业务分类；`retention_hint` 非 None 时一并写（同一次分类调用的两个产物）。

        用户手改分类（source="user"）不传 retention_hint —— 那是系统判定的
        留存价值，与用户给的业务标签是正交维度，不该被覆盖。

        `category` 存语言无关的代码值；传入中文类别名时在这里归一化，保证任何写入方
        都不会再把中文落库（迁移 `0124_file_category_codes` 之后库里只有代码值）。
        """
        from bowhead_core.artifact_classifier import normalize_category

        category = normalize_category(category) or category
        async with _open_db(self.db_path) as db:
            if retention_hint is None:
                await db.execute(
                    "UPDATE artifacts SET category = ?, category_source = ? WHERE id = ?",
                    (category, source, artifact_id),
                )
            else:
                await db.execute(
                    "UPDATE artifacts SET category = ?, category_source = ?, retention_hint = ? WHERE id = ?",
                    (category, source, retention_hint, artifact_id),
                )
            await db.commit()

    async def usage_count(self, artifact_id: str) -> int:
        """统计本 artifact 被多少 turn 引用（turn_attachments JOIN）."""
        async with (
            _open_db(self.db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM turn_attachments WHERE artifact_id = ?",
                (artifact_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def usage_counts(self, artifact_ids: list[str]) -> dict[str, int]:
        """`usage_count` 的批量版：``{artifact_id: 被多少 turn 引用}``，没有引用的也在字典里（值 0）。"""
        ids = list(dict.fromkeys(i for i in artifact_ids if i))
        out = dict.fromkeys(ids, 0)
        for start in range(0, len(ids), 500):  # Well below SQLite's bound-parameter limit
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            async with (
                _open_db(self.db_path) as db,
                db.execute(
                    f"SELECT artifact_id, COUNT(*) FROM turn_attachments WHERE artifact_id IN ({placeholders}) GROUP BY artifact_id",  # noqa: S608 - placeholders only
                    chunk,
                ) as cur,
            ):
                for artifact_id, n in await cur.fetchall():
                    out[str(artifact_id)] = int(n)
        return out

    async def list_usages(
        self,
        artifact_id: str,
    ) -> list[dict]:
        """返 [{session_id, session_title, turn_id, turn_seq, user_text, used_at}].
        JOIN turn_attachments × turns × sessions.
        """
        q = (
            "SELECT t.session_id, s.title AS session_title, t.id AS turn_id, "
            "       t.seq AS turn_seq, t.user_text, t.started_at AS used_at "
            "FROM turn_attachments ta "
            "JOIN turns t ON ta.turn_id = t.id "
            "LEFT JOIN sessions s ON t.session_id = s.id "
            "WHERE ta.artifact_id = ? "
            "ORDER BY t.started_at DESC"
        )
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(q, (artifact_id,)) as cur:
                rows = await cur.fetchall()
        return [
            {
                "session_id": r["session_id"],
                "session_title": r["session_title"],
                "turn_id": r["turn_id"],
                "turn_seq": int(r["turn_seq"]) if r["turn_seq"] is not None else 0,
                "user_text": r["user_text"],
                "used_at": r["used_at"],
            }
            for r in rows
        ]

    async def origin_sessions(self, artifact_ids: list[str]) -> dict[str, dict[str, str | None]]:
        """file-management-v2 P1: 批量取 artifact 的「来源会话」（运行数据 tab 的列）。

        运行数据是 agent 干活时 `put_bytes(session_id=...)` 落的，关系在
        `session_attachments`（与 artifacts 同库，可直接 JOIN——不同于 kb_files）。
        一份 artifact 可能被 pin 到多个 session；取**最早 pin 的那个**当产地，
        因为用户问的是"这东西哪来的"，不是"它后来还被谁用过"（后者是抽屉的 usages）。

        返 {artifact_id: {"session_id", "session_title"}}；查不到的键直接缺席。
        """
        ids = [i for i in artifact_ids if i]
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        q = (
            "SELECT sa.artifact_id, sa.session_id, s.title AS session_title "  # noqa: S608 - placeholders only
            "FROM session_attachments sa "
            "LEFT JOIN sessions s ON sa.session_id = s.id "
            f"WHERE sa.artifact_id IN ({ph}) "
            "ORDER BY sa.pinned_at ASC"
        )
        out: dict[str, dict[str, str | None]] = {}
        async with _open_db(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(q, ids) as cur:
                for r in await cur.fetchall():
                    # ORDER BY pinned_at ASC + setdefault → keep the earliest row
                    out.setdefault(
                        r["artifact_id"],
                        {"session_id": r["session_id"], "session_title": r["session_title"]},
                    )
        return out

    async def open_read(self, art: Artifact) -> bytes:
        """读 blob（给 agent 注入 / skill 分析用）."""
        return await self.backend.read(art.path)

    def iter_read_range(
        self,
        art: Artifact,
        *,
        start: int,
        end: int,
    ) -> AsyncIterator[bytes]:
        """Stream ``[start, end)`` bytes from the configured backend."""
        return self.backend.iter_range(art.path, start=start, end=end)

    async def download_url(
        self,
        art: Artifact,
        *,
        expires_in: int = 3600,
        disposition: str = "attachment",
    ) -> str | None:
        """Presigned URL；返回 None 表示当前 backend 不支持，应走代理下载."""
        return await self.backend.download_url(
            path=art.path,
            filename=art.filename,
            mime=art.mime,
            expires_in=expires_in,
            disposition=disposition,
        )


def _row_to_artifact(row: aiosqlite.Row) -> Artifact:
    keys = row.keys() if hasattr(row, "keys") else []

    def _g(k: str) -> object:
        return row[k] if k in keys else None

    return Artifact(
        id=row["id"],
        filename=row["filename"],
        mime=row["mime"],
        size=int(row["size"]),
        source=row["source"],
        path=row["path"],
        canvas_id=row["canvas_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        deleted_at=_g("deleted_at") or None,  # type: ignore[arg-type]
        last_used_at=_g("last_used_at") or None,  # type: ignore[arg-type]
        category=_g("category") or None,  # type: ignore[arg-type]
        category_source=_g("category_source") or None,  # type: ignore[arg-type]
        code_lang=_g("code_lang") or None,  # type: ignore[arg-type]
        # M1 asset model — defaulted for legacy rows that predate the columns.
        kind=_g("kind") or "tool_data",  # type: ignore[arg-type]
        retention_hint=_g("retention_hint"),
        scope=_g("scope") or "session",  # type: ignore[arg-type]
        inject=_g("inject") or "list",  # type: ignore[arg-type]
        trust=_g("trust") or "trusted",  # type: ignore[arg-type]
        producer_kind=_g("producer_kind") or None,  # type: ignore[arg-type]
        created_by_turn=_g("created_by_turn") or None,  # type: ignore[arg-type]
        derived_from=_g("derived_from") or None,  # type: ignore[arg-type]
        manifest_artifact_id=_g("manifest_artifact_id") or None,  # type: ignore[arg-type]
        status=_g("status") or "ready",  # type: ignore[arg-type]
        # Batch 2a: legacy/unmigrated deployments lack this column; _g returns None ⇒ unrecorded, distinct from an empty string.
        origin_query=_g("origin_query") or None,  # type: ignore[arg-type]
        row_count=_g("row_count"),  # type: ignore[arg-type]
        # Space isolation batch 3: legacy/unmigrated deployments lack this column ⇒ None=unassigned. Also normalize empty strings to
        # None: `artifact_upload_bindings.workspace_id`'s NOT NULL DEFAULT '' makes
        # team uploads use empty strings, which are not Space IDs.
        workspace_id=_g("workspace_id") or None,  # type: ignore[arg-type]
    )
