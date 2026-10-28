"""KB Worker — orchestration for parse → split → embed → chromadb add.

Tech design v3.6 §3.9. V1 fastapi BackgroundTasks (V2 升 arq).

Key fixes:
- P0-A: chunk_id 用 hash content-addressable (重 parse 同 id, 自动 upsert)
- P0-D: parse_done 前 sanity check chunks count (5% mismatch threshold)
- P0-O1: caller 仅传 file_id, worker 内部从 kb_files_store 取 KbFileDTO (含 scope/workspace_id/team_id)
- P1-L: embedder retry 内置
- P1-O7: embed_batch partial fail split + skip 单坏 chunk
- P1-Q6: parse_progress_pct state machine map (UI 进度条)
- P1-S2: _PARSE_SEMAPHORE = asyncio.Semaphore(5) 限并发
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING

from bowhead_core.kb.document import ChunkMetadata, KbChunk, KbDocument, is_parse_failure_placeholder, make_chunk_id
from bowhead_core.kb.errors import UnsupportedMimeError
from bowhead_core.kb.files_store import summary_json_with_truncation
from bowhead_core.kb.generations import KbGenerationStore, generation_writes_enabled
from bowhead_core.kb.loader.dispatch import get_loader
from bowhead_core.kb.parse_recovery import PARSE_ERROR_SOURCE_MISSING, PARSE_ERROR_TIMEOUT, PARSE_TIMEOUT_S
from bowhead_core.kb.splitter.markdown_section import make_markdown_splitter
from bowhead_core.kb.splitter.paragraph_aware import DEFAULT_CHUNK_SIZE, count_tokens, make_paragraph_splitter
from bowhead_core.kb.splitter.section_aware import section_aware_split
from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from bowhead_core.kb.embedder import Embedder
    from bowhead_core.kb.files_store import KbFilesStore
    from bowhead_core.kb.framework_inferrer import FrameworkInferrer
    from bowhead_core.kb.loader.base import LoadResult
    from bowhead_core.kb.schemas import KbFileDTO
    from bowhead_core.kb.store import KbStore


logger = logging.getLogger(__name__)


# [P1-S2] Cap concurrent parses at 5 (marker memory-heavy + dashscope rate limit)
_PARSE_SEMAPHORE = asyncio.Semaphore(5)

# PDFium can transiently fail to open a *valid* PDF under heavy concurrent
# ingest ("Data format error"); a fresh attempt succeeds. Retry once before
# giving up, but only for these transient open/load markers — don't burn a
# vision pass on genuinely scan-only / empty docs.
_PARSE_MAX_ATTEMPTS = 2
_TRANSIENT_PARSE_MARKERS = ("Failed to load document", "Data format error", "PDFium")

# [P1-Q6] Discrete parse_progress_pct mapping for UI progress-bar polling
PARSE_PROGRESS_MAP: dict[str, int | None] = {
    "pending": 0,
    "parsing": 20,
    "chunking": 50,
    "embedding": 75,
    "done": 100,
    "failed": None,
}

# P0-D sanity check threshold
COUNT_MISMATCH_CRITICAL_PCT = 0.05  # > 5% mismatch → status=failed
EMBED_BATCH_SIZE = 25
LONG_DOC_PAGE_THRESHOLD = 10  # >=10 pages use framework_inferrer
# Retry limit for rewriting under latest identity when scope keeps changing during writes (then fail; user may retry)
_WRITE_IDENTITY_ATTEMPTS = 3


def _chunked(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def _bomb_load_result(reason: str) -> LoadResult:
    """zip 炸弹 → 走 loader 家族既有的 parse_failed 契约(不 raise)。

    与 loader 的失败返回同形状, 好让 worker.py 的 `metadata['parse_failed']` 分支
    统一处理(标 failed + 把 reason 给用户看)。不设 `transient` → 不触发重试。
    """
    from bowhead_core.kb.loader.base import LoadResult

    return LoadResult(
        documents=[
            KbDocument(
                page_content=f"(rejected: {reason})",
                metadata={"page": None, "parse_failed": True},
            )
        ],
        layer_used="L0",
        duration_ms=0,
        metadata={"parse_failed": True, "reason": reason},
    )


def _index_identity(file: KbFileDTO) -> tuple[str, str, str, str]:
    """决定分块落在哪个集合、谁能检索到的四个字段。"""
    return (file.scope, file.scope_id, file.workspace_id, file.team_id)


def _is_transient_load_failure(load_result) -> bool:
    """True if the loader reported a *transient* parse failure worth retrying.

    Prefers the structured ``metadata['transient']`` flag the loader sets on a
    PDFium open/load error; falls back to matching known transient markers in
    the free-text reason for loaders that don't set the flag yet (so the retry
    isn't silently coupled to exact exception wording)."""
    md = load_result.metadata
    if not md.get("parse_failed"):
        return False
    if md.get("transient"):
        return True
    reason = str(md.get("reason", ""))
    return any(m in reason for m in _TRANSIENT_PARSE_MARKERS)


_PARSE_AUTHORIZER: ContextVar[Callable | None] = ContextVar("kb_parse_authorizer", default=None)
_PARSE_OWNER: ContextVar[tuple[str, int, int] | None] = ContextVar("kb_parse_owner", default=None)


