"""WP1 导出闭环 — export job SQLite 持久化 + boot 恢复.

覆盖:
- enqueue → job 落库 (queued)
- 状态变迁镜像 (_fail → failed 持久化)
- get_job: 内存 miss 走 DB (模拟重启后查历史 job)
- list_jobs_for_canvas: DB 权威 + 排序 + limit
- init_export_jobs boot 恢复: 中断的 queued/running → failed
- 无 DB (stub 模式) 回退内存行为
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.export_copy_contract import classify_export_copy_versions

from bowhead_core import canvas_export as ce


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "export_jobs_test.db")


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每测前清模块态 (dict + db_path), 测完还原."""
    ce._jobs.clear()
    old = ce._db_path
    ce._db_path = None
    yield
    ce._jobs.clear()
    ce._db_path = old


def _mk_job(jid: str, canvas_id: str = "cv_1", status: str = "queued") -> ce.ExportJob:
    return ce.ExportJob(id=jid, canvas_id=canvas_id, fmt="pdf", status=status)


def test_legacy_static_projection_recurses_and_drops_malformed_source_envelopes() -> None:
    projected = ce._static_safe_legacy_sections(
        [
            {
                "id": "sec_parent",
                "kind": "section",
                "label": "Analysis",
                "source_refs": "art_malformed",
                "body": [
                    {
                        "id": "sec_nested_sources",
                        "kind": "sources",
                        "entries": [
                            {
                                "artifact_id": "art_private_nested",
                                "label": "Board plan.xlsx",
                                "query": "select confidential_margin",
                            }
                        ],
                    },
                    {
                        "id": "sec_nested_chart",
                        "kind": "chart",
                        "spec": {
                            "title": "Revenue",
                            "source_meta": {
                                "artifact_id": "art_private_nested",
                                "query": "private query",
                            },
                        },
                    },
                ],
            }
        ],
        locale="en-US",
    )

    serialized = repr(projected)
    assert "art_malformed" not in serialized
    assert "art_private_nested" not in serialized
    assert "Board plan" not in serialized
    assert "confidential_margin" not in serialized
    assert "private query" not in serialized
    # Batch 0, 0-C leak#2: labels use limited wording from generic_type retained under the redaction contract,
    # replacing zero-information Source placeholders; real identities (four assertions above) remain unrecoverable.
    assert projected[0]["body"][0]["entries"] == [
        {
            "source_ref": "source-ordinal:1",
            "generic_type": "generated_artifact",
            "ordinal": 1,
            "label": "Generated analysis data",
        }
    ]
    assert projected[0]["body"][1]["spec"]["source_meta"]["source_ref"] == "source-ordinal:1"


def test_legacy_static_projection_types_source_labels_and_drops_empty_sources() -> None:
    """0-C leak#2 负对照：全空 sources 段整段不出；非空段按 generic_type 出有限文案。"""
    projected = ce._static_safe_legacy_sections(
        [
            {"id": "sec_empty_sources", "kind": "sources", "entries": []},
            {"id": "sec_no_entries_key", "kind": "sources"},
            {
                "id": "sec_typed_sources",
                "kind": "sources",
                "entries": [
                    {"artifact_id": "art_x1", "label": "秘密.xlsx"},
                    {"kb_chunk_id": "chunk_y2", "label": "内部文档"},
                    {"id": "tl_z3", "query": "select secret"},
                    {"id": "https://example.com/private"},
                ],
            },
        ],
        locale="zh-CN",
    )

    # Omit entire empty sections, including missing entries keys; previously rendered a bare "来源" heading.
    assert [sec["id"] for sec in projected] == ["sec_typed_sources"]
    labels = [entry["label"] for entry in projected[0]["entries"]]
    assert labels == ["分析生成的数据", "知识库文档", "查询结果", "网页来源"]
    # No redaction regression: no real label/query/id may appear.
    serialized = repr(projected)
    for secret in ("秘密", "内部文档", "select secret", "art_x1", "chunk_y2", "tl_z3", "example.com"):
        assert secret not in serialized


