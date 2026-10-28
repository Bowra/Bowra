"""记忆重做 批0②③④ —— policy 收口 / 条目带 id / turn 起点快照落库。

上游：`docs/sprints/memory-spike-2026-08-29/tech-design.md` §3.2 / §4.1。
闸：`FEATURE_MEMORY_CANONICAL`（默认 0，关掉时下面每条「今天」用例都必须还成立）。

三个被修的现状，都是 116 实测过的：

- **② 运维行进了用户的记忆段**：`build_memory_pack` 调 `list_for_user`（**无 policy 过滤**），
  `summarize_for_prompt` 只 `continue` 掉 `datarc_binding` 一种 kind ⇒ `kind='tool_context'`
  的机器状态（`{"id":"1","name":"商超零售场景"}`）堂而皇之进 `<remembered_about_user>`。
- **③ 记忆条目没有身份**：模型只能用自然语言说「那条关于净利率的」，后端无从裁决取代。
- **④ 快照落库是死码**：`_maybe_save_pack_snapshot` 全仓零生产调用，且被
  `BOWHEAD_REPLAY_ENABLED`/`_PCT`（默认 0/0、116 两个 key 都没有）挡死 ⇒ replay 面恒 0 行。
"""

from __future__ import annotations

import asyncio

import pytest
from bowhead_core.memory_pack import build_memory_pack
from bowhead_core.user_memory import UserMemoryStore


@pytest.fixture
async def seeded(tmp_path):
    """一份混着四类行的记忆库 —— 真实存量的最小复刻。"""
    store = UserMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    unit = await store.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="kpi_money_unit",
        value="百万",
    )
    rule = await store.upsert_product_memory(
        user_id="u1",
        kind="constraint",
        key="report_order",
        value="先结论后依据",
    )
    # Operational row: BI-context snapshot, machine state rather than user memory.
    await store.set_tool_context("u1", "bi", context_id="1", context_name="商超零售场景")
    # Unclassified existing genuine memory (the policy restriction will drop it; see the test below).
    legacy = await store.upsert(user_id="u1", kind="preference", key="legacy_pref", value="老偏好")
    return {"store": store, "product": [unit, rule], "legacy": legacy}


async def _pack(store) -> str:
    return await build_memory_pack(user_id="u1", user_memory_store=store)


# ─── ② Restrict policy ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_operational_rows_are_filtered_out_when_canonical_on(seeded, monkeypatch):
    pack = await _pack(seeded["store"])
    assert "商超零售场景" not in pack
    assert "tool_context" not in pack
    # Preserve every genuine memory.
    assert "kpi_money_unit" in pack
    assert "report_order" in pack


@pytest.mark.asyncio
async def test_no_product_memory_row_is_lost_by_the_cutover(seeded, monkeypatch):
    """**product_memory 行零丢失**（tech-design §9 风险表点名的那条）。

    逐 id 对拍，不是数条数：`list_product_for_user` 认的每一条都必须出现在切换后的 pack 里。
    """
    pack = await _pack(seeded["store"])
    product_rows = await seeded["store"].list_product_for_user("u1")
    assert product_rows, "fixture 没有 product 行，断言会空转"
    for row in product_rows:
        assert row.id in pack, f"product 行 {row.id}({row.key}) 被切换丢掉了"
        assert row.key in pack


@pytest.mark.asyncio
async def test_unclassified_legacy_rows_are_dropped_by_the_cutover(seeded, monkeypatch):
    """★ 这条**不是**在庆祝一个行为，是把风险钉在代码里。

    `list_product_for_user` 要求 `memory_role=product_memory` **且**
    `memory_policy_version=CURRENT` **且** `value_schema` 过得了 policy 表。存量里没被分类
    的真记忆会被**静默丢掉** ⇒ 翻 `FEATURE_MEMORY_CANONICAL` 的前置是 tech-design §5 的
    存量分类作业跑完。谁把闸提前翻开，这条用例就是他要先解释的东西。
    """
    pack = await _pack(seeded["store"])
    assert "legacy_pref" not in pack
    assert seeded["legacy"].memory_role == "unclassified"
    ok, reason = await seeded["store"].v2_readiness()
    assert (ok, reason) == (False, "memory_policy_backfill_incomplete")


# ─── ③ Entries carry short IDs ─────────────────────────────────────


@pytest.mark.asyncio
async def test_entries_carry_a_short_id_only_when_canonical_on(seeded, monkeypatch):
    """id 是 FR3（supersedes 指哪条）/ FR9（点进去看来源）/ recall 的共同把手。"""
    unit = seeded["product"][0]
    assert unit.id.startswith("mem_")

    on = await _pack(seeded["store"])
    for row in await seeded["store"].list_product_for_user("u1"):
        assert f"[{row.id}]" in on


@pytest.mark.asyncio
async def test_dreaming_digest_rendering_is_not_touched_by_the_flag(seeded, monkeypatch):
    """`summarize_for_prompt` 也喂 Dreaming 的蒸馏 digest —— 它不该跟着记忆闸变形。"""
    from bowhead_core.user_memory import summarize_for_prompt

    entries = await seeded["store"].list_for_user("u1")
    assert "[mem_" not in summarize_for_prompt(entries)