class KbWorker:
    """Orchestrates parse + index. DI: caller 注入 stores + embedder + inferrer (optional)."""

    def __init__(
        self,
        files_store: KbFilesStore,
        store: KbStore,
        embedder: Embedder,
        framework_inferrer: FrameworkInferrer | None = None,
        artifact_fetcher: object | None = None,
        vision_client: object | None = None,
        doc_type_store: object | None = None,
        extracted_record_store: object | None = None,
        structured_extractor: object | None = None,
        doc_type_classifier: object | None = None,
        # Sprint 25 kb-per-row-extract: cardinality=many doc_type per-row LLM extract
        extracted_rows_store: object | None = None,
        per_row_jobs_store: object | None = None,
        row_extractor: object | None = None,
        kb_model_registry: object | None = None,  # for resolving extractor_model catalog name → ModelSpec
        active_per_row_tasks_ref: dict | None = None,  # app.state dict for strong-ref task tracking
        ingestion_gate: object | None = None,
    ) -> None:
        self._files = files_store
        self._store = store
        self._embedder = embedder
        self._inferrer = framework_inferrer
        # artifact_fetcher: object with `async def fetch(artifact_id) -> str` (returns local path)
        # V1 caller injects artifact_store wrapper; tests use lambda
        self._artifact_fetcher = artifact_fetcher
        # image-extraction sprint: vision_client (qwen3-vl-plus) for image MIMEs +
        # PDF L2 scan-only fallback. None → ImageLoader/PdfLoader graceful placeholder.
        self._vision_client = vision_client
        # kb-knowledge-tables sprint v1.0: doc_type + record stores + structured_extractor.
        # File upload with doc_type_id → after parse, extractor extracts fields → persist extracted_record.
        # Any dependency None → skip structured extraction (chunks already OK).
        self._doc_type_store = doc_type_store
        self._extracted_record_store = extracted_record_store
        self._structured_extractor = structured_extractor
        # kb-structured-tables Sprint 2 Phase B: classifier (sample_keywords fast path
        # + LLM slow path). When file.doc_type_id is None AND classifier injected,
        # worker auto-resolves doc_type via classify(workspace_id, chunk_sample) and
        # writes to file.auto_classified_doc_type_id (NOT doc_type_id — rollback safe).
        self._doc_type_classifier = doc_type_classifier
        # Sprint 25 kb-per-row-extract DI (None values → cardinality=many extraction skipped)
        self._extracted_rows_store = extracted_rows_store
        self._per_row_jobs_store = per_row_jobs_store
        self._row_extractor = row_extractor
        self._kb_model_registry = kb_model_registry
        # app.state strong task ref (same Sprint 2 backfill pattern)
        self._active_per_row_tasks_ref = active_per_row_tasks_ref
        self._ingestion_gate = ingestion_gate

    async def parse_and_index(self, file_id: str, *, allow_terminal: bool = False, authorize: Callable[..., Awaitable[bool]] | None = None) -> None:
        auth_token = _PARSE_AUTHORIZER.set(authorize)
        token = _PARSE_OWNER.set(None)
        try:
            await self._parse_and_index_scoped(file_id, allow_terminal=allow_terminal)
        finally:
            _PARSE_OWNER.reset(token)
            _PARSE_AUTHORIZER.reset(auth_token)

    async def _parse_and_index_scoped(self, file_id: str, *, allow_terminal: bool = False) -> None:
        """[P1-S2] semaphore-wrapped entry point. 限并发 5 个.

        FR1: 进入信号量后先原子领取（files_store.claim_parse_start），领不到就放弃 ⇒
        同一文件被重复入队（重试连点、启动恢复与上传撞车）也不会并行解析。
        ``allow_terminal`` 仅给对 done/failed 文件的显式强制重解析。
        """
        file = await self._files.get(file_id)
        if file is None:
            logger.warning("parse_and_index: file_id=%s not found", file_id)
            return
        if generation_writes_enabled() or getattr(file, "active_generation_id", None):
            from datetime import UTC, datetime, timedelta

            generations = KbGenerationStore(self._files._db_path)
            before = (datetime.now(UTC) - timedelta(days=1)).isoformat()
            try:
                for generation_id in await generations.claim_orphans(file, index_version=self._store.index_version, before=before):
                    await self._store.delete_generation(file, generation_id)
                    await generations.forget_orphan(generation_id)
            except Exception:
                logger.warning("kb generation orphan cleanup deferred file=%s", file_id, exc_info=True)
        gate = self._ingestion_gate
        ingestion = getattr(gate, "ingestion", None)
        if callable(ingestion):
            async with ingestion(file.team_id), _PARSE_SEMAPHORE:
                await self._parse_and_index_inner(file_id, allow_terminal=allow_terminal)
            return
        async with _PARSE_SEMAPHORE:
            await self._parse_and_index_inner(file_id, allow_terminal=allow_terminal)

    async def reindex_file_chunks(self, file_id: str, expected_file_version: int, *, source_store: KbStore | None = None) -> bool:
        """Build target-index chunks without mutating source-file parse state.

        Returns ``False`` when the file changed while it was being processed so
        the caller can retry the new version in its delta pass.
        """
        file = await self._files.get(file_id)
        if file is None or file.soft_deleted_at or file.version != expected_file_version:
            return False
        if getattr(file, "active_generation_id", None):
            # Embedding changes copy the published text and IDs verbatim. Re-running
            # a non-deterministic parser under the same generation would forge history.
            if source_store is None or source_store is self._store:
                raise RuntimeError("published generation requires a separate source index")
            generation = await KbGenerationStore(self._files._db_path).get(file.active_generation_id)
            if not generation or not await source_store.verify_generation(file, file.active_generation_id, generation["expected_chunk_count"]):
                raise RuntimeError("published source index unavailable")
            rows = await source_store.list_active_chunks(file)
            docs = [KbDocument(page_content=content, metadata=metadata) for _, content, metadata in rows]
            embeddings = await self._embed_all(docs)
            from dataclasses import fields

            names = {field.name for field in fields(ChunkMetadata)}
            chunks = [
                KbChunk(
                    chunk_id=cid,
                    file_id=file_id,
                    content=content,
                    embedding=embedding,
                    metadata=ChunkMetadata(**{k: v for k, v in metadata.items() if k in names}),
                )
                for (cid, content, metadata), embedding in zip(rows, embeddings, strict=True)
            ]
            current = await self._files.get(file_id)
            if not current or current.soft_deleted_at or current.version != expected_file_version:
                return False
            await self._store.add_chunks(file, chunks)
            if not await self._store.verify_generation(file, file.active_generation_id, len(chunks)):
                raise RuntimeError("target generation verification failed")
            latest = await self._files.get(file_id)
            return bool(latest and not latest.soft_deleted_at and latest.version == expected_file_version)
        try:
            loader = get_loader(file.mime_type, vision_client=self._vision_client)
        except UnsupportedMimeError as exc:
            raise RuntimeError(f"unsupported mime: {exc.mime_type}") from exc

        load_result = await self._load_with_retry(file, file_id, loader)
        if load_result.metadata.get("parse_failed"):
            reason = str(load_result.metadata.get("reason", "parse_failed"))
            raise RuntimeError(f"parse_failed: {reason}")
        docs = await self._split(file, load_result)
        if not docs:
            raise RuntimeError("no chunks after split (empty file?)")
        embeddings = await self._embed_all(docs)

        refreshed = await self._files.get(file_id)
        if refreshed is None or refreshed.soft_deleted_at or refreshed.version != expected_file_version:
            return False
        chunks = self._build_kb_chunks(refreshed, docs, embeddings)
        await self._store.hard_delete_file(refreshed)
        await self._store.add_chunks(refreshed, chunks)
        actual = await self._store.count_chunks(refreshed)
        if actual != len(chunks):
            raise RuntimeError(f"chunks count mismatch: expected {len(chunks)}, actual {actual}")

        latest = await self._files.get(file_id)
        return bool(latest is not None and not latest.soft_deleted_at and latest.version == expected_file_version)

    async def parse_and_index_many(self, file_ids: list[str]) -> None:
        """Fan out parse_and_index 并发跑 (bundle ingest 用).

        每个 file 经 parse_and_index 走 _PARSE_SEMAPHORE → 实际并发上限 5.
        关键: bundle confirm 必须一次性把所有 child 交给本方法 (单个 BackgroundTask),
        而不是逐个 background_tasks.add_task —— 后者被 FastAPI 顺序 await 串行化,
        让 Semaphore(5) 形同虚设, 一个慢扫描 PDF 就堵住整包.
        return_exceptions=True: 单文件失败不取消其余 (parse_and_index 内部已自捕获并标 failed).
        """
        await asyncio.gather(
            *(self.parse_and_index(fid) for fid in file_ids),
            return_exceptions=True,
        )

    async def _parse_and_index_inner(self, file_id: str, *, allow_terminal: bool = False) -> None:
        """Background worker: load → split → embed → chromadb. Updates kb_files.parse_status."""
        file = await self._files.get(file_id)
        if file is None:
            logger.warning("parse_and_index: file_id=%s not found", file_id)
            return
        authorize = _PARSE_AUTHORIZER.get()
        if authorize is not None and not await authorize(file):
            return
        if not await self._files.claim_parse_start(file_id, allow_terminal=allow_terminal):
            logger.info("parse_and_index: file_id=%s not claimable (running, finished or deleted)", file_id)
            return

        file = await self._files.get(file_id)
        if file is None or file.soft_deleted_at:
            return

        if generation_writes_enabled() or getattr(file, "active_generation_id", None):
            _PARSE_OWNER.set((file_id, file.version, file.parse_attempts))
        try:
            if authorize is not None and not await authorize(file):
                raise PermissionError("rebuild access changed")
            emit_audit("kb.parse_started", {"file_id": file_id})
            # FR1: entire claim→done phase bounded by PARSE_TIMEOUT_S; post-done structured extraction excluded
            # (file already usable; extraction fails softly itself).
            try:
                async with asyncio.timeout(PARSE_TIMEOUT_S) as deadline:
                    load_result = await self._parse_until_done(file_id, file)
            except TimeoutError:
                if not deadline.expired():
                    raise
                await self._fail_parse(
                    file_id,
                    f"{PARSE_ERROR_TIMEOUT}: exceeded {PARSE_TIMEOUT_S}s",
                    audit_error=PARSE_ERROR_TIMEOUT,
                )
                return
            if load_result is None or _PARSE_AUTHORIZER.get() is not None:
                return
            # Writes used reread file row (scope may change during parse); structured extraction follows same row.
            file = await self._files.get(file_id) or file

            # kb-knowledge-tables sprint v1.0 §6 + kb-structured-tables Sprint 2 Phase B:
            # structured extraction. Two paths gated by whether file has explicit
            # doc_type_id (user-bound) or not (Sprint 2 classifier auto-resolves).
            if self._has_extraction_di():
                if file.doc_type_id:
                    # Sprint 1 explicit-bind path (unchanged): user pre-selected doc_type.
                    await self._run_structured_extraction(file, load_result)
                elif self._doc_type_classifier is not None:
                    # Sprint 2 auto-classify path: classifier resolves doc_type from
                    # chunk sample, writes auto_classified_doc_type_id (NOT doc_type_id).
                    await self._run_auto_classify_and_extract(file, load_result)
        except Exception as exc:
            logger.exception("kb worker failed file_id=%s", file_id)
            await self._fail_parse(file_id, str(exc))

    async def _fail_parse(self, file_id: str, parse_error: str, *, audit_error: str | None = None) -> None:
        """标 failed，并清掉该文件已写入的分块：失败的文件不应仍可被检索。

        覆盖「分块已写入后才判失败」（数量核对不符、超时、写入后异常）与强制重解析失败
        （旧分块已在写入前删除，或本次加载即失败）。清理 best-effort，失败只记日志。
        """
        await self._update_status(file_id, "failed", parse_error=parse_error)
        emit_audit("kb.parse_failed", {"file_id": file_id, "error": audit_error or parse_error})
        current = await self._files.get(file_id)
        if not generation_writes_enabled() and not (current and current.active_generation_id):
            await self._purge_file_chunks(file_id)

    async def _purge_file_chunks(self, file_id: str) -> None:
        try:
            current = await self._files.get(file_id)
            if current is not None:
                await self._store.hard_delete_file(current)
        except Exception as exc:
            logger.warning("kb worker: purge chunks failed file_id=%s err=%s", file_id, exc)

    async def _write_chunks_for_current_row(
        self,
        file_id: str,
        expected_version: int,
        docs: list[KbDocument],
        embeddings: list[list[float]],
    ) -> tuple[KbFileDTO, list[KbChunk]] | None:
        """写入前重读文件行，按当前身份（范围/空间/团队）写分块。

        文件行在领取前只读过一次，解析期间可能被删除或改了范围：
        - 已删除 → 不写，标 failed（如写入窗口内被删，删掉刚写入的分块）。
        - version 变了 → 另一次解析已完成并以它为准，不覆盖、不改状态。
        - 范围变了 → 用新身份写。范围变更路由只迁移当时已存在的分块，解析中的文件内容
          不变、新范围才是权威，因此按新范围写是正确语义；用旧身份写会让「空间→个人」
          后内容仍对空间可见。写入后再核一次，身份又变则删掉本次写入并按最新身份重写
          （chunk_id 内容寻址，重写幂等）。
        - 写入前先硬删该文件的全部旧分块：重新解析的输出可能不同，chunk_id 随内容变，
          只 upsert 会留下仍处于 active 的陈旧分块。

        返回 (写入所用的文件行, 分块)；不写入时返回 None（状态已处理）。
        """
        for _ in range(_WRITE_IDENTITY_ATTEMPTS):
            current = await self._files.get(file_id)
            if current is None or current.soft_deleted_at:
                await self._abort_deleted_during_parse(file_id, current)
                return None
            if current.version != expected_version:
                emit_audit("kb.parse_superseded", {"file_id": file_id, "expected_version": expected_version, "version": current.version})
                logger.info("parse_and_index: file_id=%s superseded by version %s, not writing", file_id, current.version)
                return None
            kb_chunks = self._build_kb_chunks(current, docs, embeddings)
            await self._store.hard_delete_file(current)
            await self._store.add_chunks(current, kb_chunks)

            latest = await self._files.get(file_id)
            if latest is None or latest.soft_deleted_at:
                await self._store.hard_delete_file(current)
                await self._abort_deleted_during_parse(file_id, latest)
                return None
            if _index_identity(latest) == _index_identity(current):
                return current, kb_chunks
            # Scope changed again during write window: delete chunks written under old identity; rewrite with latest.
            await self._store.hard_delete_file(current)
        raise RuntimeError("file scope kept changing while indexing; retry the parse")

    async def _abort_deleted_during_parse(self, file_id: str, file: KbFileDTO | None) -> None:
        emit_audit("kb.parse_aborted_file_deleted", {"file_id": file_id})
        logger.info("parse_and_index: file_id=%s deleted during parse, chunks not kept", file_id)
        if file is not None:
            await self._update_status(file_id, "failed", parse_error="aborted: file deleted during parsing")

    async def _publish_generation(
        self, file: KbFileDTO, docs: list[KbDocument], embeddings: list[list[float]], load_result: LoadResult, duration_ms: int
    ) -> bool:
        """Write a new namespace of IDs, verify it, then atomically publish."""
        current = await self._files.get(file.file_id)
        if current is None or current.soft_deleted_at or current.version != file.version or current.parse_attempts != file.parse_attempts:
            return False
        if current.artifact_id != file.artifact_id:
            raise RuntimeError("source changed during parse")
        authorize = _PARSE_AUTHORIZER.get()
        if authorize is not None and not await authorize(current):
            raise PermissionError("rebuild authorization changed")
        sha256 = load_result.metadata.get("kb_source_sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise RuntimeError("complete source fingerprint unavailable")
        generations = KbGenerationStore(self._files._db_path)
        generation_id = await generations.begin(current, sha256, len(docs), self._store.index_version)
        chunks = self._build_kb_chunks(current, docs, embeddings)
        for chunk in chunks:
            chunk.chunk_id = make_chunk_id(generation_id, chunk.metadata.chunk_index, chunk.content)
            chunk.metadata.generation_id = generation_id
        await self._store.add_chunks(current, chunks)
        if not await self._store.verify_generation(current, generation_id, len(chunks)):
            raise RuntimeError("generation verification failed")
        if authorize is not None and not await authorize(await self._files.get(file.file_id)):
            await self._store.delete_generation(current, generation_id)
            raise PermissionError("rebuild authorization changed")
        published = await generations.publish(
            current,
            generation_id,
            layer=load_result.layer_used,
            duration_ms=duration_ms,
            summary_json=summary_json_with_truncation(current.summary_json, load_result.metadata.get("truncated_rows")),
        )
        if not published:
            await self._store.delete_generation(current, generation_id)
        elif load_result.metadata.get("kb_source_identity"):
            latest = await self._files.get(file.file_id)
            if latest and latest.active_generation_id == generation_id:
                await generations.bind_source(latest, load_result.metadata["kb_source_identity"], sha256)
        return published

    async def _parse_until_done(self, file_id: str, file) -> LoadResult | None:
        """load → split → embed → chromadb → done. Returns the load result on
        ``done`` (for structured extraction), None after writing a ``failed`` state."""
        t0 = time.time()

        # ── load (artifact_fetcher may be None—tests use file.artifact_id directly as path) ──
        try:
            loader = get_loader(
                file.mime_type,
                vision_client=self._vision_client,
            )
            # kb-knowledge-tables v1.0: doc_type semantics changed (extract_schema replaces columns/chunk_strategy).
            # XlsxLoader uses default per_island strategy; structured field extraction uses new worker path (Day 4 integration).
        except UnsupportedMimeError as exc:
            await self._fail_parse(file_id, f"unsupported mime: {exc.mime_type}", audit_error=str(exc))
            return None

        load_result = await self._load_with_retry(file, file_id, loader)

        # Loader signalled a genuine parse failure (e.g. PDF that PDFium can't
        # open / scan-only with no/failed vision). It returns a placeholder
        # doc so callers don't crash, but persisting it as `done` silently
        # hides an unsearchable file. Mark `failed` with the real reason so
        # the user sees it (and can re-upload / backfill recovers it).
        if load_result.metadata.get("parse_failed"):
            reason = str(load_result.metadata.get("reason", "parse_failed"))
            await self._fail_parse(file_id, f"parse_failed: {reason}", audit_error=reason)
            return None

        # ── split (chunking) ──
        await self._update_status(file_id, "chunking")
        docs = await self._split(file, load_result)
        if not docs:
            await self._fail_parse(file_id, "no chunks after split (empty file?)", audit_error="no chunks")
            return None

        authorize = _PARSE_AUTHORIZER.get()
        if authorize is not None and not await authorize(await self._files.get(file_id)):
            raise PermissionError("rebuild access changed")

        # ── embed (batch 25, built-in retry, P1-O7 partial split) ──
        await self._update_status(file_id, "embedding")
        embeddings = await self._embed_all(docs)

        if generation_writes_enabled() or getattr(file, "active_generation_id", None):
            published = await self._publish_generation(file, docs, embeddings, load_result, int((time.time() - t0) * 1000))
            return load_result if published else None

        # ── add to chromadb (reread file row before writing; see _write_chunks_for_current_row) ──
        written = await self._write_chunks_for_current_row(file_id, file.version, docs, embeddings)
        if written is None:
            return None
        target, kb_chunks = written

        # P0-D sanity check
        actual = await self._store.count_chunks(target)
        expected = len(kb_chunks)
        mismatch_pct = abs(actual - expected) / max(expected, 1)
        if mismatch_pct > COUNT_MISMATCH_CRITICAL_PCT:
            emit_audit(
                "kb.parse_count_mismatch_critical",
                {
                    "file_id": file_id,
                    "expected": expected,
                    "actual": actual,
                },
            )
            await self._fail_parse(file_id, f"chunks count mismatch >5%: expected {expected}, actual {actual}")
            return None
        if 0 < mismatch_pct <= COUNT_MISMATCH_CRITICAL_PCT:
            emit_audit(
                "kb.parse_count_mismatch_minor",
                {
                    "file_id": file_id,
                    "expected": expected,
                    "actual": actual,
                },
            )

        duration_ms = int((time.time() - t0) * 1000)
        await self._update_status(
            file_id,
            "done",
            parse_layer=load_result.layer_used,
            parse_duration_ms=duration_ms,
            chunks_count=actual,
            # Honestly record table indexing row-cap truncation (file details); clear old value on untruncated reparse
            summary_json=summary_json_with_truncation(target.summary_json, load_result.metadata.get("truncated_rows")),
        )
        emit_audit(
            "kb.parse_done",
            {
                "file_id": file_id,
                "layer": load_result.layer_used,
                "duration_ms": duration_ms,
                "chunks": actual,
            },
        )
        return load_result

    def _has_extraction_di(self) -> bool:
        """Sprint 2 Phase B helper: all 3 stores + extractor required for extract."""
        return self._structured_extractor is not None and self._extracted_record_store is not None and self._doc_type_store is not None

    def _has_per_row_di(self) -> bool:
        """Sprint 25 Phase C helper: per-row extraction DI all wired."""
        return (
            self._row_extractor is not None
            and self._extracted_rows_store is not None
            and self._per_row_jobs_store is not None
            and self._doc_type_store is not None
            and self._kb_model_registry is not None
        )

    def _build_sample_text(self, load_result) -> str:
        """Sprint 2 Phase B: first 1500 chars of concat'd page_content for classifier.

        Classifier internally sanitizes + caps further — this is just to avoid
        copying huge documents through memory.
        """
        full = "\n\n".join(d.page_content for d in load_result.documents if d.page_content)
        return full[:1500]

    async def _run_auto_classify_and_extract(self, file, load_result) -> None:
        """Sprint 2 Phase B: classifier → auto-bind doc_type → extract.

        Soft-fail throughout — classifier failures never propagate to worker.
        Writes to `file.auto_classified_doc_type_id` (NEW Sprint 2 col), NOT
        `file.doc_type_id`, so 0.40.5 image revert is rollback-safe.
        """
        sample = self._build_sample_text(load_result)
        try:
            result = await self._doc_type_classifier.classify(
                workspace_id=file.workspace_id,
                chunk_sample=sample,
            )
        except Exception as e:
            logger.warning(
                "classifier raised (unexpected, should soft-fail internally) file_id=%s err=%s",
                file.file_id,
                type(e).__name__,
            )
            emit_audit(
                "kb.classify_failed",
                {
                    "file_id": file.file_id,
                    "workspace_id": file.workspace_id,
                    "error_code": "UNHANDLED",
                },
            )
            return
        if result is None:
            # Classifier already emitted appropriate no-match / low-conf / failed audit.
            # Worker just appends file_id for diagnostics.
            emit_audit(
                "kb.classify_no_match",
                {
                    "file_id": file.file_id,
                    "workspace_id": file.workspace_id,
                },
            )
            return

        # Write auto_classified_doc_type_id with cross-ws guard (I-B6).
        ws_ok = await self._files.update_auto_classified_doc_type(
            file.file_id,
            result.doc_type_id,
            expected_workspace_id=file.workspace_id,
        )
        if not ws_ok:
            # Cross-ws / missing-file / soft-deleted → reject silently.
            emit_audit(
                "kb.classify_invalid_name",
                {
                    "file_id": file.file_id,
                    "workspace_id": file.workspace_id,
                    "reason": "ws_guard_or_missing",
                },
            )
            return

        # Sprint 2 → Sprint 25 dispatch gate.
        # cardinality=one + auto_extract=true → file-level extract (Sprint 1+2 path, unchanged).
        # cardinality=many + auto_extract=true → row-level extract (Sprint 25 NEW).
        # auto_extract=false → bind only, no extract.
        if not result.auto_extract_enabled:
            emit_audit(
                "kb.classify_skipped_disabled",
                {
                    "file_id": file.file_id,
                    "doc_type_id": result.doc_type_id,
                },
            )
            return
        if result.cardinality == "many":
            # Sprint 25 per-row extraction path
            if not self._has_per_row_di():
                # Per-row DI not wired (lifespan didn't inject row_extractor /
                # extracted_rows_store / per_row_jobs_store). Skip per-row,
                # log audit so prod operator knows config gap.
                emit_audit(
                    "kb.classify_skipped_per_row_v1",
                    {
                        "file_id": file.file_id,
                        "doc_type_id": result.doc_type_id,
                        "reason": "per_row_di_missing",
                    },
                )
                return
            await self._dispatch_per_row_extraction(
                file=file,
                doc_type_id=result.doc_type_id,
                load_result=load_result,
            )
            return
        # cardinality == "one" — file-level path falls through to _run_structured_extraction

        # Extract. Reuse Sprint 1 method with auto_classified=True so the record
        # is created with verify_status="needs_review" (P0-PM-2: classifier output
        # needs human approval before chat treats it as authoritative).
        try:
            await self._run_structured_extraction(
                file,
                load_result,
                doc_type_id_override=result.doc_type_id,
                auto_classified=True,
            )
        except Exception as e:
            # Defense in depth: clear auto column so a retry / next worker run
            # gets a clean slate (no half-poisoned state).
            logger.warning(
                "auto-classify extract failed file_id=%s err=%s; clearing auto col",
                file.file_id,
                type(e).__name__,
            )
            with contextlib.suppress(Exception):
                await self._files.update_auto_classified_doc_type(
                    file.file_id,
                    None,
                    expected_workspace_id=file.workspace_id,
                )
            emit_audit(
                "kb.extract_failed_classified",
                {
                    "file_id": file.file_id,
                    "doc_type_id": result.doc_type_id,
                    "error_code": type(e).__name__,
                },
            )

    async def _run_structured_extraction(
        self,
        file,
        load_result,
        *,
        doc_type_id_override: str | None = None,
        auto_classified: bool = False,
    ) -> None:
        """Run StructuredExtractor → save 1 KbExtractedRecord per file.

        Failures here don't fail the parse — chunks are already indexed, this is bonus.
        Audit emit on both success & fail so observability picks issues.

        Sprint 2 Phase B (P2-ENG-1 refactor):
          - ``doc_type_id_override``: if set, use this doc_type_id instead of
            file.doc_type_id (used by _run_auto_classify_and_extract for classifier
            path — file.doc_type_id stays NULL, classifier resolution is in
            auto_classified_doc_type_id).
          - ``auto_classified=True``: forces verify_status="needs_review" regardless
            of extractor confidence (P0-PM-2: classifier output requires explicit
            user approval before chat treats it as authoritative).
        """
        # Resolve doc_type_id: classifier override > explicit user bind > auto col fallback.
        doc_type_id = doc_type_id_override or file.doc_type_id or getattr(file, "auto_classified_doc_type_id", None)
        # kb-matrix M4 gate③ (decision 12): auto_extract=off upgraded to "this table accepts
        # no new extraction"—frozen snapshots/column templates are not extraction targets (re-extraction writes ghost records,
        # splitting the two data views / soft_delete_for_attempt silently soft-deletes frozen rows). Previously off checked only
        # auto-classification and explicit bindings bypassed it; additive tightening for existing behavior (registered).
        if doc_type_id and self._doc_type_store is not None:
            _dt_gate = await self._doc_type_store.get(doc_type_id)
            if _dt_gate is not None and not _dt_gate.auto_extract_enabled:
                emit_audit(
                    "kb.classify_skipped_disabled",
                    {"file_id": file.file_id, "doc_type_id": doc_type_id, "path": "structured_extraction_entry"},
                )
                return
        if not doc_type_id:
            emit_audit(
                "kb.extract_skipped",
                {
                    "file_id": file.file_id,
                    "reason": "no doc_type bound",
                },
            )
            return
        try:
            doc_type = await self._doc_type_store.get(doc_type_id)
            if doc_type is None:
                emit_audit(
                    "kb.extract_skipped",
                    {
                        "file_id": file.file_id,
                        "doc_type_id": doc_type_id,
                        "reason": "doc_type not found / soft-deleted",
                    },
                )
                return
            if not doc_type.fields:
                emit_audit(
                    "kb.extract_skipped",
                    {
                        "file_id": file.file_id,
                        "doc_type_id": doc_type_id,
                        "reason": "doc_type has no fields",
                    },
                )
                return
            # Concatenate loader-produced text (multi-page PDF / multi-sheet xlsx both OK).
            full_text = "\n\n".join(d.page_content for d in load_result.documents if d.page_content)
            if not full_text.strip():
                emit_audit(
                    "kb.extract_skipped",
                    {
                        "file_id": file.file_id,
                        "doc_type_id": doc_type_id,
                        "reason": "empty text",
                    },
                )
                return
            result = await self._structured_extractor.extract(
                text=full_text,
                fields=doc_type.fields,
                extractor_model=doc_type.extractor_model,
                cross_verify_enabled=doc_type.cross_verify_enabled,
            )
            # Provenance path B: backfill each field's chunk_id by fuzzy-matching its
            # source_snippet against the file's indexed chunks (best-effort — the
            # extractor works on concatenated full_text and has no chunk_id itself).
            # Failure never blocks the record write (chunk_id stays None).
            try:
                from bowhead_core.kb.structured_extractor import backfill_chunk_ids

                file_chunks = await self._store.get_chunks_for_file(file)
                bound = backfill_chunk_ids(result.fields, file_chunks)
                if bound:
                    emit_audit("kb.chunk_id_backfilled", {"file_id": file.file_id, "fields_bound": bound})
            except Exception:
                logger.warning("chunk_id backfill failed file_id=%s", file.file_id, exc_info=True)
            # Sprint 2 P0-PM-2: classifier-resolved records default needs_review
            # so user sees "(自动抽取, 未人工确认)" in chat answers + must approve.
            verify_status = "needs_review" if auto_classified else "needs_review" if result.has_low_confidence_fields else "ready"
            # kb-v2 §A.7: pass doc_type.fields so currency/number fields wrap
            # as {amount, currency} instead of degraded {value: str}. Snapshot
            # at write time — acceptable staleness window for Sprint 1 (per
            # IntelliVault audit, Sprint 2 may revisit with write-time refetch).
            try:
                record_id = await self._extracted_record_store.create(
                    workspace_id=file.workspace_id,
                    doc_type_id=doc_type.doc_type_id,
                    file_id=file.file_id,
                    fields=result.fields,
                    extractor_model=result.extractor_model,
                    extraction_duration_ms=result.extraction_duration_ms,
                    verify_status=verify_status,
                    created_by=file.uploaded_by,
                    doc_type_fields=doc_type.fields,
                    dedup_check=True,
                )
            except Exception as create_exc:
                # I-B4: RecordAlreadyExistsError on duplicate (re-upload / backfill
                # rerun). Treat as benign skip — audit + return.
                from bowhead_core.kb.extracted_record_store import (
                    RecordAlreadyExistsError,
                )

                if isinstance(create_exc, RecordAlreadyExistsError):
                    emit_audit(
                        "kb.extract_duplicate_skipped",
                        {
                            "file_id": file.file_id,
                            "doc_type_id": doc_type.doc_type_id,
                            "existing_record_id": create_exc.existing_record_id,
                        },
                    )
                    return
                raise
            await self._doc_type_store.update_record_count(
                doc_type.doc_type_id,
                +1,
            )
            emit_audit(
                "kb.extract_done",
                {
                    "file_id": file.file_id,
                    "doc_type_id": doc_type.doc_type_id,
                    "record_id": record_id,
                    "fields_extracted": len(result.fields),
                    "duration_ms": result.extraction_duration_ms,
                    "chunks_processed": result.chunks_processed,
                    "self_consistency_runs": result.self_consistency_runs,
                    "verify_status": verify_status,
                    "auto_classified": auto_classified,
                },
            )
        except Exception as e:
            logger.warning(
                "kb worker structured extraction failed file_id=%s err=%s",
                file.file_id,
                e,
            )
            emit_audit(
                "kb.extract_failed",
                {
                    "file_id": file.file_id,
                    "error": str(e),
                },
            )

    # ─── Sprint 25 kb-per-row-extract: cardinality=many path ────────

    async def _dispatch_per_row_extraction(
        self,
        *,
        file,
        doc_type_id: str,
        load_result,
    ) -> None:
        """Sprint 25 cardinality=many dispatch.

        Pre-writes queued state to BOTH kb_files.extracted_rows_status (P0-UX-3:
        FE pill shows queued immediately) + kb_per_row_jobs.create_job. Then
        asyncio.create_task spawns detached background task — `parse_and_index`
        returns immediately, _PARSE_SEMAPHORE released (P0-ENG-2 真 detached).
        """
        # 1. Pre-write queued state (P0-UX-3 immediate FE visibility)
        try:
            await self._files.update_extracted_rows_status(
                file.file_id,
                "queued",
                expected_workspace_id=file.workspace_id,
            )
        except Exception:
            logger.warning("update_extracted_rows_status failed (degraded)")

        job_id = await self._per_row_jobs_store.create_job(
            file_id=file.file_id,
            doc_type_id=doc_type_id,
            workspace_id=file.workspace_id,
            status="queued",
            attempt_id=1,
        )

        emit_audit(
            "kb.row_extract_dispatched",
            {
                "file_id": file.file_id,
                "doc_type_id": doc_type_id,
                "workspace_id": file.workspace_id,
                "job_id": job_id,
            },
        )

        # 2. Write auto col (same cardinality=one path, classify_invalid_name fallback)
        with contextlib.suppress(Exception):
            await self._files.update_auto_classified_doc_type(
                file.file_id,
                doc_type_id,
                expected_workspace_id=file.workspace_id,
            )

        # 3. Spawn detached task with strong-ref (P0-ENG-2 + Eng-R2-1 dict pattern)
        # The actual extraction MAY take 5-30 min for large file — must run
        # detached so parse_and_index returns + _PARSE_SEMAPHORE releases.
        import asyncio as _asyncio

        task = _asyncio.create_task(
            self._run_per_row_extraction(
                file=file,
                doc_type_id=doc_type_id,
                job_id=job_id,
                attempt_id=1,
            ),
            name=f"kb_row_extract:{file.file_id}",
        )
        # Strong-ref via app.state dict (same Sprint 2 backfill pattern)
        if self._active_per_row_tasks_ref is not None:
            self._active_per_row_tasks_ref[file.file_id] = task
            task.add_done_callback(
                lambda _t: self._active_per_row_tasks_ref.pop(file.file_id, None),
            )

    async def _run_per_row_extraction(
        self,
        *,
        file,
        doc_type_id: str,
        job_id: str,
        attempt_id: int = 1,
        start_batch_idx: int = 0,
    ) -> None:
        """Sprint 25 actual extraction — detached task entry point.

        NO _PARSE_SEMAPHORE held (released by parse_and_index caller upon
        dispatch). Failures don't propagate — instead mark job
        failed/failed_retry + audit emit.

        On success: update_row_count(+N) + update_extracted_rows_status('succeeded')
        + mark_status('succeeded') + emit completion audit.
        """
        # Lazy import to avoid cycle (row_extractor imports from extracted_rows_store)
        from bowhead_core.kb.per_row_jobs_store import (
            CheckpointStateWrite,
        )
        from bowhead_core.kb.row_extractor import (
            RowExtractionError,
            TokenOverflowError,
        )

        file_path: str | None = None
        try:
            # Mark running
            await self._per_row_jobs_store.mark_status(job_id, "running")
            await self._files.update_extracted_rows_status(
                file.file_id,
                "running",
                expected_workspace_id=file.workspace_id,
            )
            emit_audit(
                "kb.row_extract_started",
                {
                    "file_id": file.file_id,
                    "doc_type_id": doc_type_id,
                    "job_id": job_id,
                },
            )

            # Resolve doc_type for fields + dedup_key_fields + extractor_model
            doc_type = await self._doc_type_store.get(doc_type_id)
            if doc_type is None or not doc_type.fields:
                await self._per_row_jobs_store.mark_status(
                    job_id,
                    "failed",
                    error_code="PARSE_FAILED",
                    error_hint="doc_type_missing_or_no_fields",
                )
                await self._files.update_extracted_rows_status(
                    file.file_id,
                    "failed",
                    expected_workspace_id=file.workspace_id,
                )
                return

            dedup_key_fields = doc_type.dedup_key_fields or [f.name for f in doc_type.fields if f.required]
            if not dedup_key_fields:
                dedup_key_fields = [doc_type.fields[0].name]  # last-resort fallback

            # Resolve ModelSpec from catalog name (per doc_type extractor_model
            # OR active.row_extractor default)
            model_catalog_name = doc_type.extractor_model or "qwen3.6-flash-row"
            try:
                if hasattr(self._kb_model_registry, "get_by_name"):
                    spec = self._kb_model_registry.get_by_name(model_catalog_name)
                    if spec.capability != "row_extractor":
                        # catalog name not a row_extractor — fallback to active
                        spec = self._kb_model_registry.get_active("row_extractor")
                else:
                    spec = self._kb_model_registry.get_active("row_extractor")
            except Exception as exc:
                logger.warning("model spec resolution fail: %s, fallback active", exc)
                spec = self._kb_model_registry.get_active("row_extractor")

            # Fetch artifact path (same as Sprint 2 path)
            file_path = await self._fetch_artifact_path(file)

            # Stream extract + create_many per batch
            total_inserted = 0
            total_skipped = 0
            batch_count = 0

            async for batch_result in self._row_extractor.extract_rows(
                file_path=file_path,
                mime_type=file.mime_type,
                fields=doc_type.fields,
                dedup_key_fields=dedup_key_fields,
                model_spec=spec,
                max_rows=10_000,
                batch_size=25,  # Sprint 26 dogfood: 50 → 25 (output token budget)
                start_batch_idx=start_batch_idx,
                existing_load_result=None,  # Phase B Vision L2 fallback pending—TODO
            ):
                batch_count += 1
                # Write checkpoint BEFORE next batch starts (P1-PERF-8 atomic
                # UPDATE before next LLM call — guarantees resume-safety)
                checkpoint = CheckpointStateWrite(
                    last_completed_batch_idx=batch_result.batch_idx,
                    last_completed_row_seq=batch_result.rows[-1].row_seq if batch_result.rows else 0,
                    total_batches=batch_result.total_batches,
                    attempt_id=attempt_id,
                )
                await self._per_row_jobs_store.update_checkpoint(job_id, checkpoint)

                # Insert batch (UNIQUE INDEX fallback dedup)
                create_result = await self._extracted_rows_store.create_many(
                    rows=batch_result.rows,
                    file_id=file.file_id,
                    doc_type_id=doc_type_id,
                    workspace_id=file.workspace_id,
                    attempt_id=attempt_id,
                    extractor_model=spec.name,
                    created_by=file.uploaded_by,
                    doc_type_fields=doc_type.fields,
                )
                total_inserted += create_result.inserted_count
                total_skipped += create_result.skipped_dup_count

                # Progress + heartbeat (5-batch throttle, P1-PERF-1)
                heartbeat = batch_count % 5 == 0
                await self._per_row_jobs_store.update_progress(
                    job_id,
                    completed_batches=batch_result.batch_idx + 1,
                    rows_extracted=total_inserted,
                    rows_skipped_dup=total_skipped,
                    heartbeat=heartbeat,
                )

            # Success
            await self._per_row_jobs_store.mark_status(
                job_id,
                "succeeded",
                total_batches=batch_count,
            )
            await self._files.update_extracted_rows_status(
                file.file_id,
                "succeeded",
                expected_workspace_id=file.workspace_id,
            )
            if total_inserted > 0:
                await self._doc_type_store.update_row_count(doc_type_id, total_inserted)
            emit_audit(
                "kb.row_extract_completed",
                {
                    "file_id": file.file_id,
                    "doc_type_id": doc_type_id,
                    "job_id": job_id,
                    "rows_extracted": total_inserted,
                    "rows_skipped_dup": total_skipped,
                    "total_batches": batch_count,
                },
            )

        except TokenOverflowError:
            logger.warning("per-row token overflow file_id=%s", file.file_id)
            await self._per_row_jobs_store.mark_status(
                job_id,
                "failed",
                error_code="TOKEN_OVERFLOW",
                error_hint="batch_token_over_context_after_split",
            )
            await self._files.update_extracted_rows_status(
                file.file_id,
                "failed",
                expected_workspace_id=file.workspace_id,
            )
            emit_audit(
                "kb.row_extract_failed",
                {
                    "file_id": file.file_id,
                    "doc_type_id": doc_type_id,
                    "error_code": "TOKEN_OVERFLOW",
                },
            )

        except RowExtractionError as exc:
            # Transient: mark failed_retry so heartbeat scanner picks up later
            logger.warning(
                "per-row extraction error file_id=%s err_type=%s",
                file.file_id,
                type(exc).__name__,
            )
            err_msg = str(exc)[:200]  # constant-ish, sanitized at source
            # Map first token of err_msg to error_code enum
            if "LLM_TIMEOUT" in err_msg or "TimeoutError" in err_msg:
                error_code = "LLM_TIMEOUT"
            elif "LLM_INVALID_JSON" in err_msg or "JSON" in err_msg:
                error_code = "LLM_INVALID_JSON"
            elif "PARSE_FAILED" in err_msg:
                error_code = "PARSE_FAILED"
            else:
                error_code = "UNKNOWN"
            await self._per_row_jobs_store.mark_status(
                job_id,
                "failed_retry",
                error_code=error_code,
                error_hint=f"transient_{error_code.lower()}",
            )
            # NOT update kb_files yet — let scanner resume
            emit_audit(
                "kb.row_extract_failed",
                {
                    "file_id": file.file_id,
                    "doc_type_id": doc_type_id,
                    "error_code": error_code,
                    "status": "failed_retry",
                },
            )

        except Exception:
            logger.exception("per-row extraction unhandled file_id=%s", file.file_id)
            await self._per_row_jobs_store.mark_status(
                job_id,
                "failed",
                error_code="UNKNOWN",
                error_hint="unhandled_exception",
            )
            await self._files.update_extracted_rows_status(
                file.file_id,
                "failed",
                expected_workspace_id=file.workspace_id,
            )
            emit_audit(
                "kb.row_extract_failed",
                {
                    "file_id": file.file_id,
                    "doc_type_id": doc_type_id,
                    "error_code": "UNKNOWN",
                },
            )
        finally:
            # _fetch_artifact_path wrote a NamedTemporaryFile(delete=False); clean
            # it up so per-row extraction doesn't leak one temp file per doc.
            if file_path:
                self._cleanup_temp(file_path)

    # ─── internals ────────────────────────────────────────

    async def _fetch_artifact_path(self, file) -> str:
        """Resolve artifact_id → local file path via injected artifact_store.

        artifact_fetcher: ArtifactStore-like with async get(artifact_id) → Artifact (含 .path).
        Tests 模式 (artifact_fetcher=None): 直接用 artifact_id 当 file path (e.g. tmp_path / 'a.md').

        Day 19-20 真 corpus dogfood fix: artifact_store local backend 存 path 相对 (e.g.
        'artifacts/art_xxx_file.docx'), 但实际 file 在 cwd/.data/artifacts/. 走 backend.read()
        拿 bytes + write 到 NamedTemporaryFile, 给 loader 绝对 path.
        """
        if self._artifact_fetcher is None:
            return file.artifact_id
        art = await self._artifact_fetcher.get(file.artifact_id)
        if art is None:
            raise FileNotFoundError(f"{PARSE_ERROR_SOURCE_MISSING}: artifact {file.artifact_id} not found in artifact_store")

        # P0 fix: artifact_store local backend stores path relative (.data/ prefix stripped).
        # Read bytes via backend (which handles relative path resolution) + write to temp file.
        from pathlib import Path as _Path
        from tempfile import NamedTemporaryFile

        backend = self._artifact_fetcher.backend
        try:
            raw = await backend.read(art.path)
        except (FileNotFoundError, OSError):
            # Fallback: try .data/ prefix if relative path missing it
            alt_path = str(_Path(".data") / art.path)
            try:
                raw = await backend.read(alt_path)
            except FileNotFoundError as exc:
                # Missing in both places = original file lost (116 evidence: old deployment stored outside data volume, lost after container rebuild).
                # Classify separately; generic parse-failed would suggest retry, which can never find the file.
                raise FileNotFoundError(f"{PARSE_ERROR_SOURCE_MISSING}: original file of {file.artifact_id} is missing") from exc

        suffix = _Path(file.filename).suffix or ""
        tmp = NamedTemporaryFile(suffix=suffix, delete=False)  # noqa: SIM115
        tmp.write(raw)
        tmp.close()
        return tmp.name

    async def _source_identity(self, file) -> str | None:
        if self._artifact_fetcher is None:
            return None
        from bowhead_core.kb.source_integrity import source_head

        try:
            async with asyncio.timeout(3):
                artifact = await self._artifact_fetcher.get(file.artifact_id)
                _, head = await source_head(self._artifact_fetcher.backend, artifact.path)
                return head.identity if head else None
        except Exception:
            return None

    async def _load_with_retry(self, file, file_id: str, loader):
        """Load via loader with a bounded retry on *transient* failures (a valid
        PDF that PDFium fails to open under concurrent load but opens on a fresh
        attempt). The failed attempt fails at open → ~no vision work done, so the
        retry is the first real parse pass (not a duplicated one). The fetched
        temp file is cleaned up each attempt.

        untrusted_parse 簇 B 接线: OOXML(docx/xlsx/pptx) 是不受信 zip —— 解压炸弹
        实测 1.5MB 的 pptx 可撑到 3GB RSS(2030×), docx 同样暴露。worker 跑在网关
        进程内 → OOM = 全租户下线。`zip_bomb_reason_path` 读 zip 中央目录判压缩比,
        **不把文件读进内存**, 所以必须在 `loader.load` 之前跑。
        非 OOXML(pdf/md/image) 该函数自身 no-op, 零成本。
        """
        from bowhead_core.untrusted_parse import zip_bomb_reason_path

        load_result = None
        for attempt in range(_PARSE_MAX_ATTEMPTS):
            fingerprint = generation_writes_enabled() or bool(getattr(file, "active_generation_id", None))
            before = await self._source_identity(file) if fingerprint else None
            file_path = await self._fetch_artifact_path(file)
            try:
                after = await self._source_identity(file) if before else None
                # ⚠ Pass file.filename (authoritative name), not file_path: type detection uses extension;
                # relying on temporary suffix silently breaks when fetch changes. to_thread because it reads disk.
                bomb = await asyncio.to_thread(zip_bomb_reason_path, file_path, file.filename)
                if bomb:
                    emit_audit("kb.zip_bomb_rejected", {"file_id": file_id, "reason": bomb})
                    logger.warning("kb zip-bomb rejected file=%s reason=%s", file_id, bomb)
                    # Use existing parse_failed contract (worker.py:194 marks failed and shows reason
                    # to user); no transient flag → no retry.
                    return _bomb_load_result(bomb)
                source_sha256 = None
                if generation_writes_enabled() or getattr(file, "active_generation_id", None):
                    from bowhead_core.kb.source_integrity import hash_path

                    source_sha256 = await asyncio.to_thread(hash_path, file_path)
                load_result = await loader.load(file_path, use_llm=file.use_llm_marker)
                if source_sha256:
                    load_result.metadata["kb_source_sha256"] = source_sha256
                    if before and before == after:
                        load_result.metadata["kb_source_identity"] = before
            finally:
                self._cleanup_temp(file_path)
            if not _is_transient_load_failure(load_result):
                break  # success, or a non-transient failure (don't waste a retry)
            if attempt + 1 < _PARSE_MAX_ATTEMPTS:
                logger.warning(
                    "parse transient-fail file=%s attempt=%d/%d reason=%s — retrying",
                    file_id,
                    attempt + 1,
                    _PARSE_MAX_ATTEMPTS,
                    load_result.metadata.get("reason"),
                )
        return load_result

    def _cleanup_temp(self, file_path: str) -> None:
        """Delete the NamedTemporaryFile that _fetch_artifact_path created
        (delete=False so the loader can read it → otherwise leaks on disk; a
        big bundle left dozens of stray temp files). Only when artifact_fetcher
        is set; in test mode file_path is the caller's own file — never delete."""
        if self._artifact_fetcher is None:
            return
        import contextlib
        from pathlib import Path

        with contextlib.suppress(OSError):
            Path(file_path).unlink()

    async def _split(self, file, load_result) -> list[KbDocument]:
        """切分，并丢弃加载器占位（如空表的「没找到数据」）：占位不是内容，不入库。"""
        docs = await self._split_by_strategy(file, load_result)
        return [d for d in docs if not is_parse_failure_placeholder(d.page_content)]

    async def _split_by_strategy(self, file, load_result) -> list[KbDocument]:
        """Pick splitter strategy based on mime + page count.

        - markdown / md mime → markdown splitter (按 heading)
        - long PDF (>= 10 page) + inferrer 注入 → section_aware
        - default → paragraph splitter (中文 separator + 句号 hard cap)
        """
        mime = file.mime_type
        load_docs = load_result.documents
        if not load_docs:
            return []

        page_count = load_result.metadata.get("page_count", 0)

        # Markdown handled directly via heading splitter
        if mime in ("text/markdown", "text/x-markdown", "text/plain"):
            md_splitter = make_markdown_splitter()
            md_text = "\n\n".join(d.page_content for d in load_docs)

            # Heading splitting has no length cap: headingless .txt or very long section becomes one whole chunk;
            # embedding/rerank see only beginning, and oversized input rejects entire rerank request. Split long sections by paragraph,
            # retain section metadata (heading hierarchy); leave short sections unchanged, preserving chunk content/id.
            para_splitter = make_paragraph_splitter()
            out: list[KbDocument] = []
            for section in md_splitter.split_text(md_text):
                pieces = (
                    para_splitter.split_text(section.page_content)
                    if count_tokens(section.page_content) > DEFAULT_CHUNK_SIZE
                    else [section.page_content]
                )
                out.extend(KbDocument(page_content=piece, metadata={**(section.metadata or {})}) for piece in pieces)
            return out

        # Long PDF + inferrer present → section_aware
        if mime == "application/pdf" and page_count >= LONG_DOC_PAGE_THRESHOLD and self._inferrer is not None:
            md_text = load_docs[0].page_content if load_docs else ""
            sections = await self._inferrer.infer(
                md_text,
                page_count=page_count,
            )
            if sections:
                return section_aware_split(md_text, sections)

        # Default: paragraph splitter (Chinese separators)
        para_splitter = make_paragraph_splitter()
        # split each loaded doc, preserve metadata

        out: list[KbDocument] = []
        for d in load_docs:
            for chunk_text in para_splitter.split_text(d.page_content):
                out.append(
                    KbDocument(
                        page_content=chunk_text,
                        metadata={**(d.metadata or {})},
                    )
                )
        return out

    async def _embed_all(self, docs: list[KbDocument]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for batch in _chunked(docs, EMBED_BATCH_SIZE):
            authorize, owner = _PARSE_AUTHORIZER.get(), _PARSE_OWNER.get()
            if authorize is not None and (owner is None or not await authorize(await self._files.get(owner[0]))):
                raise PermissionError("rebuild access changed")
            batch_embs = await self._embedder.embed_batch([d.page_content for d in batch])
            embeddings.extend(batch_embs)
        return embeddings

    def _build_kb_chunks(
        self,
        file,
        docs: list[KbDocument],
        embeddings: list[list[float]],
    ) -> list[KbChunk]:
        # P1-4: strict zip prevents silent truncation on docs/embeddings length mismatch
        if len(docs) != len(embeddings):
            raise ValueError(
                f"docs/embeddings len mismatch: {len(docs)} vs {len(embeddings)}",
            )
        kb_chunks: list[KbChunk] = []
        for chunk_index, (d, emb) in enumerate(zip(docs, embeddings, strict=True), start=1):
            md = d.metadata or {}
            kb_chunks.append(
                KbChunk(
                    chunk_id=make_chunk_id(file.file_id, chunk_index, d.page_content),
                    file_id=file.file_id,
                    content=d.page_content,
                    embedding=emb,
                    metadata=ChunkMetadata(
                        file_id=file.file_id,
                        file_name=file.filename,
                        scope=file.scope,
                        scope_owner=file.scope_id,
                        workspace_id=file.workspace_id,
                        team_id=file.team_id,
                        chunk_index=chunk_index,
                        role=md.get("role", "intro"),
                        page=md.get("page"),
                        section_title=md.get("section_title"),
                        char_offset=md.get("char_offset") or md.get("start_index"),
                        lang=md.get("lang", "zh"),
                        version=file.version,
                        uploaded_by=file.uploaded_by,
                        uploaded_at=file.uploaded_at,
                    ),
                )
            )
        return kb_chunks

    async def _update_status(self, file_id: str, status: str, **fields) -> None:
        """[P1-Q6] state machine update + parse_progress_pct."""
        pct = PARSE_PROGRESS_MAP.get(status)
        owner = _PARSE_OWNER.get()
        owner_args = {"parse_owner": owner[1:]} if owner and owner[0] == file_id else {}
        await self._files.update(
            file_id,
            **owner_args,
            parse_status=status,
            parse_progress_pct=pct,
            **fields,
        )
        # kb-matrix M2 §8⑥: only successful parse terminal state = "done" (_PARSE_STATUS_VALUES CHECK
        # enum has no "ready"—that belongs to record verify_status, do not confuse) → increment kb_files.version
        # for matrix file-level stale derivation (cell.file_version < file.version).
        if status == "done":
            await self._files.bump_version(file_id)
