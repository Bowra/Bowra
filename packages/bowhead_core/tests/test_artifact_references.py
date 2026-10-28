"""运行数据 TTL 的「还在用」判据 —— 引用边的并集。

改动前：判据是「有没有被 pin」，而 pin 是 `put_bytes` 在产物出生时自动打的
⇒ 恒为真 ⇒ TTL 恒回收 0 条（116 实测 3,785 条候选被这一条挡下 3,781 条）。
改动后：自动 pin 不再算引用，真实引用边（citation / 画布正文 / 用户手动 pin /
文档任务输入）才算。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.artifact_references import canvas_referenced_artifact_ids, is_user_intent_pin
from bowhead_core.artifact_store import ArtifactStore, LocalBackend

if TYPE_CHECKING:
    from pathlib import Path

_OLD = "2020-01-01T00:00:00+00:00"


@pytest.fixture
async def store(tmp_path: Path) -> ArtifactStore:
    s = ArtifactStore(db_path=str(tmp_path / "a.db"), backend=LocalBackend(root_dir=tmp_path / "blob"))
    await s.initialize()
    async with aiosqlite.connect(s.db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS session_attachments (
                session_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
                pinned_at TEXT, detached_at TEXT, PRIMARY KEY(session_id, artifact_id));
            CREATE TABLE IF NOT EXISTS turn_citations (
                turn_id TEXT, seq INTEGER, artifact_id TEXT, label TEXT, kind TEXT, registered_at TEXT);
            CREATE TABLE IF NOT EXISTS canvas_sections (
                id TEXT PRIMARY KEY, canvas_id TEXT, seq INTEGER, kind TEXT, payload TEXT);
        """)
        await db.commit()
    return s


async def _run_datum(store: ArtifactStore, name: str, *, session_id: str | None = "s_1", kind: str = "tool_data") -> str:
    """造一份「到期的运行数据」。传 session_id = 复刻 put_bytes 的**出生即自动 pin**。"""
    art = await store.put_bytes(
        content=b"a,b\n1,2\n",
        filename=name,
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
        session_id=session_id,
        kind=kind,
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET created_at=? WHERE id=?", (_OLD, art.id))
        await db.commit()
    return art.id


@pytest.mark.asyncio
async def test_auto_pin_no_longer_blocks_collection(store: ArtifactStore) -> None:
    """★ 本次改动的核心：出生时的自动 pin 不再冒充「还在用」。"""
    aid = await _run_datum(store, "auto_pinned.csv")
    async with aiosqlite.connect(store.db_path) as db:
        pinned = await (await db.execute("SELECT count(*) FROM session_attachments WHERE artifact_id=? AND detached_at IS NULL", (aid,))).fetchone()
    assert pinned[0] == 1, "前置不成立：put_bytes 没有自动 pin，这个测试证明不了任何事"
    assert await store.stale_run_data_ids(days=30) == [aid]


@pytest.mark.asyncio
async def test_user_pinned_artifact_is_kept(store: ArtifactStore) -> None:
    """用户自己 pin 的东西仍然保活——这是 pin 本来的语义，不能连它一起丢。"""
    aid = await _run_datum(store, "user_pinned.csv")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET inject='full' WHERE id=?", (aid,))
        await db.commit()
    assert await store.stale_run_data_ids(days=30) == []


@pytest.mark.asyncio
async def test_cited_artifact_is_kept(store: ArtifactStore) -> None:
    """★ 正对照：被老报告引用的数据必须**不**进候选。

    直接放开 pin 判据而不加这条边，116 实测会有 74.8% 的候选是被引用的
    ⇒ 硬删后用户打开老报告，数据引用永久 404。
    """
    aid = await _run_datum(store, "cited.csv")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO turn_citations(turn_id,seq,artifact_id) VALUES ('t_1',1,?)", (aid,))
        await db.commit()
    assert await store.stale_run_data_ids(days=30) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload_template"),
    [
        # Real shapes found by the full 116 audit; only recognizing input/output_artifact_ids misses all but the first.
        ("code_block", '{{"input_artifact_ids": ["{aid}"]}}'),
        ("sources", '{{"entries": [{{"artifact_id": "{aid}", "label": "x"}}]}}'),
        ("chart", '{{"spec": {{"data_artifact_id": "{aid}"}}}}'),
        ("chart", '{{"spec": {{"source_meta": {{"resolved_from_artifact_id": "{aid}"}}}}}}'),
        ("image", '{{"artifact_id": "{aid}"}}'),
        ("insight_card", '{{"evidence": [{{"table_artifact_id": "{aid}"}}]}}'),
        ("narrative", '{{"source_refs": ["{aid}"]}}'),
        ("design", '{{"background": {{"artifact_id": "{aid}"}}}}'),
        # Free-text mentions count too: sandbox code uses it, so deletion prevents reproduction.
        ("code_block", '{{"code": "df = pd.read_csv(INPUTS[{aid}])"}}'),
    ],
)
async def test_canvas_reference_in_any_shape_keeps_it(store: ArtifactStore, kind: str, payload_template: str) -> None:
    """★ 判据是全文扫，不是枚举 JSON 路径。

    116 全量核：`art_` id 散落在 ~20 条路径、8 种 section kind 里，只取
    input/output_artifact_ids 会漏 40%（4,328 个）。这条把各种真实形态逐个钉住。
    """
    aid = await _run_datum(store, "in_canvas.csv")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO canvas_sections(id,canvas_id,seq,kind,payload) VALUES ('cs_1','cv_1',0,?,?)",
            (kind, payload_template.format(aid=aid)),
        )
        await db.commit()
    assert await store.stale_run_data_ids(days=30) == [], f"{kind} 的 {payload_template} 形态没被认出来"