def test_legacy_export_order_uses_canonical_rank_fallback(monkeypatch) -> None:
    """0-C leak#3：legacy 导出跟 FE preprocessSections 用同一套 rank 兜底。

    writer 的 h1/meta 落库晚于 peer 期 code/chart（真机症状=PDF 标题在第 3 页）。
    flag 关（现状）→ 稳定 rank 强排；flag 开（seq 权威化）→ 保持 seq，不强排。
    """
    from types import SimpleNamespace

    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "0")
    raw = [
        SimpleNamespace(id="s_code", kind="code_block"),
        SimpleNamespace(id="s_chart", kind="chart"),
        SimpleNamespace(id="s_narr", kind="narrative"),
        SimpleNamespace(id="s_eyebrow", kind="eyebrow"),
        SimpleNamespace(id="s_h1", kind="h1"),
        SimpleNamespace(id="s_meta", kind="meta"),
    ]
    ordered = ce._canonical_export_order(raw)
    # Headers first → body (chart/narrative retain relative seq order) → code appendix last.
    assert [s.id for s in ordered] == ["s_eyebrow", "s_h1", "s_meta", "s_chart", "s_narr", "s_code"]

    # Positive control: once seq is authoritative, never force rank sorting (it would undo user dragging; P1-4 rule B).
    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "1")
    assert [s.id for s in ce._canonical_export_order(raw)] == [s.id for s in raw]


_W1_JOB_COLUMNS = {
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
}