# ─── ④ Turn-start snapshot: execute a real Turn ─────────────────────


async def _run_one_turn(tmp_path, *, canonical: bool):
    """真起一个 Orchestrator turn（scripted MockAdapter，不打网络）。

    只有真跑 turn 才能证明 `_maybe_save_pack_snapshot` **有生产调用点**——
    它此前的全部「证据」就是一个自己调自己的单测文件。
    """
    from bowhead_core.memory_pack_snapshots import MemoryPackSnapshotsStore
    from bowhead_core.session_store import SessionStore

    from bowhead_core import (
        EventBusRegistry,
        Orchestrator,
        OrchestratorConfig,
        TurnRequest,
        build_default_registry,
    )
    from bowhead_router import (
        AgentBinding,
        LLMResponse,
        MockAdapter,
        ProviderConfig,
        ProviderRegistry,
        Router,
    )

    db = tmp_path / "bowhead.db"
    from bowhead_cli.migrate import upgrade

    upgrade(f"sqlite+aiosqlite:///{db}", target="core")
    memory = UserMemoryStore(db)
    await memory.initialize()
    remembered = await memory.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="kpi_money_unit",
        value="百万",
    )
    await memory.set_tool_context("u1", "bi", context_id="1", context_name="商超零售场景")
    snapshots = MemoryPackSnapshotsStore(db)
    await snapshots.initialize()
    sessions = SessionStore(db)
    await sessions.initialize()

    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="好的。")]
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="p1", type="mock")])
    providers._adapters["p1"] = adapter
    router = Router(providers)
    for agent_type in ("lead", "analyst", "composer"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock")))

    async def _sink(_event):  # In-memory log sink: durable admission requires only a successful write.
        return True

    orch = Orchestrator(
        router=router,
        datarc=None,
        tool_registry=build_default_registry(),
        event_registry=EventBusRegistry(default_log_sink=_sink),
        config=OrchestratorConfig(max_tool_iterations=1),
        user_memory_store=memory,
        session_store=sessions,
        memory_pack_snapshots_store=snapshots,
        memory_pack_snapshots_enabled=canonical,
    )
    request = TurnRequest(
        session_id="s1",
        workspace_id="ws_a",
        user_id="u1",
        member_uid="m1",
        text="你好",
    )
    await orch.event_registry.get_or_create(request.turn_id, persist_before_live=True)
    await orch.run_turn(request)

    from bowhead_core.turn_runner import _PACK_SNAPSHOT_BG_TASKS

    if _PACK_SNAPSHOT_BG_TASKS:
        await asyncio.gather(*list(_PACK_SNAPSHOT_BG_TASKS), return_exceptions=True)
    snapshot = await snapshots.get_by_turn_for_user(turn_id=request.turn_id, user_id="u1")
    return snapshot, remembered


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_real_turn_persists_the_memory_snapshot(tmp_path, monkeypatch):
    snapshot, remembered = await _run_one_turn(tmp_path, canonical=True)

    assert snapshot is not None, "跑完一个 turn 后 memory_pack_snapshots 仍然是 0 行"
    # Persist exactly what was injected: IDs included, operational rows excluded (live / persistence / replay must match).
    assert f"[{remembered.id}]" in snapshot.content
    assert "商超零售场景" not in snapshot.content
    assert snapshot.pack_version == "canonical_v1"
    assert snapshot.workspace_id == "ws_a"
    assert snapshot.sections_breakdown["entries"] > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unready_storage_writes_no_snapshot_row(tmp_path, monkeypatch):
    """闸关 ⇒ 一行都不写。

    ★ 等价突变留档：把 `_maybe_save_pack_snapshot` 里那句
    `memory_pack_snapshots_enabled` 检查删掉，本条**不会**红——因为唯一的调用点
    （`_run_inner`）已经先被预取的 flag 挡住了。那句检查是第二道防线，
    由 `test_pack_snapshot_save_helper.py::test_noop_when_gate_closed` 直接钉住
    （删掉它那条会红，已实测）。别因为这里没红就以为那句可以删。
    """
    snapshot, _ = await _run_one_turn(tmp_path, canonical=False)
    assert snapshot is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lead_reads_the_prefetched_pack_instead_of_rebuilding(tmp_path, monkeypatch):
    """唯一裁决点：一个 turn 里 `build_memory_pack` 只许跑**一次**。

    跑两次意味着 Lead 在 turn 中途又自己建了一份——那份与落库/重放的那份可能不同
    （chat 不变量：live、持久化、重放的数据必须一致），而且每个执行体各读一次 DB。
    """
    import bowhead_core.memory_pack as mp

    calls: list[str] = []
    original = mp.build_memory_pack

    async def _spy(**kwargs):
        result = await original(**kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(mp, "build_memory_pack", _spy)
    snapshot, _ = await _run_one_turn(tmp_path, canonical=True)

    assert snapshot is not None
    assert len(calls) == 1, f"记忆包被建了 {len(calls)} 次；turn 起点预取之后 Lead 不该再建"