@pytest.mark.asyncio
async def test_unreferenced_stays_collectable_alongside_referenced_ones(store: ArtifactStore) -> None:
    """负对照：同一批里有引用的留下、没引用的照收 —— 否则「全留着」也能让上面全绿。"""
    kept = await _run_datum(store, "kept.csv")
    collectable = await _run_datum(store, "free.csv")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO canvas_sections(id,canvas_id,seq,kind,payload) VALUES ('cs_1','cv_1',0,'chart',?)",
            (json.dumps({"spec": {"data_artifact_id": kept}}),),
        )
        await db.commit()
    assert await store.stale_run_data_ids(days=30) == [collectable]


@pytest.mark.asyncio
async def test_not_yet_expired_is_never_a_candidate(store: ArtifactStore) -> None:
    """TTL 还是 TTL：没到期的一律不动。"""
    await _run_datum(store, "fresh.csv")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET created_at=datetime('now')")
        await db.commit()
    assert await store.stale_run_data_ids(days=30) == []


@pytest.mark.asyncio
async def test_assets_are_never_candidates(store: ArtifactStore) -> None:
    """资产（用户上传/交付物）永不自动过期——只有运行数据进 TTL。"""
    await _run_datum(store, "mine.pdf", kind="user_upload")
    await _run_datum(store, "report.pdf", kind="deliverable")
    assert await store.stale_run_data_ids(days=30) == []


@pytest.mark.asyncio
async def test_canvas_scan_skips_payloads_without_ids(store: ArtifactStore) -> None:
    """全表扫要能跳过绝大多数无关 section（GC 每晚一轮，别在这儿做无谓解析）。"""
    async with aiosqlite.connect(store.db_path) as db:
        await db.executemany(
            "INSERT INTO canvas_sections(id,canvas_id,seq,kind,payload) VALUES (?,?,?,?,?)",
            [(f"cs_{i}", "cv_1", i, "narrative", '{"markdown": "no ids here"}') for i in range(50)],
        )
        await db.execute(
            "INSERT INTO canvas_sections(id,canvas_id,seq,kind,payload) VALUES ('cs_hit','cv_1',99,'chart',?)",
            ('{"spec": {"data_artifact_id": "art_deadbeef1234"}}',),
        )
        await db.commit()
        assert await canvas_referenced_artifact_ids(db) == {"art_deadbeef1234"}


def test_user_intent_pin_predicate_matches_the_sql_side() -> None:
    """Python 判据与 SQL 判据必须同真同假（两处：composer pin 条 / TTL 保活）。"""
    from types import SimpleNamespace

    assert is_user_intent_pin(SimpleNamespace(inject="full", kind="user_upload")) is True
    assert is_user_intent_pin(SimpleNamespace(inject="list", kind="tool_data")) is False
    assert is_user_intent_pin(SimpleNamespace(inject="stub", kind="sandbox_output")) is False
    # Legacy NULL inject: fall back to kind.
    assert is_user_intent_pin(SimpleNamespace(inject=None, kind="user_upload")) is True
    assert is_user_intent_pin(SimpleNamespace(inject=None, kind="tool_data")) is False


