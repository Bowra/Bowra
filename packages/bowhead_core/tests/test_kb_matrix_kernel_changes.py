"""kb-matrix M2 §8 内核七小改回归 UT（全部 additive，默认参数下旧路径行为不变）。

① ExtractionResult usage/effective 外传  ② extract user_pref/strict_invoke
③ KbStore.get_ordered_chunks_for_file    ④ try_deduct_credit reason + credits_summary
⑤ TeamKbRuntime 暴露 extractor/router     ⑥ files_store.bump_version 挂 worker "done"
⑦ v1_value_to_normalized 公开 re-export
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.doc_type_store import ExtractField
from bowhead_core.kb.extracted_record_store import (
    _v1_value_to_normalized,
    v1_value_to_normalized,
)
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.structured_extractor import StructuredExtractor
from bowhead_core.kb.worker import KbWorker

if TYPE_CHECKING:
    from pathlib import Path


# ─── fakes ──────────────────────────────────────────────────


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cached_tokens: int = 0


@dataclass
class _FakeResp:
    content: str = "{}"
    usage: _FakeUsage = field(default_factory=_FakeUsage)
    provider: str = "fake-provider"
    model: str = "fake-model"


class _FakeRouter:
    """可编程 fake：记录 user_pref、可注入异常（验收 1 测法：extract 全链无其他外部依赖）。"""

    def __init__(self, content: str = "{}", *, fail_times: int = 0) -> None:
        self.content = content
        self.fail_times = fail_times
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, agent_type: str, req: Any, **kw: Any) -> _FakeResp:
        self.calls.append({"agent_type": agent_type, "user_pref": kw.get("user_pref")})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("provider down")
        return _FakeResp(content=self.content)


_FIELDS = [ExtractField(name="金额", type="currency", description="合同金额")]
_LLM_OUT = json.dumps(
    {"金额": {"value": "5000000", "source_snippet": "金额为5000000元", "source_page": 1, "confidence": 0.9}},
    ensure_ascii=False,
)


# ─── ①②: extractor ─────────────────────────────────────────


async def test_extract_accumulates_usage_and_effective() -> None:
    router = _FakeRouter(content=_LLM_OUT)
    extractor = StructuredExtractor(router=router)
    result = await extractor.extract(text="合同文本：金额为5000000元", fields=_FIELDS, cross_verify_enabled=False)
    assert result.usage_input_tokens == 100
    assert result.usage_output_tokens == 50
    assert result.effective_provider == "fake-provider"
    assert result.effective_model == "fake-model"


async def test_extract_worker_path_defaults_unchanged() -> None:
    """风险② 回归：worker 路径参数缺省时 ExtractionResult 旧字段逐一相等
    （MagicMock resp 形状——存量测试同款——不得让 usage 累加炸掉）。"""
    resp = MagicMock(content=_LLM_OUT)
    router = MagicMock()
    router.invoke = AsyncMock(return_value=resp)
    extractor = StructuredExtractor(router=router)
    result = await extractor.extract(text="合同文本：金额为5000000元", fields=_FIELDS, cross_verify_enabled=False)
    assert result.fields["金额"]["value"] == 5000000
    assert result.chunks_processed == 1
    assert result.extractor_model == "qwen-plus"
    assert result.self_consistency_runs == 0
    # MagicMock attr neither int/str → defensive skip, new fields default.
    assert result.usage_input_tokens == 0
    assert result.effective_provider is None
    # Byte-identical call shape: no user_pref kwarg.
    router.invoke.assert_awaited_once()
    assert router.invoke.await_args is not None
    assert "user_pref" not in router.invoke.await_args.kwargs


async def test_extract_user_pref_passthrough() -> None:
    router = _FakeRouter(content=_LLM_OUT)
    extractor = StructuredExtractor(router=router)
    pref = object()
    await extractor.extract(text="文本", fields=_FIELDS, cross_verify_enabled=False, user_pref=pref)
    assert router.calls[0]["user_pref"] is pref


async def test_extract_strict_invoke_raises() -> None:
    router = _FakeRouter(fail_times=99)
    extractor = StructuredExtractor(router=router)
    with pytest.raises(RuntimeError, match="provider down"):
        await extractor.extract(text="文本", fields=_FIELDS, cross_verify_enabled=False, strict_invoke=True)
    # Default nonstrict still swallows into empty result, old behavior.
    router2 = _FakeRouter(fail_times=99)
    extractor2 = StructuredExtractor(router=router2)
    result = await extractor2.extract(text="文本", fields=_FIELDS, cross_verify_enabled=False)
    assert result.fields == {}


# ─── ③: get_ordered_chunks_for_file ────────────────────────


async def test_get_ordered_chunks_for_file_sorts_by_chunk_index(tmp_path: Path) -> None:
    """Spike A 约束：拼文本必须按 metadata chunk_index（1-based）排序，
    不能用 chromadb 存储序。乱序插入 → 断言输出有序。"""
    from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
    from bowhead_core.kb.schemas import KbFileDTO
    from bowhead_core.kb.store import KbStore

    kb_store = KbStore(str(tmp_path / "chroma"))
    file = KbFileDTO(
        file_id="kbf_ord",
        artifact_id="a",
        filename="f.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
        uploaded_at="2026-01-01T00:00:00+00:00",
    )
    # Reverse insertion (indices 3,1,2).
    chunks = []
    for idx, content in [(3, "第三段"), (1, "第一段"), (2, "第二段")]:
        chunks.append(
            KbChunk(
                chunk_id=make_chunk_id("kbf_ord", idx, content),
                file_id="kbf_ord",
                content=content,
                embedding=None,
                metadata=ChunkMetadata(
                    file_id="kbf_ord",
                    file_name="f.md",
                    scope="workspace",
                    scope_owner="ws_a",
                    workspace_id="ws_a",
                    team_id="team_a",
                    chunk_index=idx,
                    is_active=True,
                ),
            )
        )
    await kb_store.add_chunks(file, chunks)
    ordered = await kb_store.get_ordered_chunks_for_file(file)
    assert [c for _, c, _ in ordered] == ["第一段", "第二段", "第三段"]
    assert [i for _, _, i in ordered] == [1, 2, 3]
    # Tuple projection compatible with backfill_chunk_ids input (§5.3 review P2).
    two = [(cid, content) for cid, content, _ in ordered]
    assert all(isinstance(cid, str) and isinstance(content, str) for cid, content in two)


# ─── ④: try_deduct_credit reason + credits_summary ─────────


async def test_deduct_reason_lands_in_ledger(tmp_path: Path) -> None:
    import aiosqlite
    from bowhead_core.user_store import UserStore, _now

    store = UserStore(str(tmp_path / "users.db"))
    await store.initialize()
    # Direct team/user SQL seed (test_user_store_credits pattern), bypass complex create logic.
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES ('team_a', 'Test Team', 'test.com', 10, 'ultra', 1000, 1000, '{}', 'active', ?)",
            (_now(),),
        )
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES ('u_a', 'team_a', 'a@test.com', 1, '', '[]', 'ultra', 1000, 1000, ?, 'zh-CN', 'UTC', '{}', 'active', ?)",
            (_now(), _now()),
        )
        await db.commit()

    ok, _ = await store.try_deduct_credit(
        user_id="u_a",
        team_id="team_a",
        cost_credits=3,
        turn_id="mxrun_abc",
        workspace_id="ws_1",
        tokens_input=1200,
        tokens_output=300,
        reason="kb_matrix_run",
    )
    assert ok
    # Default reason unchanged, no chat-path changes.
    ok, _ = await store.try_deduct_credit(user_id="u_a", team_id="team_a", cost_credits=2, turn_id="t_x")
    assert ok

    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT reason, turn_id, delta FROM credit_ledger") as cur:
        rows = await cur.fetchall()
    assert ("kb_matrix_run", "mxrun_abc", -3) in rows
    assert ("turn", "t_x", -2) in rows

    # credits_summary 30d spend includes matrix usage (§8④).
    summary = await store.credits_summary("team_a")
    assert summary["last_30d"]["spend"] == 5
    assert summary["last_30d"]["tokens_in"] == 1200


# ─── ⑤: TeamKbRuntime fields ────────────────────────────────────


def test_team_kb_runtime_dataclass_has_new_fields() -> None:
    from dataclasses import fields as dc_fields

    from bowhead_core.team_kb_runtime import TeamKbRuntime

    names = {f.name for f in dc_fields(TeamKbRuntime)}
    assert "structured_extractor" in names
    assert "router" in names


# ─── ⑥: bump_version + actual worker done parse path ────────────


class _StubEmbedder:
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _StubChromaStore:
    def __init__(self) -> None:
        self._added: list[Any] = []

    async def add_chunks(self, file: Any, chunks: Any) -> None:
        self._added.extend(chunks)

    async def count_chunks(self, file: Any) -> int:
        return len(self._added)

    async def hard_delete_file(self, file: Any) -> int:
        n = len(self._added)
        self._added = []
        return n


async def test_parse_done_bumps_file_version(initialize_migrated_kb_files_database, tmp_path: Path) -> None:
    """验收 10：必须走真实 parse 成功路径断言（不许直调假状态值——r1 的 "ready"
    UT 会绿着掩盖生产死路）。重解析再 bump。"""
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    doc = tmp_path / "doc.md"
    doc.write_text("# 标题\n\n正文内容。", encoding="utf-8")
    file_id = await files_store.create(
        artifact_id=str(doc),
        filename="doc.md",
        mime_type="text/markdown",
        size_bytes=doc.stat().st_size,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
    )
    before = await files_store.get(file_id)
    assert before is not None
    assert before.version == 1

    worker = KbWorker(
        files_store=files_store,
        store=_StubChromaStore(),  # type: ignore[arg-type]
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
        artifact_fetcher=None,
    )
    await worker.parse_and_index(file_id)
    after = await files_store.get(file_id)
    assert after is not None
    assert after.parse_status == "done"
    assert after.version == 2  # Done transition bumps.
    # Reparse bumps again (matrix stale-derived input). Fresh stub: shared stub accumulates
    # two turns' chunks, count mismatch (real chroma idempotent upsert).
    worker2 = KbWorker(
        files_store=files_store,
        store=_StubChromaStore(),  # type: ignore[arg-type]
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
        artifact_fetcher=None,
    )
    await worker2.parse_and_index(file_id, allow_terminal=True)
    again = await files_store.get(file_id)
    assert again is not None
    assert again.parse_status == "done"
    assert again.version == 3


async def test_bump_version_atomic(initialize_migrated_kb_files_database, tmp_path: Path) -> None:
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    doc = tmp_path / "a.md"
    doc.write_text("x", encoding="utf-8")
    file_id = await files_store.create(
        artifact_id=str(doc),
        filename="a.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="workspace",
        scope_id="ws_a",
        workspace_id="ws_a",
        team_id="team_a",
        uploaded_by="u1",
    )
    await files_store.bump_version(file_id)
    await files_store.bump_version(file_id)
    got = await files_store.get(file_id)
    assert got is not None
    assert got.version == 3


# ─── ⑦: Public re-export ────────────────────────────────────────


def test_v1_value_to_normalized_public_alias() -> None:
    assert v1_value_to_normalized is _v1_value_to_normalized
    assert v1_value_to_normalized("约100元", "currency") == {"raw_string": "约100元"}
    assert v1_value_to_normalized("5000000", "currency") == {"amount": "5000000", "currency": None}