@pytest.mark.asyncio
async def test_init_migrates_legacy_job_table_additively(db_path):
    legacy_schema = """
    CREATE TABLE canvas_export_jobs (
        id TEXT PRIMARY KEY,
        canvas_id TEXT NOT NULL,
        fmt TEXT NOT NULL,
        status TEXT NOT NULL,
        url TEXT,
        artifact_id TEXT,
        filename TEXT,
        size INTEGER,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(legacy_schema)
        await db.execute(
            "INSERT INTO canvas_export_jobs (id,canvas_id,fmt,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("exp_legacy", "cv_legacy", "pdf", "done", "2026-07-01", "2026-07-01"),
        )
        await db.commit()

    await ce.init_export_jobs(db_path)

    async with aiosqlite.connect(db_path) as db:
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(canvas_export_jobs)")).fetchall()}
        indexes = {row[1] for row in await (await db.execute("PRAGMA index_list(canvas_export_jobs)")).fetchall()}
        row = await (
            await db.execute(
                "SELECT contract_version,phase,quality_status,warning_count,attempt,status FROM canvas_export_jobs WHERE id='exp_legacy'"
            )
        ).fetchone()

    assert columns >= _W1_JOB_COLUMNS
    assert {
        "idx_export_jobs_canvas",
        "idx_export_jobs_idempotency",
        "idx_export_jobs_status_phase",
    } <= indexes
    assert row == (1, "queued", "pending", 0, 0, "done")


@pytest.mark.asyncio
async def test_persist_upsert_keeps_v2_metadata_across_status_updates(db_path):
    await ce.init_export_jobs(db_path)
    job = ce.ExportJob(
        id="exp_v2",
        canvas_id="cv_v2",
        fmt="pptx",
        status="queued",
        contract_version=2,
        profile="full_report",
        parity_contract="full_content_v1",
        surface_token="sha256:surface",
        phase="snapshotting",
        quality_status="pending",
        snapshot_id="exs_123",
        requested_by="u_1",
        idempotency_key="idem_1",
        request_sha256="sha256:request",
        attempt=2,
        runner_kind="local",
    )
    await ce._persist(job)
    async with aiosqlite.connect(db_path) as db:
        original_rowid = (await (await db.execute("SELECT rowid FROM canvas_export_jobs WHERE id=?", (job.id,))).fetchone())[0]

    job.status = "running"
    job.phase = "rendering"
    await ce._persist(job)
    async with aiosqlite.connect(db_path) as db:
        updated_rowid = (await (await db.execute("SELECT rowid FROM canvas_export_jobs WHERE id=?", (job.id,))).fetchone())[0]
    ce._jobs.clear()

    got = await ce.get_job(job.id)
    assert got is not None
    assert got.contract_version == 2
    assert got.profile == "full_report"
    assert got.parity_contract == "full_content_v1"
    assert got.surface_token == "sha256:surface"
    assert got.snapshot_id == "exs_123"
    assert got.requested_by == "u_1"
    assert got.idempotency_key == "idem_1"
    assert got.request_sha256 == "sha256:request"
    assert got.attempt == 2
    assert got.runner_kind == "local"
    assert got.status == "running"
    assert got.phase == "rendering"
    assert updated_rowid == original_rowid


@pytest.mark.asyncio
async def test_persist_and_get_after_memory_loss(db_path):
    await ce.init_export_jobs(db_path)
    job = _mk_job("exp_aaa")
    ce._jobs[job.id] = job
    await ce._persist(job)

    # Simulate gateway restart: memory clears, database remains.
    ce._jobs.clear()
    got = await ce.get_job("exp_aaa")
    assert got is not None
    assert got.id == "exp_aaa"
    assert got.canvas_id == "cv_1"


@pytest.mark.asyncio
async def test_fail_persists_via_nowait(db_path):
    await ce.init_export_jobs(db_path)
    job = _mk_job("exp_bbb")
    ce._jobs[job.id] = job
    await ce._persist(job)

    ce._fail(job, "boom: human readable reason")
    # _persist_nowait is a fire-and-forget task; let the loop advance once.
    await asyncio.sleep(0.05)

    ce._jobs.clear()
    got = await ce.get_job("exp_bbb")
    assert got is not None
    assert got.status == "failed"
    assert "boom" in (got.error or "")


@pytest.mark.asyncio
async def test_list_jobs_db_authoritative_with_order_and_limit(db_path):
    await ce.init_export_jobs(db_path)
    for i in range(5):
        j = _mk_job(f"exp_l{i}", canvas_id="cv_list")
        j.created_at = f"2026-06-12T00:0{i}:00+00:00"
        await ce._persist(j)
    # Do not mix in another Canvas's jobs.
    await ce._persist(_mk_job("exp_other", canvas_id="cv_other"))

    items = await ce.list_jobs_for_canvas("cv_list", limit=3)
    assert [j.id for j in items] == ["exp_l4", "exp_l3", "exp_l2"]


@pytest.mark.asyncio
async def test_boot_recovery_marks_interrupted_failed(db_path):
    await ce.init_export_jobs(db_path)
    for jid, st in [("exp_q", "queued"), ("exp_r", "running"), ("exp_d", "done")]:
        await ce._persist(_mk_job(jid, status=st))

    # Second initialization simulates restart.
    await ce.init_export_jobs(db_path)
    q = await ce.get_job("exp_q")
    r = await ce.get_job("exp_r")
    d = await ce.get_job("exp_d")
    assert q is not None and q.status == "failed"
    assert "restart" in (q.error or "")
    assert r is not None and r.status == "failed"
    assert d is not None and d.status == "done"  # Leave completed jobs unchanged


@pytest.mark.asyncio
async def test_boot_recovery_only_fails_legacy_jobs(db_path):
    await ce.init_export_jobs(db_path)
    legacy = _mk_job("exp_legacy_queued", status="queued")
    v2 = ce.ExportJob(
        id="exp_v2_queued",
        canvas_id="cv_1",
        fmt="pdf",
        status="queued",
        contract_version=2,
        phase="queued",
    )
    await ce._persist(legacy)
    await ce._persist(v2)

    await ce.init_export_jobs(db_path)
    ce._jobs.clear()

    recovered_legacy = await ce.get_job(legacy.id)
    recovered_v2 = await ce.get_job(v2.id)
    assert recovered_legacy is not None and recovered_legacy.status == "failed"
    assert recovered_v2 is not None and recovered_v2.status == "queued"


@pytest.mark.asyncio
async def test_stub_mode_without_db_falls_back_to_memory():
    # _db_path=None (set by autouse fixture); same behavior as the old in-memory dict.
    job = _mk_job("exp_mem", canvas_id="cv_mem")
    ce._jobs[job.id] = job
    assert await ce.get_job("exp_mem") is job
    items = await ce.list_jobs_for_canvas("cv_mem")
    assert items == [job]
    assert await ce.get_job("exp_missing") is None


@pytest.mark.asyncio
async def test_legacy_completion_atomically_publishes_b1a_pair(db_path, tmp_path):
    artifacts = ArtifactStore(
        db_path=db_path,
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await artifacts.initialize()
    await ce.init_export_jobs(db_path)
    job = _mk_job("exp_atomic", status="running")
    ce._jobs[job.id] = job
    await ce._persist(job)
    output = tmp_path / "report.pdf"
    output.write_bytes(b"%PDF-1.7\n%%EOF\n")

    artifact = await ce._persist_legacy_export_pair(
        job=job,
        artifact_store=artifacts,
        output_path=output,
        filename="report.pdf",
        mime="application/pdf",
        canvas_id=job.canvas_id,
        created_by="u_owner",
        session_id=None,
        engine="legacy",
    )

    assert job.status == "done"
    assert job.artifact_id == artifact.id
    assert job.manifest_artifact_id == artifact.manifest_artifact_id
    persisted = await ce.get_job(job.id)
    assert persisted is job
    versions = await classify_export_copy_versions([job], artifact_store=artifacts)
    assert versions[job.id] == 3


@pytest.mark.asyncio
async def test_legacy_completion_cancellation_after_commit_keeps_done_pair(
    db_path,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    artifacts = ArtifactStore(
        db_path=db_path,
        backend=LocalBackend(root_dir=tmp_path / "cancel-blobs"),
    )
    await artifacts.initialize()
    await ce.init_export_jobs(db_path)
    job = _mk_job("exp_atomic_cancel", status="running")
    ce._jobs[job.id] = job
    await ce._persist(job)
    output = tmp_path / "cancel-report.pdf"
    output.write_bytes(b"%PDF-1.7\n%%EOF\n")
    commit_landed = asyncio.Event()
    release_commit = asyncio.Event()
    original_commit = aiosqlite.Connection.commit

    async def delayed_commit(connection: aiosqlite.Connection) -> None:
        await original_commit(connection)
        commit_landed.set()
        await release_commit.wait()

    monkeypatch.setattr(aiosqlite.Connection, "commit", delayed_commit)
    publication = asyncio.create_task(
        ce._persist_legacy_export_pair(
            job=job,
            artifact_store=artifacts,
            output_path=output,
            filename="report.pdf",
            mime="application/pdf",
            canvas_id=job.canvas_id,
            created_by="u_owner",
            session_id=None,
            engine="legacy",
        )
    )
    await commit_landed.wait()
    publication.cancel()
    release_commit.set()

    artifact = await publication
    persisted = await ce.get_job(job.id)
    assert persisted is not None and persisted.status == "done"
    assert await artifacts.get(artifact.id) is not None
    assert await artifacts.get(artifact.manifest_artifact_id or "") is not None


@pytest.mark.asyncio
async def test_legacy_completion_transaction_failure_discards_staged_blobs(db_path, tmp_path):
    blob_root = tmp_path / "rollback-blobs"
    artifacts = ArtifactStore(
        db_path=db_path,
        backend=LocalBackend(root_dir=blob_root),
    )
    await artifacts.initialize()
    await ce.init_export_jobs(db_path)
    job = _mk_job("exp_atomic_rollback", status="running")
    ce._jobs[job.id] = job
    await ce._persist(job)
    output = tmp_path / "rollback-report.pdf"
    output.write_bytes(b"%PDF-1.7\n%%EOF\n")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            "CREATE TRIGGER fail_legacy_b1a_output BEFORE INSERT ON artifacts "
            "WHEN NEW.producer_kind='canvas_export_b1a_v1_pdf_legacy' "
            "BEGIN SELECT RAISE(ABORT, 'forced legacy publication failure'); END;"
        )
        await db.commit()

    with pytest.raises(Exception, match="forced legacy publication failure"):
        await ce._persist_legacy_export_pair(
            job=job,
            artifact_store=artifacts,
            output_path=output,
            filename="report.pdf",
            mime="application/pdf",
            canvas_id=job.canvas_id,
            created_by="u_owner",
            session_id=None,
            engine="legacy",
        )

    assert job.status == "running"
    persisted = await ce.get_job(job.id)
    assert persisted is not None and persisted.status == "running"
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE canvas_id=?",
            (job.canvas_id,),
        ) as cursor,
    ):
        assert (await cursor.fetchone())[0] == 0
    assert list(blob_root.rglob("*bowhead-export-staged-*")) == []


@pytest.mark.asyncio
async def test_live_in_memory_job_overrides_db_row(db_path):
    """list 时本进程在飞的 job 用内存版 (比 DB 镜像更新)."""
    await ce.init_export_jobs(db_path)
    job = _mk_job("exp_live", canvas_id="cv_live")
    await ce._persist(job)  # DB: queued
    job.status = "running"  # Updated in memory but not yet mirrored
    ce._jobs[job.id] = job
    items = await ce.list_jobs_for_canvas("cv_live")
    assert items[0].status == "running"
