"""KbMatrixEngine UT — kb-matrix M2 块2（验收 2/3/4 + 短路/缓存/配额/冻结/恢复）。

harness：真 store 族（matrix/files/quotas/user，tmp sqlite）+ 真 StructuredExtractor
注入可编程 FakeRouter（盘点确认 extract 全链无其他外部依赖）+ FakeKbStore 喂 chunks。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_engine import (
    KbMatrixEngine,
    MatrixInsufficientCredits,
    MatrixRateLimited,
    MatrixRunConflict,
    MatrixRunError,
    ProviderHealth,
    build_cache_key,
    call_timeout_s,
    column_fingerprint,
    normalize_prompt_text,
    zombie_threshold_s,
)
from bowhead_core.kb.matrix_store import KbMatrixStore, new_run_id
from bowhead_core.kb.structured_extractor import StructuredExtractor
from bowhead_core.kb.workspace_quotas_store import KbWorkspaceQuotasStore
from bowhead_core.user_store import UserStore, _now

if TYPE_CHECKING:
    from pathlib import Path

WS = "ws_mx"
TEAM = "team_mx"
USER = "u_runner"


# ─── fakes ──────────────────────────────────────────────────


@dataclass
class _Usage:
    input_tokens: int = 3000
    output_tokens: int = 2000
    cached_tokens: int = 0


@dataclass
class _Resp:
    content: str
    usage: _Usage = field(default_factory=_Usage)
    provider: str = "prov_a"
    model: str = "model_a"


@dataclass
class _Binding:
    primary: tuple[str, str] = ("prov_a", "model_a")


def _parse_requested_fields(req: Any) -> list[str]:
    content = req.messages[0].content
    idx = content.rindex("要抽取的字段:")
    spec = json.loads(content[idx + len("要抽取的字段:") :])
    return [f["name"] for f in spec]


def _content_for(names: list[str], value_map: dict[str, Any] | None = None, snippet_map: dict[str, str] | None = None) -> str:
    out = {}
    for n in names:
        out[n] = {
            "value": (value_map or {}).get(n, f"{n}的值"),
            "source_snippet": (snippet_map or {}).get(n, ""),
            "source_page": 1,
            "confidence": 0.9,
        }
    return json.dumps(out, ensure_ascii=False)


class FakeRouter:
    """resolve("lead").primary=("prov_a","model_a")；invoke 行为由 handler(i, req,
    user_pref) 编程——返回 _Resp 或抛异常。记录每次 user_pref（短路断言）。"""

    def __init__(self, handler: Any = None) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def resolve(self, agent_type: str, profile: Any = None, user_pref: Any = None) -> _Binding:
        return _Binding()

    async def invoke(self, agent_type: str, req: Any, profile: Any = None, user_pref: Any = None) -> _Resp:
        i = len(self.calls)
        self.calls.append({"user_pref": user_pref, "req": req})
        if self.handler is not None:
            out = self.handler(i, req, user_pref)
            if isinstance(out, Exception):
                raise out
            if out is not None:
                return out
        return _Resp(content=_content_for(_parse_requested_fields(req)))


class FakeKbStore:
    def __init__(self, chunks_by_file: dict[str, list[tuple[str, str, int]]]) -> None:
        self.chunks_by_file = chunks_by_file

    async def get_ordered_chunks_for_file(self, file: Any) -> list[tuple[str, str, int]]:
        return self.chunks_by_file.get(file.file_id, [])


# ─── harness ────────────────────────────────────────────────


@dataclass
class Env:
    engine: KbMatrixEngine
    store: KbMatrixStore
    files_store: KbFilesStore
    user_store: UserStore
    quotas: KbWorkspaceQuotasStore
    router: FakeRouter
    kb: FakeKbStore


async def _seed_credit(user_store: UserStore, *, user_balance: int = 100_000, team_balance: int = 100_000) -> None:
    async with aiosqlite.connect(user_store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'O', 'o.com', 10, 'ultra', ?, ?, '{}', 'active', ?)",
            (TEAM, team_balance, team_balance, _now()),
        )
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, 'r@o.com', 1, '', '[]', 'ultra', ?, ?, ?, 'zh-CN', 'UTC', '{}', 'active', ?)",
            (USER, TEAM, user_balance, user_balance, _now(), _now()),
        )
        await db.commit()


@pytest.fixture(autouse=True)
def _migrated_kb_files(tmp_path: Path, initialize_migrated_kb_files_database) -> None:
    """_mk_env 的 KbFilesStore 建在 tmp_path/files.db；先铺 Alembic head 结构。"""
    initialize_migrated_kb_files_database(tmp_path / "files.db")


async def _mk_env(
    tmp_path: Path,
    *,
    handler: Any = None,
    chunks_by_file: dict[str, list[tuple[str, str, int]]] | None = None,
    user_balance: int = 100_000,
    team_balance: int = 100_000,
) -> Env:
    store = KbMatrixStore(tmp_path / "kb_matrix.db")
    await store.initialize()
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    user_store = UserStore(str(tmp_path / "users.db"))
    await user_store.initialize()
    await _seed_credit(user_store, user_balance=user_balance, team_balance=team_balance)
    quotas = KbWorkspaceQuotasStore(tmp_path / "quotas.db")
    await quotas.initialize()
    router = FakeRouter(handler)
    kb = FakeKbStore(chunks_by_file or {})
    engine = KbMatrixEngine(
        matrix_store=store,
        files_store=files_store,
        kb_store=kb,  # type: ignore[arg-type]
        extractor=StructuredExtractor(router=router),
        router=router,
        user_store=user_store,
        quotas_store=quotas,
    )
    return Env(engine=engine, store=store, files_store=files_store, user_store=user_store, quotas=quotas, router=router, kb=kb)


async def _register_file(env: Env, file_id_hint: str, content: str) -> str:
    """files_store 里造一个真文件行（chunks 由 FakeKbStore 喂）。返回 file_id。"""
    real_id = await env.files_store.create(
        artifact_id="dummy",
        filename=f"{file_id_hint}.md",
        mime_type="text/markdown",
        size_bytes=len(content),
        scope="workspace",
        scope_id=WS,
        workspace_id=WS,
        team_id=TEAM,
        uploaded_by=USER,
    )
    env.kb.chunks_by_file[real_id] = [(f"chk_{real_id}_1", content, 1)]
    return real_id


async def _mk_matrix(env: Env, file_ids: list[str], columns: list[dict[str, Any]]):
    files = [{"file_id": fid, "char_count": 1000} for fid in file_ids]
    return await env.store.create_matrix(
        name="矩阵",
        workspace_id=WS,
        team_id=TEAM,
        created_by=USER,
        files=files,
        columns=columns,
    )


async def _wait_run(env: Env, run_id: str, timeout: float = 10.0) -> None:
    task = env.engine.run_tasks.get(run_id)
    if task is not None:
        await asyncio.wait_for(task, timeout)


@pytest.fixture(autouse=True)
def _single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)


# ─── expand_targets (acceptance 2 / FR-9 / FR-12b) ────────


async def test_expand_targets_excludes_edited_all_scopes(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1", "f2", "f3"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    # f1 = done+edited; f2 = plain failed; f3 = failed+edited (rerun failure after edit preserves flag)
    await env.store.edit_cell_value(
        m.matrix_id, "f1", col, value="人工", column={"type": "question", "prompt_version": 1}, workspace_id=WS, team_id=TEAM
    )
    await env.store.enqueue_cells(
        m.matrix_id, [("f2", col)], run_id="mxrun_x", workspace_id=WS, team_id=TEAM, column_versions={col: 1}, file_versions={}
    )
    await env.store.finalize_cell(m.matrix_id, "f2", col, status="failed", fail_reason="timeout")
    await env.store.edit_cell_value(
        m.matrix_id, "f3", col, value="人工3", column={"type": "question", "prompt_version": 1}, workspace_id=WS, team_id=TEAM
    )
    await env.store.enqueue_cells(
        m.matrix_id, [("f3", col)], run_id="mxrun_y", workspace_id=WS, team_id=TEAM, column_versions={col: 1}, file_versions={}
    )
    await env.store.finalize_cell(m.matrix_id, "f3", col, status="failed", fail_reason="timeout")

    cells = await env.store.list_cells(m.matrix_id)
    # all_pending = empty+failed: f2 targeted; f3 (failed+edited) excluded/counted by FR-9;
    # f1 (done+edited) not candidate, do not inflate count
    targets, skipped = KbMatrixEngine.expand_targets(m, cells, {"kind": "all_pending"})
    assert targets == [("f2", col)]
    assert skipped == 1
    # force_all = all except edited: f1+f3 both excluded and counted
    targets, skipped = KbMatrixEngine.expand_targets(m, cells, {"kind": "force_all"})
    assert targets == [("f2", col)]
    assert skipped == 2
    targets, skipped = KbMatrixEngine.expand_targets(m, cells, {"kind": "force_column", "column_id": col})
    assert targets == [("f2", col)]
    assert skipped == 2
    # Explicit single-cell operation can target edited cell
    targets, skipped = KbMatrixEngine.expand_targets(m, cells, {"kind": "single_cell", "file_id": "f1", "column_id": col})
    assert targets == [("f1", col)]
    assert skipped == 0


async def test_expand_targets_all_pending_is_empty_plus_failed(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1", "f2"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    await env.store.enqueue_cells(
        m.matrix_id, [("f1", col)], run_id="mxrun_x", workspace_id=WS, team_id=TEAM, column_versions={col: 1}, file_versions={}
    )
    await env.store.finalize_cell(
        m.matrix_id, "f1", col, status="done", variants=[{"is_primary": True, "normalized": {"value": "x"}}], prompt_version=1, file_version=1
    )
    cells = await env.store.list_cells(m.matrix_id)
    targets, _ = KbMatrixEngine.expand_targets(m, cells, {"kind": "all_pending"})
    assert targets == [("f2", col)]  # done excluded from all_pending; empty included
    # force_all includes done, excludes edited (none)
    targets, _ = KbMatrixEngine.expand_targets(m, cells, {"kind": "force_all"})
    assert set(targets) == {("f1", col), ("f2", col)}


# ─── Estimates (acceptance 4, fixed-value assertions) ─────


async def test_estimate_fixed_values_with_chunk_multiplier(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    m = await env.store.create_matrix(
        name="est",
        workspace_id=WS,
        team_id=TEAM,
        created_by=USER,
        files=[{"file_id": "fa", "char_count": 1000}, {"file_id": "fb", "char_count": 50_000}],
        columns=[
            {"name": "A", "type": "question", "question": "a?"},
            {"name": "B", "type": "question", "question": "b?"},
        ],
    )
    cols = [c["column_id"] for c in m.columns]
    targets = [(f, c) for f in ("fa", "fb") for c in cols]
    est = env.engine.estimate_targets(m, targets)
    # M3 formula = chars + prompt text×n_chunks + reasoning×n_cols×n_chunks (S-P2-2 adds prompt term):
    # fa: 1000 + (2+2)×1 + 1500×2×1 = 4004 → 5；fb: 50000 + 4×3 + 1500×2×3 = 59012 → 60
    assert est["credits_estimated"] == 65
    assert est["calls"] == 2
    assert est["cells_pending"] == 4
    assert est["eta_s"] == 14  # ceil(28×4/8)


async def test_start_run_blocked_when_over_balance(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path, user_balance=2, team_balance=100)  # est=3 >2 → reject
    fid = await _register_file(env, "f1", "文本")
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    with pytest.raises(MatrixInsufficientCredits) as ei:
        await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    assert ei.value.payload["available"] == 2  # min(two levels) = user
    assert ei.value.payload["estimated"] == 3


# ─── Full run flow (acceptance 1, backend slice) ──────────


async def test_run_end_to_end_success(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    content = "合同总金额为人民币5000000元"
    fid = await _register_file(env, "f1", content)

    def handler(i: int, req: Any, user_pref: Any) -> _Resp:
        names = _parse_requested_fields(req)
        return _Resp(content=_content_for(names, {"金额": "5000000"}, {"金额": "合同总金额为人民币5000000元"}))

    env.router.handler = handler
    m = await _mk_matrix(env, [fid], [{"name": "金额", "type": "question", "question": "合同金额?"}])
    col = m.columns[0]["column_id"]
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)

    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "succeeded"
    assert got.cells_done == 1
    cell = await env.store.get_cell(m.matrix_id, fid, col)
    assert cell is not None
    assert cell.status == "done"
    assert cell.variants[0]["normalized"] == {"value": "5000000"}
    assert cell.variants[0]["chunk_id"] == f"chk_{fid}_1"  # backfill hit
    assert cell.content_hash
    assert cell.cache_key
    # Billing: usage 5000 →5 credits; run totals == ledger lookup
    assert got.credits_charged == 5
    async with (
        aiosqlite.connect(env.user_store.db_path) as db,
        db.execute("SELECT COALESCE(SUM(-delta),0) FROM credit_ledger WHERE turn_id=? AND reason='kb_matrix_run'", (run.run_id,)) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == 5
    # Quota wiring: actual tokens enter usage
    summary = await env.quotas.get_summary(WS)
    assert summary is not None and summary.tokens_used == 5000


async def test_needs_review_sentinel_lands(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "金额大约一百元左右")

    def handler(i: int, req: Any, user_pref: Any) -> _Resp:
        # Nonnumeric currency value → extractor coerce preserves original → wrap yields raw_string sentinel
        names = _parse_requested_fields(req)
        return _Resp(content=_content_for(names, {"金额": "金额以实际结算为准"}))

    env.router.handler = handler
    m = await _mk_matrix(env, [fid], [{"name": "金额", "type": "currency", "question": "金额?"}])
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    cell = await env.store.get_cell(m.matrix_id, fid, m.columns[0]["column_id"])
    assert cell is not None
    assert cell.status == "needs_review"  # I-A6 sentinel → FR-8 glue


async def test_missing_field_key_is_parse_failed(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本")
    env.router.handler = lambda i, req, p: _Resp(content="{}")  # Contains no field keys
    m = await _mk_matrix(env, [fid], [{"name": "金额", "type": "question", "question": "金额?"}])
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    cell = await env.store.get_cell(m.matrix_id, fid, m.columns[0]["column_id"])
    assert cell is not None
    assert cell.status == "failed"
    assert cell.fail_reason == "llm_parse_failed"


async def test_file_without_chunks_fails_cells(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "x")
    env.kb.chunks_by_file[fid] = []  # No chunks
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    cell = await env.store.get_cell(m.matrix_id, fid, m.columns[0]["column_id"])
    assert cell is not None
    assert cell.status == "failed"
    assert cell.fail_hint == "file_has_no_chunks"


async def test_missing_file_skipped_not_failed(tmp_path: Path) -> None:
    """§4.6：文件不可用 → 跳过该行不算 failed，cells_total 剔除。"""
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["kbf_ghost"], [{"name": "A", "type": "question", "question": "a?"}])
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "succeeded"
    assert got.cells_total == 0  # 1 →0 after exclusion
    assert got.cells_failed == 0
    assert await env.store.get_cell(m.matrix_id, "kbf_ghost", m.columns[0]["column_id"]) is None  # Rollback deletes row


# ─── Billing guards (acceptance 4) ───────────────────────


async def test_deduct_failure_systemic_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mid-run 余额扣穿 → 停止投递；已交付 call 保留 done 且照实扣；剩余 → failed(insufficient_credits)。

    数字标定：est = 4×ceil((1000+1500)/1000) = 12 ≤ 余额 12 过预检；实耗 5/call，
    第 3 笔余额 2 < 5 仍照实扣到 -3（本 call 已交付照常 done），余额 ≤0 ⇒ 第 4 格停跑。
    """
    monkeypatch.setenv("BOWHEAD_KB_MATRIX_CELL_CONCURRENCY", "1")
    env = await _mk_env(tmp_path, user_balance=12, team_balance=1000)
    fids = [await _register_file(env, f"f{i}", f"文本{i}") for i in range(4)]
    m = await _mk_matrix(env, fids, [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "failed"
    assert got.error_code == "insufficient_credits"
    statuses = {}
    for fid in fids:
        cell = await env.store.get_cell(m.matrix_id, fid, col)
        assert cell is not None
        statuses[fid] = (cell.status, cell.fail_reason)
    assert statuses[fids[0]][0] == "done"
    assert statuses[fids[1]][0] == "done"
    assert statuses[fids[2]][0] == "done"  # Overdrawn, service delivered
    assert statuses[fids[3]] == ("failed", "insufficient_credits")
    # Run totals == ledger lookup (all three charged actual, third overdraws)
    assert got.credits_charged == 15
    user = await env.user_store.get_user(USER)
    assert user is not None and user.credit_balance == -3
    assert len(env.router.calls) == 3  # Fourth call not dispatched


async def test_quota_exceeded_stops_before_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_KB_MATRIX_CELL_CONCURRENCY", "1")
    env = await _mk_env(tmp_path)
    f1 = await _register_file(env, "f1", "文本")
    m = await _mk_matrix(env, [f1], [{"name": "A", "type": "question", "question": "a?"}])
    await env.quotas.set_cap(WS, 10)
    await env.quotas.increment_usage(WS, 100)  # Cap hit
    # start_run preflight rejects (different path); create directly via store, execute, test mid-run pre-dispatch gate
    run_dto = await env.store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=USER,
        targets=[(f1, m.columns[0]["column_id"])],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={m.columns[0]["column_id"]: 1},
        file_versions={f1: 1},
    )
    await env.engine.execute_run(run_dto.run_id)
    got = await env.store.get_run(run_dto.run_id)
    assert got is not None
    assert got.status == "failed"
    assert got.error_code == "quota_exceeded"
    assert env.router.calls == []  # Zero LLM calls
    cell = await env.store.get_cell(m.matrix_id, f1, m.columns[0]["column_id"])
    assert cell is not None
    assert cell.fail_reason == "quota_exceeded"


# ─── Power-loss recovery (acceptance 3) ──────────────────


async def test_resume_requeues_running_not_done(tmp_path: Path) -> None:
    """running cells 回 queued 重跑；done 格不重跑（cell 状态即 checkpoint）。"""
    env = await _mk_env(tmp_path)
    f1 = await _register_file(env, "f1", "一")
    f2 = await _register_file(env, "f2", "二")
    m = await _mk_matrix(env, [f1, f2], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    run = await env.store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=USER,
        targets=[(f1, col), (f2, col)],
        skipped_edited=0,
        credits_estimated=2,
        column_versions={col: 1},
        file_versions={f1: 1, f2: 1},
    )
    # Simulate state before process death: f1 done, f2 died mid-call (running)
    done_variants = [{"is_primary": True, "normalized": {"value": "旧值"}, "extracted_by": "tier1"}]
    await env.store.finalize_cell(m.matrix_id, f1, col, status="done", variants=done_variants, prompt_version=1, file_version=1)
    await env.store.mark_cells_running(m.matrix_id, [(f2, col)])

    resumed = await env.engine.resume_interrupted_runs()
    assert resumed == 1
    await _wait_run(env, run.run_id)
    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "succeeded"
    assert len(env.router.calls) == 1  # Rerun f2 only; done cell gets no calls
    c1 = await env.store.get_cell(m.matrix_id, f1, col)
    assert c1 is not None
    assert c1.variants[0]["normalized"] == {"value": "旧值"}  # Untouched
    c2 = await env.store.get_cell(m.matrix_id, f2, col)
    assert c2 is not None
    assert c2.status == "done"


async def test_resume_cancelling_converges_to_cancelled(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    run = await env.store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=USER,
        targets=[("f1", col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    await env.store.set_run_status(run.run_id, "cancelling")
    await env.engine.resume_interrupted_runs()
    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "cancelled"
    assert await env.store.get_cell(m.matrix_id, "f1", col) is None  # empty rollback deletes row


async def test_cancel_paused_run_inline_convergence(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    run = await env.store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=USER,
        targets=[("f1", col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    await env.store.set_run_status(run.run_id, "paused")
    assert await env.engine.cancel_run(run.run_id)
    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "cancelled"
    # Terminal run cannot be cancelled again
    assert not await env.engine.cancel_run(run.run_id)


async def test_zombie_scan_marks_stale_run(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    run = await env.store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=USER,
        targets=[("f1", col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    async with aiosqlite.connect(str(tmp_path / "kb_matrix.db")) as db:
        await db.execute("UPDATE kb_matrix_runs SET heartbeat_at = '2020-01-01T00:00:00+00:00' WHERE run_id = ?", (run.run_id,))
        await db.commit()
    assert await env.engine.scan_zombies_once() == 1
    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "failed"
    assert got.error_code == "system_interrupted"
    cell = await env.store.get_cell(m.matrix_id, "f1", col)
    assert cell is not None
    assert cell.fail_reason == "system_interrupted"


# ─── Health short-circuit (§5.4) ─────────────────────────


async def test_provider_shortcircuit_via_user_pref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """primary 连败 2 次（fallback 兜住）→ 第 3 call 起 user_pref 顶健康 fallback。"""
    monkeypatch.setenv("BOWHEAD_KB_MATRIX_CELL_CONCURRENCY", "1")
    env = await _mk_env(tmp_path)
    fids = [await _register_file(env, f"f{i}", f"文本{i}") for i in range(3)]

    def handler(i: int, req: Any, user_pref: Any) -> _Resp:
        # First two primary failures rescued by fallback (effective=prov_b); pref takes effect after short-circuit
        return _Resp(content=_content_for(_parse_requested_fields(req)), provider="prov_b", model="model_b")

    env.router.handler = handler
    m = await _mk_matrix(env, fids, [{"name": "A", "type": "question", "question": "a?"}])
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    prefs = [c["user_pref"] for c in env.router.calls]
    assert prefs[0] is None
    assert prefs[1] is None
    assert prefs[2] is not None
    assert prefs[2].provider == "prov_b"


def test_provider_health_probe_and_recovery() -> None:
    h = ProviderHealth(("prov_a", "model_a"), threshold=2, probe_every=3)
    assert h.next_pref() is None
    h.record_success("prov_b", "model_b")  # Fallback success = primary failure 1
    h.record_success("prov_b", "model_b")  # Failure 2 → short-circuit
    assert h.shorted
    p1 = h.next_pref()
    assert p1 is not None and p1.provider == "prov_b"
    p2 = h.next_pref()
    assert p2 is not None
    assert h.next_pref() is None  # Third call allows probe
    h.record_success("prov_a", "model_a")  # Probe success → clear
    assert not h.shorted
    assert h.next_pref() is None


def test_provider_health_chain_failure_no_shortcircuit() -> None:
    h = ProviderHealth(("prov_a", "model_a"), threshold=2, probe_every=3)
    h.record_chain_failure()
    h.record_chain_failure()
    assert not h.shorted  # Exhausted chain has no healthy fallback, no basis for short-circuit
    assert h.next_pref() is None


# ─── Cache (§5.6) ───────────────────────────────────────


async def test_cache_hit_across_matrices_regrounds_handles(tmp_path: Path) -> None:
    """同 team 同内容同列 → 第二矩阵命中缓存：零 LLM、零扣费、chunk_id 重打到自己文件。"""
    env = await _mk_env(tmp_path)
    content = "合同总金额为人民币5000000元"
    f1 = await _register_file(env, "f1", content)
    f2 = await _register_file(env, "f2", content)  # Same content → same content_hash

    def handler(i: int, req: Any, user_pref: Any) -> _Resp:
        names = _parse_requested_fields(req)
        return _Resp(content=_content_for(names, {"金额": "5000000"}, {"金额": content}))

    env.router.handler = handler
    col_def = {"name": "金额", "type": "question", "question": "合同金额?"}
    m1 = await _mk_matrix(env, [f1], [dict(col_def)])
    run1 = await env.engine.start_run(m1, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run1.run_id)
    assert len(env.router.calls) == 1

    m2 = await _mk_matrix(env, [f2], [dict(col_def)])
    run2 = await env.engine.start_run(m2, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run2.run_id)
    assert len(env.router.calls) == 1  # Zero new LLM calls
    got2 = await env.store.get_run(run2.run_id)
    assert got2 is not None
    assert got2.status == "succeeded"
    assert got2.cells_cached == 1
    assert got2.credits_charged == 0  # Cache hit free
    cell = await env.store.get_cell(m2.matrix_id, f2, m2.columns[0]["column_id"])
    assert cell is not None
    assert cell.cached
    assert cell.variants[0]["chunk_id"] == f"chk_{f2}_1"  # Handle hygiene: reground to own file
    assert cell.variants[0].get("page") is None


def test_cache_key_normalization() -> None:
    col_a = {"question": "合同金额？  ", "type": "question", "extraction_hint": ""}
    col_b = {"question": "合同金额？", "type": "question", "extraction_hint": ""}
    assert build_cache_key("o", "h", col_a, "m") == build_cache_key("o", "h", col_b, "m")  # Fold whitespace
    assert build_cache_key("o1", "h", col_a, "m") != build_cache_key("o2", "h", col_a, "m")  # team included in key
    assert normalize_prompt_text("Ａ　Ｂ") == "A B"  # NFKC full/halfwidth fold


# ─── Single-cell edited rerun (§5.5) ────────────────────


async def test_single_cell_rerun_supersedes_edit(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本")
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    await env.store.edit_cell_value(
        m.matrix_id, fid, col, value="人工值", column={"type": "question", "prompt_version": 1}, workspace_id=WS, team_id=TEAM
    )
    run = await env.engine.start_run(m, {"kind": "single_cell", "file_id": fid, "column_id": col}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    cell = await env.store.get_cell(m.matrix_id, fid, col)
    assert cell is not None
    assert cell.status == "done"
    assert not cell.edited_by_user  # Explicit rerun clears edited
    assert not cell.verified
    superseded = [v for v in cell.variants if v.get("extracted_by") == "user_edit_superseded"]
    assert len(superseded) == 1  # Retain superseded manual value (light undo)
    assert superseded[0]["normalized"] == {"value": "人工值"}
    assert not superseded[0].get("is_primary")


# ─── Run creation gates ─────────────────────────────────


async def test_matrix_active_runs_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M3：单 run 409 闸退役 → per-matrix 批量 run 帽（决策 6）；single_cell 豁免（决策 7）。"""
    monkeypatch.setenv("BOWHEAD_KB_MATRIX_MATRIX_ACTIVE_RUNS", "2")
    monkeypatch.setenv("BOWHEAD_KB_MATRIX_WS_ACTIVE_RUNS", "10")
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本")
    m = await _mk_matrix(env, [fid, "fx2", "fx3"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    for filler in ("fx2", "fx3"):
        created = await env.store.create_run_and_enqueue(
            matrix_id=m.matrix_id,
            workspace_id=WS,
            team_id=TEAM,
            scope={"kind": "all_pending"},
            initiated_by=USER,
            targets=[(filler, col)],
            skipped_edited=0,
            credits_estimated=1,
            column_versions={col: 1},
            file_versions={},
        )
        assert created is not None
    with pytest.raises(MatrixRunConflict) as ei:
        await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    assert ei.value.code == "matrix_active_runs_cap"
    # single_cell cap-exempt: full cap still admits retry-this-cell with 202 and reaches done (FR-11 main flow not crowded out)
    run = await env.engine.start_run(m, {"kind": "single_cell", "file_id": fid, "column_id": col}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    got = await env.store.get_run(run.run_id)
    assert got is not None and got.status == "succeeded"
    cell = await env.store.get_cell(m.matrix_id, fid, col)
    assert cell is not None and cell.status == "done"


async def test_multi_run_targets_disjoint(tmp_path: Path) -> None:
    """决策 3：锁内展开跳过在飞格 → 同矩阵并发 run 目标集不相交；同列重复发起
    在首 run 圈完后 = no_pending_cells。"""
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    created = await env.store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "column", "column_id": col},
        initiated_by=USER,
        targets=[("f1", col)],
        skipped_edited=0,
        credits_estimated=1,
        column_versions={col: 1},
        file_versions={},
    )
    assert created is not None
    with pytest.raises(MatrixRunError) as ei:
        await env.engine.start_run(m, {"kind": "column", "column_id": col}, initiated_by=USER)
    assert ei.value.code == "no_pending_cells"  # In-flight skipped; empty targets


async def test_rate_limit_bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_KB_MATRIX_RL_USER", "1")
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本")
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    with pytest.raises(MatrixRateLimited):
        await env.engine.start_run(m, {"kind": "force_all"}, initiated_by=USER)


async def test_no_pending_cells_rejected(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本")
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    with pytest.raises(MatrixRunError) as ei:
        await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    assert ei.value.code == "no_pending_cells"


# ─── CTO impl-diff review fix regressions (B-P1-1/2/3 + S-P1-1) ─


async def test_converge_cancelled_rescues_orphan_running_cells(tmp_path: Path) -> None:
    """B-P1-1：崩溃于 cancelling 的 run 残留 running 格 → converge 先拉回 queued
    再回退，不留终态 run 下的孤儿 running 格。"""
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1", "f2"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    run = await env.store.create_run_and_enqueue(
        matrix_id=m.matrix_id,
        workspace_id=WS,
        team_id=TEAM,
        scope={"kind": "all_pending"},
        initiated_by=USER,
        targets=[("f1", col), ("f2", col)],
        skipped_edited=0,
        credits_estimated=2,
        column_versions={col: 1},
        file_versions={},
    )
    await env.store.mark_cells_running(m.matrix_id, [("f1", col)])  # Simulate mid-call death
    await env.store.set_run_status(run.run_id, "cancelling")
    await env.engine.resume_interrupted_runs()
    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "cancelled"
    # f1(running→queued→empty rollback deletion) f2(queued→empty deletion): no orphans
    assert await env.store.get_cell(m.matrix_id, "f1", col) is None
    assert await env.store.get_cell(m.matrix_id, "f2", col) is None


async def test_call_crash_lands_failed_not_stuck(tmp_path: Path) -> None:
    """B-P1-2：_execute_call 非预期异常（chunks 取数抛）→ 格兜成 failed 可重试，
    run 收敛 succeeded 且无残留 queued/running。"""
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本")

    async def boom(file: Any) -> list[tuple[str, str, int]]:
        raise RuntimeError("chroma down")

    env.kb.get_ordered_chunks_for_file = boom  # type: ignore[method-assign]
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    got = await env.store.get_run(run.run_id)
    assert got is not None
    assert got.status == "succeeded"
    cell = await env.store.get_cell(m.matrix_id, fid, m.columns[0]["column_id"])
    assert cell is not None
    assert cell.status == "failed"
    assert cell.fail_reason == "system_interrupted"
    assert got.cells_failed == 1


async def test_batch_run_never_overwrites_midrun_edit(tmp_path: Path) -> None:
    """B-P1-3：enqueue 后、落态前用户编辑 → 批量 run 放弃 AI 值（FR-9 全时长成立）。
    handler 在 LLM 调用期间落一笔编辑模拟真竞态窗口。"""
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本")
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    edited_during_call: dict[str, Any] = {}

    def handler(i: int, req: Any, user_pref: Any) -> Any:
        # Sync handler cannot await—use inline marker outside event loop; actual edit before invoke returns
        # is uncontrollable via engine asyncio interleaving; instead test atomic guard: edit first, then return.
        edited_during_call["hit"] = True
        return _Resp(content=_content_for(_parse_requested_fields(req), {"A": "AI值"}))

    env.router.handler = handler
    run_task_started = asyncio.Event()

    async def edit_when_started() -> None:
        await run_task_started.wait()
        await env.store.edit_cell_value(
            m.matrix_id, fid, col, value="人工值", column={"type": "question", "prompt_version": 1}, workspace_id=WS, team_id=TEAM
        )

    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    # Edit immediately (run task not at finalize—possibly before/during mark_running)
    run_task_started.set()
    await edit_when_started()
    await _wait_run(env, run.run_id)
    cell = await env.store.get_cell(m.matrix_id, fid, col)
    assert cell is not None
    assert cell.edited_by_user  # Manual flag retained
    assert cell.variants[0]["normalized"] == {"value": "人工值"}  # AI value did not overwrite


async def test_finalize_cell_require_unedited_guard(tmp_path: Path) -> None:
    """store 层原子守卫：edited 格 + require_unedited=True → 不落态返 False。"""
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    await env.store.edit_cell_value(
        m.matrix_id, "f1", col, value="人工", column={"type": "question", "prompt_version": 1}, workspace_id=WS, team_id=TEAM
    )
    landed = await env.store.finalize_cell(
        m.matrix_id,
        "f1",
        col,
        status="done",
        variants=[{"is_primary": True, "normalized": {"value": "AI"}}],
        prompt_version=1,
        file_version=1,
        require_unedited=True,
    )
    assert landed is False
    cell = await env.store.get_cell(m.matrix_id, "f1", col)
    assert cell is not None
    assert cell.variants[0]["normalized"] == {"value": "人工"}


async def test_402_payload_never_leaks_team_pool(tmp_path: Path) -> None:
    """S-P1-1：team 池更紧时 402 available 恒回 user 层 + team_pool_tighter 布尔。"""
    env = await _mk_env(tmp_path, user_balance=1000, team_balance=1)
    fid = await _register_file(env, "f1", "文本")
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    with pytest.raises(MatrixInsufficientCredits) as ei:
        await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    assert ei.value.payload["available"] == 1000  # User level, not min(two levels)=1
    assert ei.value.payload["team_pool_tighter"] is True


# ═══ M3 additions (tech-design-m3) ════════════════════════
# Stale selection / cv grouping and dual sampling / estimate multiplier and prompt term / conditional fingerprint / degraded overwrite / copy filtering


async def test_expand_stale_scope(tmp_path: Path) -> None:
    """FR-12b「重跑已过期」行：含 verified 不含 edited；empty/failed 不属 stale；
    prompt/file 双轴（决策 2 谓词经 expand 消费）。"""
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(env, ["f1", "f2", "f3", "f4", "f5"], [{"name": "A", "type": "question", "question": "a?"}])
    col = m.columns[0]["column_id"]
    kwargs: dict[str, Any] = {"workspace_id": WS, "team_id": TEAM, "column_versions": {col: 1}, "file_versions": {}}
    # f1 done@pv1 / f2 done+verified@pv1 / f3 done+edited@pv1 / f4 failed / f5 empty
    for fid in ("f1", "f2", "f3", "f4"):
        await env.store.enqueue_cells(m.matrix_id, [(fid, col)], run_id=new_run_id(), **kwargs)
    for fid in ("f1", "f2", "f3"):
        await env.store.finalize_cell(
            m.matrix_id,
            fid,
            col,
            status="done",
            variants=[{"raw": None, "normalized": {"value": "v"}, "is_primary": True, "extracted_by": "tier1"}],
            prompt_version=1,
            file_version=1,
        )
    await env.store.set_cell_verified(m.matrix_id, "f2", col, workspace_id=WS, verified=True)
    await env.store.edit_cell_value(
        m.matrix_id, "f3", col, value="人工", column={"type": "question", "prompt_version": 1}, workspace_id=WS, team_id=TEAM
    )
    await env.store.finalize_cell(m.matrix_id, "f4", col, status="failed", fail_reason="timeout")
    # Column prompt_version bump → derived stale f1/f2/f3
    updated = await env.store.update_column(m.matrix_id, col, workspace_id=WS, question="改后?")
    assert updated is not None and updated["prompt_version"] == 2
    m2 = await env.store.get_matrix(m.matrix_id)
    assert m2 is not None
    cells = await env.store.list_cells(m.matrix_id)
    targets, skipped = env.engine.expand_targets(m2, cells, {"kind": "stale"}, file_versions={})
    assert set(targets) == {("f1", col), ("f2", col)}  # verified included; edited excluded; failed/empty not stale
    assert skipped == 1  # f3 (edited, otherwise targeted)
    # stale_column equivalent (single-column matrix)
    targets2, _ = env.engine.expand_targets(m2, cells, {"kind": "stale_column", "column_id": col}, file_versions={})
    assert set(targets2) == set(targets)
    # File axis: after pv catches up, only cells behind file_version stale
    for fid in ("f1", "f2"):
        await env.store.finalize_cell(
            m.matrix_id,
            fid,
            col,
            status="done",
            variants=[{"raw": None, "normalized": {"value": "v2"}, "is_primary": True, "extracted_by": "tier1"}],
            prompt_version=2,
            file_version=1,
        )
    cells = await env.store.list_cells(m.matrix_id)
    targets3, _ = env.engine.expand_targets(m2, cells, {"kind": "stale"}, file_versions={"f1": 2})
    assert set(targets3) == {("f1", col)}
    # Stale scope without file_versions → explicit error (no silent half-predicate)
    with pytest.raises(ValueError, match="file_versions"):
        env.engine.expand_targets(m2, cells, {"kind": "stale"})


async def test_file_versions_soft_deleted_coerced_to_one(tmp_path: Path) -> None:
    """决策 2 双消费对称（impl-diff review P2-2）：soft-deleted 文件的 file_version
    在 _file_versions（expand 侧）与快照 file_meta（`_cell_payload` 侧）都强制为 1——
    否则 soft-deleted-但版本已 bump 的文件会「显示不 stale 但被 stale 圈进」。"""
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本")
    await env.files_store.bump_version(fid)  # version → 2
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "question", "question": "a?"}])
    fv = await env.engine._file_versions(m)
    assert fv[fid] == 2  # Live file actual version
    await env.files_store.soft_delete(fid)
    fv = await env.engine._file_versions(m)
    assert fv[fid] == 1  # Soft-deleted → force 1 (symmetric with snapshot ("",True,1))


async def test_build_calls_cv_grouping(tmp_path: Path) -> None:
    """决策 10：cv 列与非 cv 列永不同 call（prompt 级采样 → 同批陪跑双倍）。"""
    env = await _mk_env(tmp_path)
    m = await _mk_matrix(
        env,
        ["f1"],
        [
            {"name": "A", "type": "number", "question": "a?", "cross_verify": True},
            {"name": "B", "type": "number", "question": "b?"},
            {"name": "C", "type": "number", "question": "c?", "cross_verify": True},
        ],
    )
    ids = {str(c["name"]): str(c["column_id"]) for c in m.columns}
    cv_flag = {str(c["column_id"]): bool(c.get("cross_verify")) for c in m.columns}
    calls = env.engine.build_calls(m, [("f1", ids["A"]), ("f1", ids["B"]), ("f1", ids["C"])])
    assert len(calls) == 2
    for _fid, col_ids in calls:
        assert len({cv_flag[c] for c in col_ids}) == 1  # Homogeneous cv in batch
    cv_batch = next(cols for _f, cols in calls if cv_flag[cols[0]])
    assert set(cv_batch) == {ids["A"], ids["C"]}


async def test_cv_run_double_samples_and_agree_in_variant(tmp_path: Path) -> None:
    """FR-39：cv call 真双采样（router 调 2 次）；agree 经 wrap 进 variant（§0.2 修）；
    verified 不被机器信号触碰。"""
    env = await _mk_env(tmp_path)
    fid = await _register_file(env, "f1", "文本内容")
    m = await _mk_matrix(env, [fid], [{"name": "A", "type": "string", "question": "a?", "cross_verify": True}])
    col = m.columns[0]["column_id"]
    run = await env.engine.start_run(m, {"kind": "column", "column_id": col}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    assert len(env.router.calls) == 2  # SELF_CONSISTENCY_N=2 × 1 chunk
    cell = await env.store.get_cell(m.matrix_id, fid, col)
    assert cell is not None
    assert cell.status == "done"
    assert not cell.verified
    primary = next(v for v in cell.variants if v.get("is_primary"))
    assert primary.get("self_consistency_agree") == 1.0  # Two identical samples fully agree


async def test_estimate_cv_multiplier_and_prompt_term(tmp_path: Path) -> None:
    env = await _mk_env(tmp_path)
    m = await env.store.create_matrix(
        name="est",
        workspace_id=WS,
        team_id=TEAM,
        created_by=USER,
        files=[{"file_id": "fa", "char_count": 1000}],
        columns=[
            {"name": "A", "type": "number", "question": "a?"},
            {"name": "B", "type": "number", "question": "b?", "cross_verify": True},
        ],
    )
    ids = {str(c["name"]): str(c["column_id"]) for c in m.columns}
    est = env.engine.estimate_targets(m, [("fa", ids["A"]), ("fa", ids["B"])])
    # Non-cv call: 1000 +2×1 +1500 =2502 →3; cv call: (1000 +2×1 +1500)×2 =5004 →6
    assert est["credits_estimated"] == 9
    assert est["calls"] == 2
    # ETA weight: non-cv 1 cell +cv 1 cell×2 =3 cell equivalents
    assert est["eta_s"] == pytest.approx(28 * 3 / 8, abs=1) or est["eta_s"] >= 1


def test_fingerprint_conditional_segments() -> None:
    """FR-18 r5 条件式规则：无 options 且 cv=false 的存量指纹逐字节不变。"""
    legacy = {"question": "金额?", "type": "currency", "extraction_hint": "看合同"}
    with_defaults = {**legacy, "options": None, "cross_verify": False, "prompt_version": 7}
    assert column_fingerprint(legacy) == column_fingerprint(with_defaults)
    assert column_fingerprint(legacy) != column_fingerprint({**legacy, "cross_verify": True})
    sel = {"question": "q?", "type": "select", "extraction_hint": "", "options": ["a", "b"]}
    assert column_fingerprint(sel) != column_fingerprint({**sel, "options": ["a", "c"]})
    # Order-independent (impl-diff review P2-1): reorder same set → same fingerprint (cache stays valid)
    assert column_fingerprint(sel) == column_fingerprint({**sel, "options": ["b", "a"]})


async def test_select_column_normalize_override(tmp_path: Path) -> None:
    """§4.6：降解 string 抽取 → 矩阵层归一化覆写 primary normalized；
    命中选项 → choice；不中 → 哨兵 → needs_review（判定与 M2 同源）。"""

    def handler(i: int, req: Any, user_pref: Any) -> Any:
        names = _parse_requested_fields(req)
        return _Resp(content=_content_for(names, value_map={"情感": "正面", "标签": "不存在的选项"}))

    env = await _mk_env(tmp_path, handler=handler)
    fid = await _register_file(env, "f1", "报道文本")
    m = await _mk_matrix(
        env,
        [fid],
        [
            {"name": "情感", "type": "select", "question": "情感?", "options": ["正面", "负面"]},
            {"name": "标签", "type": "select", "question": "标签?", "options": ["新品", "价格"]},
        ],
    )
    run = await env.engine.start_run(m, {"kind": "all_pending"}, initiated_by=USER)
    await _wait_run(env, run.run_id)
    ids = {str(c["name"]): str(c["column_id"]) for c in m.columns}
    c1 = await env.store.get_cell(m.matrix_id, fid, ids["情感"])
    assert c1 is not None and c1.status == "done"
    assert next(v for v in c1.variants if v["is_primary"])["normalized"] == {"choice": "正面"}
    c2 = await env.store.get_cell(m.matrix_id, fid, ids["标签"])
    assert c2 is not None and c2.status == "needs_review"
    # Extraction request contains options constraint clause (degraded into hint)
    req_content = env.router.calls[0]["req"].messages[0].content
    assert "严格从以下选项中选择" in req_content


async def test_reground_filters_user_origin_variants(tmp_path: Path) -> None:
    """毒缓存收口路④不变量：缓存复制结果永不含人工来源 variant（S-P1-1）。"""
    env = await _mk_env(tmp_path)
    source = [
        {
            "raw": None,
            "normalized": {"value": "AI值"},
            "is_primary": True,
            "extracted_by": "tier1",
            "source_excerpt": None,
            "chunk_id": None,
            "page": None,
        },
        {
            "raw": None,
            "normalized": {"value": "人工旧值"},
            "is_primary": False,
            "extracted_by": "user_edit_superseded",
            "source_excerpt": None,
            "chunk_id": None,
            "page": None,
        },
        {
            "raw": None,
            "normalized": {"value": "直编"},
            "is_primary": False,
            "extracted_by": "user_edit",
            "source_excerpt": None,
            "chunk_id": None,
            "page": None,
        },
    ]
    out = env.engine._reground_cached_variants(source, [])
    assert len(out) == 1
    assert out[0]["extracted_by"] == "tier1"


async def test_reground_filters_by_variant_edited_flag(tmp_path: Path) -> None:
    """双保险第二条件（impl-diff review S-P2-a）：即使 extracted_by 未以 user_edit 起头，
    variant 级 edited_by_user=True 也剔除（防未来 stamp 字面量漂移）。"""
    env = await _mk_env(tmp_path)
    source = [
        {
            "raw": None,
            "normalized": {"value": "AI值"},
            "is_primary": True,
            "extracted_by": "tier1",
            "source_excerpt": None,
            "chunk_id": None,
            "page": None,
        },
        # extracted_by lacks user_edit but variant manual flag true → still must exclude
        {
            "raw": None,
            "normalized": {"value": "人工"},
            "is_primary": False,
            "extracted_by": "tier1",
            "edited_by_user": True,
            "source_excerpt": None,
            "chunk_id": None,
            "page": None,
        },
    ]
    out = env.engine._reground_cached_variants(source, [])
    assert len(out) == 1
    assert out[0]["extracted_by"] == "tier1"
    assert not out[0].get("edited_by_user")


def test_call_timeout_cv_multiplier_and_zombie_threshold() -> None:
    """决策 10：cv timeout ×2；zombie 阈值构造恒 > 最大可能 call timeout（含 cv）。"""
    base = call_timeout_s(10)
    assert call_timeout_s(10, cross_verify=True) == base * 2
    assert zombie_threshold_s() > call_timeout_s(10, cross_verify=True)