@pytest.mark.asyncio
async def test_store_only_database_without_canvas_or_citation_tables(tmp_path: Path) -> None:
    """★ 只初始化 ArtifactStore 的库（缺 canvas_sections / turn_citations）不得崩。

    我自己的 fixture 把三张表都建了 ⇒ 只覆盖了顺利路径；这个缺口是既有的
    `test_files_p4_governance` 逮出来的（GC 直接 OperationalError）。
    表不存在 = 它一行都没有 = 这条边确实没有引用，按 documents.retention 同款推理降级。
    """
    store = ArtifactStore(db_path=str(tmp_path / "solo.db"), backend=LocalBackend(root_dir=tmp_path / "b"))
    await store.initialize()
    art = await store.put_bytes(content=b"x", filename="run.csv", mime="text/csv", source="bi_fetch_rows", created_by="u1", kind="tool_data")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET created_at=? WHERE id=?", (_OLD, art.id))
        await db.commit()
        for table in ("canvas_sections", "turn_citations"):
            got = await (await db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,))).fetchone()
            assert got is None, f"前置不成立：{table} 竟然存在，这条测试证明不了任何事"

    assert await store.stale_run_data_ids(days=30) == [art.id]


@pytest.mark.asyncio
async def test_query_failures_are_not_swallowed_as_no_references(store: ArtifactStore) -> None:
    """★ 缺表降级**只**认「表不存在」，绝不能把查询失败读成「没有引用」。

    一次锁库或损坏被吞掉 = 仍被引用的数据被送进回收站，且没有任何人会知道。
    """

    class _RaisingCursor:
        async def __aenter__(self) -> None:
            raise aiosqlite.OperationalError("database is locked")

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class _BrokenDB:
        def execute(self, _sql: str, *_args: object) -> _RaisingCursor:
            return _RaisingCursor()

    with pytest.raises(aiosqlite.OperationalError):
        await canvas_referenced_artifact_ids(_BrokenDB())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_end_to_end_ttl_actually_collects_an_auto_pinned_artifact(store: ArtifactStore) -> None:
    """★★ 穿过闸本身：列候选 **+ 真落刀**，而不是只验候选查询。

    2026-09-07 那次就栽在这儿：只改了 `stale_run_data_ids`，`expire_run_data_ids` 里的
    旧 blanket pin 排除留着 ⇒ 候选列出 1,029 条、落刀一条不放行，整个改动在生产上空转。
    候选查询单独 dry-run 全绿，因为**没穿过闸**。

    既有的 `test_files_p4_governance` 也没逮到：它的 `_make` 不传 session_id ⇒
    那套测试里从没发生过自动 pin。
    """
    aid = await _run_datum(store, "auto_pinned.csv")
    async with aiosqlite.connect(store.db_path) as db:
        pinned = await (await db.execute("SELECT count(*) FROM session_attachments WHERE artifact_id=? AND detached_at IS NULL", (aid,))).fetchone()
    assert pinned[0] == 1, "前置不成立：没发生自动 pin，这条测试证明不了任何事"

    candidates = await store.stale_run_data_ids(days=30)
    assert candidates == [aid]
    assert await store.expire_run_data_ids(candidates) == 1, "落刀这步把候选拒了——判据没同源"

    art = await store.get(aid, include_deleted=True)
    assert art is not None and art.deleted_at is not None, "应该进回收站（软删），不是硬删"


@pytest.mark.asyncio
async def test_expire_refuses_what_the_candidate_query_would_refuse(store: ArtifactStore) -> None:
    """负对照：调用方就算把「仍被引用」的 id 传进来，落刀也必须拒。

    这是 `expire_run_data_ids` 再判一次的**全部意义**——防「列候选之后新增了引用」。
    """
    cited = await _run_datum(store, "cited.csv")
    in_canvas = await _run_datum(store, "in_canvas.csv")
    free = await _run_datum(store, "free.csv")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO turn_citations(turn_id,seq,artifact_id) VALUES ('t_1',1,?)", (cited,))
        await db.execute(
            "INSERT INTO canvas_sections(id,canvas_id,seq,kind,payload) VALUES ('cs_1','cv_1',0,'chart',?)",
            (json.dumps({"spec": {"data_artifact_id": in_canvas}}),),
        )
        await db.commit()

    # Force all three ids in (simulate references added after candidate lookup).
    assert await store.expire_run_data_ids([cited, in_canvas, free]) == 1
    assert (await store.get(cited)).deleted_at is None
    assert (await store.get(in_canvas)).deleted_at is None
    assert (await store.get(free, include_deleted=True)).deleted_at is not None
