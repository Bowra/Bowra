"""Canvas 导出 — 真实跑 skill runtime + 持久化产物到 ArtifactStore.

flow：
    enqueue_export(canvas_id, fmt, store, catalog, runtime, artifact_store)
      └─ 建 ExportJob(status=queued)
         └─ asyncio.create_task(_run_export) 异步跑
             └─ 1. 从 canvas_store 拉 sections
                2. rt.run(spec=canvas-<fmt>, input={sections, title})
                3. 成功 → artifact_store.put_file → job.url + job.artifact_id
                4. 失败 → job.error + job.status=failed

M3 将把 _jobs dict 换为 SQLite 持久化（支持 gateway 重启续命）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bowhead_skill.export_naming import export_filename, report_title_from_sections

from bowhead_core._db import open_shared_ctx
from bowhead_core.canvas_rank import canonical_rank, is_seq_authoritative
from bowhead_core.embedded_data_provenance import project_embedded_data_provenance
from bowhead_core.export_copy_contract import (
    CURRENT_MANIFEST_PRODUCER,
    MAX_COPY_MANIFEST_BYTES,
    TRUST_COPY_CONTRACT,
    output_producer_for,
)
from bowhead_core.source_access_projection import StaticSafeSourceMap, project_section_for_static_export
from bowhead_core.source_linkage_copy import (
    format_embedded_data_provenance,
    generic_source_type_copy,
    source_list_copy,
)

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact, ArtifactStore
    from bowhead_core.canvas_store import CanvasStore
    from bowhead_skill import SkillCatalog, SkillRuntime

logger = logging.getLogger(__name__)


# Canvas format → skill name mapping
_FMT_SKILL = {
    "pdf": "canvas-pdf",
    "pptx": "canvas-pptx",
    "xlsx": "canvas-xlsx",
    "docx": "canvas-docx",
}

_FMT_MIME = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass
class ExportJob:
    id: str
    canvas_id: str
    fmt: str
    status: str  # queued | running | done | failed
    contract_version: int = 1
    profile: str | None = None
    parity_contract: str | None = None
    surface_token: str | None = None
    phase: str = "queued"
    quality_status: str = "pending"
    warning_count: int = 0
    snapshot_id: str | None = None
    manifest_artifact_id: str | None = None
    requested_by: str | None = None
    idempotency_key: str | None = None
    request_sha256: str | None = None
    error_code: str | None = None
    progress_json: str | None = None
    attempt: int = 0
    worker_id: str | None = None
    runner_kind: str | None = None
    runner_task_id: str | None = None
    lease_expires_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    expires_at: str | None = None
    url: str | None = None
    artifact_id: str | None = None
    filename: str | None = None
    size: int | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


# In-process job table: hot cache; since WP1, each state transition mirrors to SQLite (after init_export_jobs).
# Historical jobs remain queryable/downloadable after gateway restart; interrupted jobs become failed instead of disappearing.
_jobs: dict[str, ExportJob] = {}
_db_path: str | None = None

_EXPORT_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS canvas_export_jobs (
    id          TEXT PRIMARY KEY,
    canvas_id   TEXT NOT NULL,
    fmt         TEXT NOT NULL,
    status      TEXT NOT NULL,
    url         TEXT,
    artifact_id TEXT,
    filename    TEXT,
    size        INTEGER,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""
_EXPORT_JOBS_INDEX = "CREATE INDEX IF NOT EXISTS idx_export_jobs_canvas ON canvas_export_jobs(canvas_id, created_at DESC)"
_EXPORT_JOBS_MIGRATIONS = (
    ("contract_version", "INTEGER NOT NULL DEFAULT 1"),
    ("profile", "TEXT"),
    ("parity_contract", "TEXT"),
    ("surface_token", "TEXT"),
    ("phase", "TEXT NOT NULL DEFAULT 'queued'"),
    ("quality_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("warning_count", "INTEGER NOT NULL DEFAULT 0"),
    ("snapshot_id", "TEXT"),
    ("manifest_artifact_id", "TEXT"),
    ("requested_by", "TEXT"),
    ("idempotency_key", "TEXT"),
    ("request_sha256", "TEXT"),
    ("error_code", "TEXT"),
    ("progress_json", "TEXT"),
    ("attempt", "INTEGER NOT NULL DEFAULT 0"),
    ("worker_id", "TEXT"),
    ("runner_kind", "TEXT"),
    ("runner_task_id", "TEXT"),
    ("lease_expires_at", "TEXT"),
    ("started_at", "TEXT"),
    ("finished_at", "TEXT"),
    ("expires_at", "TEXT"),
)
_EXPORT_JOBS_IDEMPOTENCY_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_export_jobs_idempotency
ON canvas_export_jobs(requested_by, idempotency_key)
WHERE idempotency_key IS NOT NULL
"""
_EXPORT_JOBS_STATUS_PHASE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_export_jobs_status_phase
ON canvas_export_jobs(status, phase, created_at)
"""


async def _ensure_export_job_column(db: Any, column: str, declaration: str) -> None:
    cur = await db.execute(
        "SELECT 1 FROM pragma_table_info('canvas_export_jobs') WHERE name = ?",
        (column,),
    )
    if await cur.fetchone() is None:
        await db.execute(f"ALTER TABLE canvas_export_jobs ADD COLUMN {column} {declaration}")


async def init_export_jobs(db_path: str) -> None:
    """gateway lifespan 调一次: 建表 + boot 恢复.

    asyncio task 不跨进程存活 — 重启时还在 queued/running 的 job 永远不会
    完成, 诚实标 failed (带人话原因), 而不是让前端轮询到天荒地老.
    """
    global _db_path  # noqa: PLW0603
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        await db.execute(_EXPORT_JOBS_SCHEMA)
        for column, declaration in _EXPORT_JOBS_MIGRATIONS:
            await _ensure_export_job_column(db, column, declaration)
        await db.execute(_EXPORT_JOBS_INDEX)
        await db.execute(_EXPORT_JOBS_IDEMPOTENCY_INDEX)
        await db.execute(_EXPORT_JOBS_STATUS_PHASE_INDEX)
        cur = await db.execute(
            "UPDATE canvas_export_jobs SET status='failed', "
            "error='export interrupted by server restart — please retry', "
            "updated_at=? WHERE contract_version=1 AND status IN ('queued','running')",
            (_now(),),
        )
        await db.commit()
        if cur.rowcount:
            logger.info("canvas_export.boot_recovery interrupted_jobs=%d", cur.rowcount)
    _db_path = db_path


_JOB_COLS = (
    "id",
    "canvas_id",
    "fmt",
    "status",
    "contract_version",
    "profile",
    "parity_contract",
    "surface_token",
    "phase",
    "quality_status",
    "warning_count",
    "snapshot_id",
    "manifest_artifact_id",
    "requested_by",
    "idempotency_key",
    "request_sha256",
    "error_code",
    "progress_json",
    "attempt",
    "worker_id",
    "runner_kind",
    "runner_task_id",
    "lease_expires_at",
    "started_at",
    "finished_at",
    "expires_at",
    "url",
    "artifact_id",
    "filename",
    "size",
    "error",
    "created_at",
    "updated_at",
)
_JOB_COLS_SQL = ", ".join(_JOB_COLS)
_JOB_PLACEHOLDERS_SQL = ",".join("?" for _ in _JOB_COLS)
_JOB_UPDATES_SQL = ", ".join(f"{column}=excluded.{column}" for column in _JOB_COLS if column != "id")


def _row_to_job(row: Sequence[Any]) -> ExportJob:
    return ExportJob(**dict(zip(_JOB_COLS, row, strict=True)))


async def _persist(job: ExportJob) -> None:
    if _db_path is None:
        return
    import aiosqlite

    try:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                f"INSERT INTO canvas_export_jobs ({_JOB_COLS_SQL}) VALUES ({_JOB_PLACEHOLDERS_SQL}) ON CONFLICT(id) DO UPDATE SET {_JOB_UPDATES_SQL}",
                tuple(getattr(job, column) for column in _JOB_COLS),
            )
            await db.commit()
    except Exception as e:
        # Persistence failure does not block export itself (in-memory state is still correct); only log it.
        logger.warning("canvas_export.persist_failed jid=%s err=%s", job.id, e)


def _persist_nowait(job: ExportJob) -> None:
    """sync 上下文 (_fail) 的 fire-and-forget 镜像; 状态单调, last-write-wins."""
    if _db_path is None:
        return
    try:  # noqa: SIM105
        asyncio.get_running_loop().create_task(_persist(job))
    except RuntimeError:
        pass  # No event loop (synchronous tests); in-memory state is still correct.


async def enqueue_export(
    *,
    canvas_id: str,
    fmt: str,
    store: CanvasStore,
    catalog: SkillCatalog | None = None,
    runtime: SkillRuntime | None = None,
    artifact_store: ArtifactStore | None = None,
    canvas_title: str | None = None,
    session_store: object | None = None,  # Optional; for PPTX template reuse
    template_artifact_id: str | None = None,  # V2.1: explicit user template
    use_clone_render: bool = False,  # V2.9 A6+ #60: template_clone_render fully preserves the master
    locale: str = "en-US",
) -> ExportJob:
    """建 job，fire-and-forget 异步跑 skill. 立刻返回."""
    fmt = fmt.lower()
    if fmt not in _FMT_SKILL:
        raise ValueError(f"unsupported fmt: {fmt} (want pdf|pptx|xlsx)")

    jid = f"exp_{uuid4().hex[:12]}"
    job = ExportJob(id=jid, canvas_id=canvas_id, fmt=fmt, status="queued")
    _jobs[jid] = job
    await _persist(job)

    # Without the skill stack, retain stub behavior (status stays queued without advancing; log a warning).
    if catalog is None or runtime is None or artifact_store is None:
        logger.warning(
            "canvas_export.stub jid=%s reason=no_skill_or_artifact_store canvas=%s fmt=%s",
            jid,
            canvas_id,
            fmt,
        )
        return job

    # Run in the background with an outer timeout and failure logging.
    # Large document exports can take several minutes; the outer budget complements
    # Skill timeouts and ensures a hung job eventually receives failed status.
    EXPORT_TIMEOUT_SEC = 900

    async def _run_with_guard() -> None:
        try:
            await asyncio.wait_for(
                _run_export(
                    job=job,
                    store=store,
                    catalog=catalog,
                    runtime=runtime,
                    artifact_store=artifact_store,
                    canvas_title=canvas_title,
                    session_store=session_store,
                    template_artifact_id=template_artifact_id,
                    use_clone_render=use_clone_render,
                    locale=locale,
                ),
                timeout=EXPORT_TIMEOUT_SEC,
            )
        except TimeoutError:
            logger.error(
                "canvas_export.timeout jid=%s canvas=%s fmt=%s timeout=%ds",
                jid,
                canvas_id,
                fmt,
                EXPORT_TIMEOUT_SEC,
            )
            _fail(job, f"export timed out after {EXPORT_TIMEOUT_SEC}s")
        except Exception as e:
            logger.exception("canvas_export.unhandled jid=%s canvas=%s fmt=%s", jid, canvas_id, fmt)
            _fail(job, f"unhandled: {type(e).__name__}: {e}")

    asyncio.create_task(_run_with_guard())
    return job


async def get_job(job_id: str) -> ExportJob | None:
    """内存热缓存优先 (本进程在跑的 job), miss 走 SQLite (历史/重启前的 job)."""
    job = _jobs.get(job_id)
    if job is not None or _db_path is None:
        return job
    import aiosqlite

    async with aiosqlite.connect(_db_path) as db:
        cur = await db.execute(
            f"SELECT {_JOB_COLS_SQL} FROM canvas_export_jobs WHERE id=?",
            (job_id,),
        )
        row = await cur.fetchone()
    return _row_to_job(row) if row else None


async def list_jobs_for_canvas(canvas_id: str, limit: int = 20) -> list[ExportJob]:
    """SQLite 为权威 (每次变迁镜像); 无 DB (stub/测试) 回退内存."""
    if _db_path is None:
        items = [j for j in _jobs.values() if j.canvas_id == canvas_id]
        items.sort(key=lambda j: j.created_at, reverse=True)
        return items[:limit]
    import aiosqlite

    async with aiosqlite.connect(_db_path) as db:
        cur = await db.execute(
            f"SELECT {_JOB_COLS_SQL} FROM canvas_export_jobs WHERE canvas_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (canvas_id, limit),
        )
        rows = await cur.fetchall()
    out = [_row_to_job(r) for r in rows]
    # Override matching IDs with in-memory jobs still in flight but not yet mirrored (the theoretical window is tiny).
    for i, j in enumerate(out):
        live = _jobs.get(j.id)
        if live is not None:
            out[i] = live
    return out


async def _inject_image_paths(
    sections: list,
    artifact_store: ArtifactStore,
    *,
    owner_user_id: str | None = None,
) -> None:
    """image section 的 artifact 字节预取到 tmp, 注入 _local_path (递归 section.body).

    feedback-0713 F5 review 安全修:
    - `_local_path` 是 render 脚本直接 open 的宿主路径, 而 section payload 的 `_local_path`
      用户可经 PATCH 写 (不在 _PATCH_PROTECTED_FIELDS) → 循环开头**无条件 strip**, 只有本函数
      成功 get 到 artifact 才重新注入, 杜绝用户塞任意宿主路径读文件 (安全 P3)。
    - `artifact_store.get` 传 `check_created_by=owner_user_id` (#54 owner 校验) —— 否则用户
      把别租户 artifact_id PATCH 进自己 image section, 导出即读到他人 artifact 字节 (跨租户
      IDOR, 安全 P2)。owner=canvas.user_id。
    """
    import tempfile

    img_dir: Path | None = None
    for s in sections:
        if not isinstance(s, dict):
            continue
        # Security: unconditionally clear user-controlled _local_path first, regardless of this section's subsequent path.
        s.pop("_local_path", None)
        if s.get("kind") == "section" and isinstance(s.get("body"), list):
            await _inject_image_paths(s["body"], artifact_store, owner_user_id=owner_user_id)
            continue
        if s.get("kind") != "image" or not s.get("artifact_id"):
            continue
        try:
            art = await artifact_store.get(str(s["artifact_id"]), check_created_by=owner_user_id)
            if art is None:
                continue
            if img_dir is None:
                img_dir = Path(tempfile.mkdtemp(prefix="bow-pdf-img-"))
            ext = Path(art.filename or "img.png").suffix or ".png"
            p = img_dir / f"{s['artifact_id']}{ext}"
            p.write_bytes(await artifact_store.open_read(art))
            s["_local_path"] = str(p.resolve())
        except Exception as e:
            logger.warning("canvas_export.image_inject_failed aid=%s err=%s", s.get("artifact_id"), e)


async def _load_owned_pptx_template(
    *,
    artifact_store: ArtifactStore,
    template_artifact_id: str,
    owner_user_id: str,
) -> Artifact | None:
    """Return one live PPTX owned by the Canvas owner, else hide the reason."""
    art = await artifact_store.get(
        template_artifact_id,
        check_created_by=owner_user_id,
        reject_null_owner=True,
    )
    if art is None:
        return None
    is_pptx = art.filename.lower().endswith(".pptx") or (art.mime or "").lower() == _FMT_MIME["pptx"]
    return art if is_pptx else None


def _canonical_export_order(sections: list) -> list:
    """批0 0-C leak#3：legacy 导出此前按裸 seq 出块。

    writer-loop 的 h1/meta 头部 section 落库晚于 peer 期的 code/chart，前端靠
    `preprocessSections` 的 rank 强排兜底，legacy 导出漏接同一套兜底 → PDF 标题
    出现在第 3 页、python 代码块领跑报告。这里复用 `canvas_rank`（与 FE 逐字对齐，
    test_canvas_rank_parity.py 钉死）：稳定排序，同 rank 保持 seq 相对序。
    seq 权威化 flag 翻开后与 FE 同步信任 seq，不再强排。
    """
    if is_seq_authoritative():
        return sections
    return sorted(sections, key=lambda s: canonical_rank(str(getattr(s, "kind", ""))))


def _static_safe_legacy_sections(value: object, *, locale: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    source_map = StaticSafeSourceMap()
    heading, _ = source_list_copy(locale)

    def project_one(
        item: object,
        *,
        position: int,
        parent_scope: str,
    ) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        section_id = str(item.get("id") or f"{parent_scope}-{position}")
        section_kind = str(item.get("kind") or "")
        projected, refs = project_section_for_static_export(
            section_id=section_id,
            section_kind=section_kind,
            payload=item,
            source_refs=item.get("source_refs") if isinstance(item.get("source_refs"), list) else (),
            source_map=source_map,
        )
        # ``source_refs`` belongs to the section envelope, not arbitrary
        # payload. Remove malformed/scalar legacy values even when no valid
        # ordinal survives.
        projected.pop("source_refs", None)
        if refs:
            projected["source_refs"] = refs
        if section_kind == "section" and isinstance(item.get("body"), list):
            children: list[dict[str, Any]] = []
            for child_position, child_item in enumerate(item["body"], start=1):
                child = project_one(
                    child_item,
                    position=child_position,
                    parent_scope=f"{section_id}-child",
                )
                if child is not None:
                    children.append(child)
            projected["body"] = children
        if section_kind == "sources":
            # Batch 0, 0-C leak#2: redacted entries retain only {source_ref, generic_type, ordinal}.
            # Labels were uniformly reduced to "来源", producing N zero-information "[i] Source" placeholders in PDF.
            # Do not restore real labels (redaction is a security decision); use the contract's retained generic_type for
            # limited wording and retain rows so [N] anchors survive. Omit the entire sources section if all entries are empty.
            entries = projected.get("entries")
            safe_entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
            if not safe_entries:
                return None
            projected["label"] = heading
            for entry in safe_entries:
                entry["label"] = generic_source_type_copy(entry.get("generic_type"), locale)
        if section_kind == "html_artifact":
            provenance = project_embedded_data_provenance(item)
            projected["embedded_data_provenance"] = asdict(provenance)
            projected["embedded_data_copy"] = format_embedded_data_provenance(provenance, locale)
        return projected

    projected_sections: list[dict[str, Any]] = []
    for position, item in enumerate(value, start=1):
        projected = project_one(
            item,
            position=position,
            parent_scope="legacy-section",
        )
        if projected is not None:
            projected_sections.append(projected)
    return projected_sections


def _validate_legacy_output(path: Path, fmt: str) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("export output is empty")
    if fmt == "pdf":
        with path.open("rb") as stream:
            magic = stream.read(5)
        if magic != b"%PDF-":
            raise ValueError("invalid PDF output")
        return
    try:
        with zipfile.ZipFile(path) as archive:
            names = frozenset(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("invalid Office output") from exc
    # Each of the three OOXML packages has its own main-part directory. The previous binary expression
    # `"ppt/" if fmt == "pptx" else "xl/"` required docx to contain `xl/`, so **every Word export failed**
    # (observed on dev exp_4e02c4d6896c: `unexpected: ValueError: invalid Office output`).
    _OOXML_PREFIX = {"pptx": "ppt/", "xlsx": "xl/", "docx": "word/"}
    required_prefix = _OOXML_PREFIX.get(fmt)
    if required_prefix is None:
        raise ValueError(f"invalid Office output: unsupported fmt {fmt!r}")
    if "[Content_Types].xml" not in names or not any(name.startswith(required_prefix) for name in names):
        raise ValueError("invalid Office output")


async def _commit_legacy_publish_transaction(db: Any) -> None:
    """Once publication starts, cancellation cannot orphan committed metadata."""

    commit_task = asyncio.create_task(db.commit())
    try:
        await asyncio.shield(commit_task)
    except asyncio.CancelledError:
        await commit_task


async def _persist_legacy_export_pair(
    *,
    job: ExportJob,
    artifact_store: ArtifactStore,
    output_path: Path,
    filename: str,
    mime: str,
    canvas_id: str,
    created_by: str,
    session_id: str | None,
    engine: str,
) -> Artifact:
    """Stage manifest/output and publish them with the legacy job terminal row."""
    _validate_legacy_output(output_path, job.fmt)
    # Test doubles and third-party stores retain the old compatibility path.
    # Such outputs have no B1a producer/manifest evidence and therefore always
    # classify as legacy; production ArtifactStore implements the staged API.
    if not all(hasattr(artifact_store, method) for method in ("stage_export_bytes", "stage_export_file", "commit_staged_artifacts")):
        return await artifact_store.put_file(
            src_path=output_path,
            filename=filename,
            mime=mime,
            source="canvas_export_clone" if engine == "legacy_clone" else "canvas_export",
            canvas_id=canvas_id,
            created_by=created_by,
        )

    manifest_content = json.dumps(
        {
            "manifest_version": 2,
            "trust_copy_contract": TRUST_COPY_CONTRACT,
            "job_id": job.id,
            "format": job.fmt,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(manifest_content) > MAX_COPY_MANIFEST_BYTES:
        raise ValueError("export manifest too large")
    staged: list[Any] = []
    committed = False
    try:
        manifest = await artifact_store.stage_export_bytes(
            content=manifest_content,
            filename=f"{Path(filename).stem}.manifest.json",
            mime="application/json",
            canvas_id=canvas_id,
            created_by=created_by,
            session_id=session_id,
            producer_kind=CURRENT_MANIFEST_PRODUCER,
        )
        staged.append(manifest)
        output = await artifact_store.stage_export_file(
            src_path=output_path,
            filename=filename,
            mime=mime,
            canvas_id=canvas_id,
            created_by=created_by,
            session_id=session_id,
            producer_kind=output_producer_for(engine=engine, fmt=job.fmt),  # type: ignore[arg-type]
            manifest_artifact_id=manifest.artifact.id,
        )
        staged.append(output)
        finished_at = _now()
        if _db_path is None:
            await artifact_store.commit_staged_artifacts((manifest, output))
        else:
            if Path(_db_path).resolve() != Path(artifact_store.db_path).resolve():
                raise RuntimeError("legacy export and artifact store must share SQLite")
            async with open_shared_ctx(_db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await artifact_store.insert_staged_artifacts_in_transaction(db, (manifest, output))
                    # Batch 0, 0-A (chart-table-ux tech-design §1.1 change 3): the legacy renderer
                    # replaces every chart with a data table (no graphics), so the output must not claim to be clean. A real production
                    # export of a 30-chart report contained zero charts yet was marked clean (observed on cv_55d680eda3f7),
                    # so the quality gate never fired. 'warnings' does not block downloads
                    # (routes/canvas.py accepts clean|warnings for download).
                    cursor = await db.execute(
                        "UPDATE canvas_export_jobs SET status='done', phase='terminal', "
                        "quality_status='warnings', warning_count=1, artifact_id=?, "
                        "manifest_artifact_id=?, url=?, filename=?, size=?, error=NULL, "
                        "finished_at=?, updated_at=? WHERE id=? AND contract_version=1 "
                        "AND status='running'",
                        (
                            output.artifact.id,
                            manifest.artifact.id,
                            f"/api/v1/files/{output.artifact.id}",
                            output.artifact.filename,
                            output.artifact.size,
                            finished_at,
                            finished_at,
                            job.id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("legacy export job terminal transition lost")
                    await _commit_legacy_publish_transaction(db)
                except BaseException:
                    await db.rollback()
                    raise
        committed = True
        job.status = "done"
        job.phase = "terminal"
        job.quality_status = "warnings"
        job.warning_count = 1
        job.finished_at = finished_at
        job.artifact_id = output.artifact.id
        job.manifest_artifact_id = manifest.artifact.id
        job.url = f"/api/v1/files/{output.artifact.id}"
        job.filename = output.artifact.filename
        job.size = output.artifact.size
        job.updated_at = finished_at
        return output.artifact
    finally:
        if not committed and staged:
            await artifact_store.discard_staged_artifacts(tuple(staged))


async def _run_export(
    *,
    job: ExportJob,
    store: CanvasStore,
    catalog: SkillCatalog,
    runtime: SkillRuntime,
    artifact_store: ArtifactStore,
    canvas_title: str | None = None,
    session_store: object | None = None,
    template_artifact_id: str | None = None,
    use_clone_render: bool = False,
    locale: str = "en-US",
) -> None:
    job.status = "running"
    job.updated_at = _now()
    await _persist(job)

    # V2.9 A6+: run the clone path separately, bypassing the skill runtime.
    if use_clone_render and job.fmt == "pptx":
        try:
            await _run_clone_render(
                job=job,
                store=store,
                artifact_store=artifact_store,
                template_artifact_id=template_artifact_id,
                locale=locale,
            )
        except Exception as e:
            logger.exception("canvas_export.clone_failed jid=%s", job.id)
            _fail(job, f"clone render failed: {type(e).__name__}: {e}")
        return

    try:
        skill_name = _FMT_SKILL[job.fmt]
        spec = catalog.get(skill_name)
        if spec is None:
            _fail(job, f"skill '{skill_name}' not installed in catalog")
            return

        canvas = await store.get_canvas(job.canvas_id)
        if canvas is None:
            _fail(job, f"canvas {job.canvas_id} not found")
            return
        sections = await store.list_sections(job.canvas_id)
        if not sections:
            _fail(job, "canvas is empty")
            return

        # i18n v1 R4: check quarantined sections (historical LLM numeric errors, off-by-N scales, etc.).
        # Any quarantined section → reject the entire export so the admin/user actively regenerates it.
        quarantined = [s for s in sections if getattr(s, "quarantined", False)]
        if quarantined:
            reasons = [f"  - section {s.id} ({s.kind}): {s.quarantine_reason or 'flagged'}" for s in quarantined[:5]]
            _fail(
                job,
                (
                    f"export rejected: {len(quarantined)} canvas section(s) are quarantined "
                    f"due to known data quality issues (e.g. off-by-N scale errors in legacy "
                    f"LLM output). Re-run analysis to regenerate or contact admin to clear "
                    f"quarantine.\n" + "\n".join(reasons)
                ),
            )
            return

        payload: dict = {
            # Display title shares its source with the filename: explicit caller title > report h1 > Canvas title > fallback.
            "title": canvas_title or report_title_from_sections(sections, canvas.title) or "Bowhead Canvas",
            "sections": [s.to_report_section() for s in _canonical_export_order(sections)],
            "locale": locale,
        }

        # i18n v1: render inline number syntax before export.
        # Design §3.5.2: export is one of the final exits; prefer the export caller's target_locale,
        # falling back to the Canvas creator's output_language (M3 temporarily defaults to zh-CN until the export
        # API accepts locale, then use that argument). Without locale, skip rendering for legacy-path compatibility.
        # citation-audit M1-6② (permanent ingress seam, ordering fixed in impl review C-P1-2):
        # Export ingress fallback for retired markers (M1-6②): bake into plain value+unit text, idempotently.
        # citation-audit M3-3: the subsequent i18n `render_inline_numbers` was retired with §4.5.
        # Its salvage step caused the loss of units while retaining values (why bake had to run first for safety);
        # retiring the entire subsystem also eliminated that hazard.
        try:
            from bowhead_core.citation_marks import bake_payload_strings

            payload = bake_payload_strings(payload)
        except Exception as _e:
            logger.warning(
                "canvas_export.bake_legacy_failed jid=%s err=%s",
                job.id,
                _e,
            )
        payload["sections"] = _static_safe_legacy_sections(
            payload.get("sections"),
            locale=locale,
        )

        # WP1: image sections: the sandbox has no network/artifact-store access; prefetch bytes into tmp
        # and inject _local_path for render scripts to embed images (the same pattern as template_path).
        # feedback-0713 F5 (B4 debt): also inject for pptx; canvas-pptx previously did not render image
        # sections at all. The script now has an image pool, but without injected bytes that support is useless.
        # Security (F5 review): pass owner=canvas.user_id as check_created_by to prevent cross-tenant
        # artifact IDOR, and strip user-controlled _local_path inside the function.
        # canvas-docx also embeds images (`doc.add_picture`); without injected bytes, image sections
        # silently degrade to alt text, the same ineffective-support pitfall pptx had.
        if job.fmt in ("pdf", "pptx", "docx"):
            await _inject_image_paths(
                payload.get("sections") or [],
                artifact_store,
                owner_user_id=getattr(canvas, "user_id", None),
            )

        # W0-B: an explicit template may read only a live PPTX owned by the Canvas owner.
        if job.fmt == "pptx" and template_artifact_id and artifact_store is not None:
            try:
                art = await _load_owned_pptx_template(
                    artifact_store=artifact_store,
                    template_artifact_id=template_artifact_id,
                    owner_user_id=canvas.user_id,
                )
                if art is None:
                    _fail(job, "template artifact missing_or_forbidden")
                    return

                import tempfile
                from pathlib import Path as _P

                tmp_dir = _P(tempfile.mkdtemp(prefix="bow-pptx-tpl-"))
                tpl_path = tmp_dir / art.filename.replace("/", "_")
                tpl_path.write_bytes(await artifact_store.open_read(art))
                payload["template_path"] = str(tpl_path.resolve())
                logger.info(
                    "canvas_export.use_explicit_template jid=%s aid=%s",
                    job.id,
                    art.id,
                )
            except Exception as e:
                logger.warning("canvas_export.explicit_template_failed jid=%s err=%s", job.id, e)
                _fail(job, "template artifact missing_or_forbidden")
                return

        # Scenario A (legacy): if the session has a PPT attachment, automatically use it as the PPTX export template (style inheritance).
        # Use this fallback only if the V2.1 explicit template did not take effect.
        if job.fmt == "pptx" and session_store is not None and "template_path" not in payload:
            try:
                aids = await session_store.list_session_attachments(  # type: ignore[attr-defined]
                    session_id=canvas.session_id,
                )
                for aid in reversed(aids):
                    art = await artifact_store.get(
                        aid,
                        check_created_by=canvas.user_id,
                        reject_null_owner=True,
                    )
                    if art is None:
                        continue
                    is_pptx = "presentationml" in (art.mime or "") or art.filename.lower().endswith(".pptx")
                    if not is_pptx:
                        continue
                    # Write the blob into tmp for the skill script to read.
                    import tempfile
                    from pathlib import Path as _P

                    tmp_dir = _P(tempfile.mkdtemp(prefix="bow-pptx-tpl-"))
                    tpl_path = tmp_dir / art.filename.replace("/", "_")
                    tpl_path.write_bytes(await artifact_store.open_read(art))
                    payload["template_path"] = str(tpl_path.resolve())
                    logger.info(
                        "canvas_export.use_template jid=%s artifact=%s path=%s",
                        job.id,
                        art.id,
                        tpl_path,
                    )
                    break
            except Exception as e:
                logger.warning("canvas_export.template_lookup_failed jid=%s err=%s", job.id, e)

        result = await runtime.run(
            spec,
            entry_script="render_from_canvas.py",
            input_data=payload,
        )
        if not result.ok:
            _fail(job, result.error or f"exit_code={result.exit_code}; stderr={result.stderr[-400:]}")
            return

        # Find the output file by the format's extension.
        out: Path | None = None
        for p in result.outputs:
            if p.suffix.lower() == f".{job.fmt}":
                out = p
                break
        if out is None:
            _fail(job, f"no .{job.fmt} produced by skill (outputs={[p.name for p in result.outputs]})")
            return

        mime = _FMT_MIME[job.fmt]
        # Share `export_naming` with the skill export path: derive filenames from the report's own h1 title.
        # canvas.title is set only when users manually rename the Canvas; automatic output always has NULL, so every file
        # previously produced through this path was named `canvas.docx` / `canvas.pptx` (observed in 116).
        filename = export_filename(report_title_from_sections(sections, canvas.title), f".{job.fmt}")
        art = await _persist_legacy_export_pair(
            job=job,
            artifact_store=artifact_store,
            output_path=out,
            filename=filename,
            mime=mime,
            canvas_id=job.canvas_id,
            created_by=canvas.user_id,
            session_id=canvas.session_id,
            engine="legacy",
        )

        job.status = "done"
        job.artifact_id = art.id
        job.manifest_artifact_id = getattr(art, "manifest_artifact_id", None)
        job.url = f"/api/v1/files/{art.id}"
        job.filename = art.filename
        job.size = art.size
        job.updated_at = _now()
        await _persist(job)
        logger.info(
            "canvas_export.done jid=%s canvas=%s fmt=%s artifact=%s size=%d",
            job.id,
            job.canvas_id,
            job.fmt,
            art.id,
            art.size,
        )
    except Exception as e:
        _fail(job, f"unexpected: {type(e).__name__}: {e}")


def _fail(job: ExportJob, error: str) -> None:
    job.status = "failed"
    job.error = error
    job.updated_at = _now()
    logger.error("canvas_export.failed jid=%s canvas=%s fmt=%s err=%s", job.id, job.canvas_id, job.fmt, error)
    _persist_nowait(job)


# ─────────────────────────────────────────────────────────────
# V2.9 A6+ #60: template-clone render path (bypasses skill runtime)
# ─────────────────────────────────────────────────────────────


async def _run_clone_render(
    *,
    job: ExportJob,
    store: CanvasStore,
    artifact_store: ArtifactStore,
    template_artifact_id: str | None,
    locale: str = "en-US",
) -> None:
    """clone 模式: 用 render_filled_pptx 把 canvas section 填进用户母版."""
    if not template_artifact_id:
        _fail(job, "clone mode 需 template_artifact_id")
        return

    canvas = await store.get_canvas(job.canvas_id)
    if canvas is None:
        _fail(job, f"canvas {job.canvas_id} not found")
        return
    sections = await store.list_sections(job.canvas_id)
    if not sections:
        _fail(job, "canvas is empty")
        return

    # W0-B: silently deny by Canvas owner before reading the user's uploaded .pptx master.
    art = await _load_owned_pptx_template(
        artifact_store=artifact_store,
        template_artifact_id=template_artifact_id,
        owner_user_id=canvas.user_id,
    )
    if art is None:
        _fail(job, "template artifact missing_or_forbidden")
        return

    import tempfile

    tpl_dir = Path(tempfile.mkdtemp(prefix="clone_tpl_"))
    tpl_path = tpl_dir / f"{template_artifact_id}.pptx"
    tpl_path.write_bytes(await artifact_store.open_read(art))

    out_dir = Path(tempfile.mkdtemp(prefix="clone_out_"))
    # naming-exempt: **temporary** sandbox working path, not the name users see;
    # the actual filename registered for the user goes through `export_filename` below.
    out_path = out_dir / f"{canvas.title or 'canvas'}.pptx"

    # Convert Canvas sections into ComposedSection.
    from bowhead_core.template_clone_workflow import ComposedSection

    composed: list[ComposedSection] = []
    safe_sections = _static_safe_legacy_sections(
        [section.to_report_section() for section in _canonical_export_order(sections)],
        locale=locale,
    )
    for rs in safe_sections:
        composed.append(
            ComposedSection(
                id=rs.get("id") or "legacy-section",
                title=rs.get("title") or "",
                chapter_tag=rs.get("chapter_tag") or "",
                kind=rs.get("kind") or "narrative",
                data=rs.get("data"),
                narrative=rs.get("narrative") or "",
                error="",
                needs_user_input=bool(rs.get("needs_user_input", False)),
            )
        )

    from bowhead_core.template_clone_render import render_filled_pptx

    fill_res = render_filled_pptx(tpl_path, composed, out_path)
    logger.info(
        "canvas_export.clone_render jid=%s filled=%d skipped=%d unmatched=%d",
        job.id,
        fill_res.slides_filled,
        fill_res.slides_skipped,
        len(fill_res.sections_unmatched),
    )

    if not out_path.exists() or out_path.stat().st_size == 0:
        _fail(job, "clone render produced empty output")
        return

    saved = await _persist_legacy_export_pair(
        job=job,
        artifact_store=artifact_store,
        output_path=out_path,
        filename=export_filename(report_title_from_sections(sections, canvas.title), ".pptx"),
        mime=_FMT_MIME["pptx"],
        canvas_id=job.canvas_id,
        created_by=canvas.user_id,
        session_id=getattr(canvas, "session_id", None),
        engine="legacy_clone",
    )
    job.status = "done"
    job.artifact_id = saved.id
    job.manifest_artifact_id = getattr(saved, "manifest_artifact_id", None)
    job.url = f"/api/v1/files/{saved.id}"
    job.filename = saved.filename
    job.size = saved.size
    job.updated_at = _now()
    await _persist(job)
