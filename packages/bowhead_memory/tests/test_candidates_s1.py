"""memory-revive S1 · candidates 强化：workspace_id 列迁移 / enqueue_if_new
rejected·approved 抑制 / DreamingLight 走 enqueue_if_new + 存量记忆入 prompt。

对应 discovery §6.2 的三层去重缺口——每条测试锚一个缺口，防回退。
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from bowhead_memory.candidates import (
    ENQUEUE_CREATED,
    ENQUEUE_DUPLICATE_PENDING,
    ENQUEUE_SUPPRESSED,
    ORIGIN_FEEDBACK_DRAFT,
    DreamingLight,
    DreamObservation,
    MemoryCandidateStore,
    _backfill_candidate_suppressions,
    _redact_nonrecoverable_candidates,
    _suppression_scope_key,
)


@pytest.fixture
async def store(tmp_path):
    s = MemoryCandidateStore(db_path=str(tmp_path / "cand.db"))
    await s.initialize()
    return s


class _JsonRouter:
    """Fake router：固定返回同一批候选，记录每次收到的 prompt。"""

    def __init__(self, candidates: list[dict]) -> None:
        self.prompts: list[str] = []
        self._payload = json.dumps({"candidates": candidates}, ensure_ascii=False)

    async def invoke(self, agent_type, req):
        self.prompts.append(req.messages[0].content)
        payload = self._payload

        class _Resp:
            content = payload

        return _Resp()


# ─── workspace_id column: migration + write/read round trip ───────


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_table_without_workspace_column(tmp_path):
    """老库（无 workspace_id 列）上 initialize 必须 ALTER 补列而不是炸。"""
    db_path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE memory_candidates ("
            "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
            "subject_type TEXT NOT NULL DEFAULT 'user', kind TEXT NOT NULL, "
            "key TEXT NOT NULL, value_json TEXT NOT NULL, reason TEXT, "
            "source_turn_id TEXT, status TEXT NOT NULL DEFAULT 'pending', "
            "decided_by TEXT, decided_at TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT INTO memory_candidates(id, user_id, kind, key, value_json, created_at, updated_at) "
            "VALUES ('cand_old1', 'u1', 'preference', 'k', '\"v\"', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        await db.commit()

    s = MemoryCandidateStore(db_path=db_path)
    await s.initialize()
    old = await s.get("cand_old1")
    assert old is not None and old.workspace_id is None

    c = await s.enqueue(
        user_id="u1",
        kind="preference",
        key="k2",
        value="v2",
        reason=None,
        source_turn_id=None,
        workspace_id="ws_a",
    )
    got = await s.get(c.id)
    assert got is not None and got.workspace_id == "ws_a"


@pytest.mark.asyncio
async def test_list_for_user_source_turn_id_filter(store):
    await store.enqueue(user_id="u1", kind="note", key="", value="a", reason=None, source_turn_id="t_1")
    await store.enqueue(user_id="u1", kind="note", key="", value="b", reason=None, source_turn_id="t_2")
    only_t1 = await store.list_for_user(user_id="u1", status="pending", source_turn_id="t_1")
    assert [c.value for c in only_t1] == ["a"]


# ─── enqueue_if_new: decided suppression (discovery §6.2 gap 2) ────


@pytest.mark.asyncio
async def test_enqueue_if_new_suppressed_after_reject(store):
    """用户拒绝过的同值候选，抑制窗口内不得原样卷土重来。"""
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="月度口径", reason=None, source_turn_id="t_1")
    assert c is not None
    await store.set_decision(cid=c.id, status="rejected", decided_by="u1")

    again = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="月度口径", reason=None, source_turn_id="t_2")
    assert again is None


@pytest.mark.asyncio
async def test_enqueue_if_new_suppressed_after_approve(store):
    """已批准入库的同值也无需再提。"""
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="环比优先", reason=None, source_turn_id="t_1")
    assert c is not None
    await store.set_decision(cid=c.id, status="approved", decided_by="u1")

    again = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="环比优先", reason=None, source_turn_id="t_2")
    assert again is None


@pytest.mark.asyncio
async def test_enqueue_if_new_allows_after_suppress_window(store, tmp_path):
    """decided 抑制过窗（30d）后允许重提——backdate decided_at 验证窗口边界。"""
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="旧偏好", reason=None, source_turn_id="t_1")
    assert c is not None
    await store.set_decision(cid=c.id, status="rejected", decided_by="u1")
    async with aiosqlite.connect(str(tmp_path / "cand.db")) as db:
        await db.execute(
            "UPDATE memory_candidates SET decided_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (c.id,),
        )
        await db.execute(
            """
            UPDATE memory_candidate_suppressions
            SET expires_at='2020-01-31T00:00:00+00:00'
            WHERE user_id='u1'
            """,
        )
        await db.commit()

    again = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="旧偏好", reason=None, source_turn_id="t_2")
    assert again is not None


@pytest.mark.asyncio
async def test_enqueue_if_new_different_value_not_suppressed(store):
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="月度口径", reason=None, source_turn_id="t_1")
    await store.set_decision(cid=c.id, status="rejected", decided_by="u1")

    other = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="周度口径", reason=None, source_turn_id="t_2")
    assert other is not None


# ─── DreamingLight.observe: enqueue_if_new + existing memories in the prompt (gaps 1/3) ──


def _obs(**overrides) -> DreamObservation:
    base: dict = {
        "turn_id": "t_1",
        "user_id": "u1",
        "user_text": "我更喜欢看环比",
        "final_answer": "好的",
        "intent": "exploration",
        "tool_chain": ["bi_query"],
    }
    base.update(overrides)
    return DreamObservation(**base)


@pytest.mark.asyncio
async def test_observe_dedupes_across_turns(store):
    """同值候选跨 turn 只入队一次——observe 必须走 enqueue_if_new（缺口 1）。"""
    router = _JsonRouter([{"kind": "preference", "key": "prefer_qoq", "value": "QoQ", "reason": "said so"}])
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)

    first = await light.observe(_obs(turn_id="t_1"))
    second = await light.observe(_obs(turn_id="t_2"))
    assert len(first) == 1
    assert second == []
    all_pending = await store.list_for_user(user_id="u1", status="pending")
    assert len(all_pending) == 1


@pytest.mark.asyncio
async def test_observe_not_reproposed_after_reject(store):
    """observe 提议 → 用户拒绝 → 下一 turn 同值不再入队（缺口 2 走通 observe 全路径）。"""
    router = _JsonRouter([{"kind": "preference", "key": "prefer_qoq", "value": "QoQ", "reason": "said so"}])
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)

    first = await light.observe(_obs(turn_id="t_1"))
    await store.set_decision(cid=first[0].id, status="rejected", decided_by="u1")
    second = await light.observe(_obs(turn_id="t_2"))
    assert second == []


@pytest.mark.asyncio
async def test_observe_existing_memory_reaches_prompt(store):
    """existing_memory 摘要必须进 LLM prompt（缺口 3：语义去重靠蒸馏侧）。"""
    router = _JsonRouter([])
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)

    await light.observe(_obs(existing_memory="- [preference] prefer_qoq: QoQ"))
    assert len(router.prompts) == 1
    assert "prefer_qoq" in router.prompts[0]
    assert "勿重复提议" in router.prompts[0]


@pytest.mark.asyncio
async def test_observe_empty_existing_memory_omits_block(store):
    router = _JsonRouter([])
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)

    await light.observe(_obs(existing_memory=""))
    assert "已有记忆" not in router.prompts[0]


@pytest.mark.asyncio
async def test_observe_threads_workspace_id(store):
    router = _JsonRouter([{"kind": "note", "key": "", "value": "老板叫张总", "reason": "r"}])
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)

    created = await light.observe(_obs(workspace_id="ws_retail"))
    assert created[0].workspace_id == "ws_retail"
    got = await store.get(created[0].id)
    assert got is not None and got.workspace_id == "ws_retail"


# ─── review-fix batch (three-perspective review on 2026-07-22) ──────


@pytest.mark.asyncio
async def test_system_auto_reject_does_not_suppress(store):
    """F2: GC 过期清理(system_auto_reject)不是用户决策——不得抑制重提。"""
    c = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="月度口径", reason=None, source_turn_id="t_1")
    await store.set_decision(cid=c.id, status="rejected", decided_by="system_auto_reject")

    again = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value="月度口径", reason=None, source_turn_id="t_2")
    assert again is not None  # The user never saw it; allow it to be proposed again.


@pytest.mark.asyncio
async def test_dict_value_dedupe_key_order_insensitive(store):
    """F3: 落库序列化与去重指纹同源(sort_keys)——对象值键序不同也要命中去重。"""
    first = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value={"b": 1, "a": 2}, reason=None, source_turn_id="t_1")
    assert first is not None
    dup = await store.enqueue_if_new(user_id="u1", kind="preference", key="k", value={"a": 2, "b": 1}, reason=None, source_turn_id="t_2")
    assert dup is None


@pytest.mark.asyncio
async def test_enqueue_rejects_oversized_value(store):
    """F8: 共享原语防御纵深——超限值拒收。"""
    with pytest.raises(ValueError):
        await store.enqueue(user_id="u1", kind="note", key="", value="x" * 3000, reason=None, source_turn_id=None)
    with pytest.raises(ValueError):
        await store.enqueue(user_id="u1", kind="preference", key="k" * 200, value="v", reason=None, source_turn_id=None)


@pytest.mark.asyncio
async def test_observe_skips_oversized_candidate_keeps_rest(store):
    """F8: 单候选超限跳过,同批其余候选照常入队。"""
    router = _JsonRouter(
        [
            {"kind": "note", "key": "", "value": "x" * 3000, "reason": "too big"},
            {"kind": "preference", "key": "ok_key", "value": "正常值", "reason": "fine"},
        ]
    )
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)
    created = await light.observe(_obs())
    assert len(created) == 1 and created[0].key == "ok_key"


@pytest.mark.asyncio
async def test_observe_rate_limited_per_user(store):
    """F5: per-user 冷却——间隔内第二次 observe 不打 LLM。"""
    router = _JsonRouter([])
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=3600.0)
    await light.observe(_obs(turn_id="t_1"))
    await light.observe(_obs(turn_id="t_2"))
    assert len(router.prompts) == 1  # The cooldown blocks the second attempt.


@pytest.mark.asyncio
async def test_observe_daily_cap(store):
    """F5: 日上限——超限后不再打 LLM。"""
    router = _JsonRouter([])
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0.0, daily_cap=2)
    await light.observe(_obs(turn_id="t_1"))
    await light.observe(_obs(turn_id="t_2"))
    await light.observe(_obs(turn_id="t_3"))
    assert len(router.prompts) == 2


@pytest.mark.asyncio
async def test_list_users_with_stale_pending(store, tmp_path):
    c = await store.enqueue(user_id="u_stale", kind="note", key="", value="旧", reason=None, source_turn_id=None)
    async with aiosqlite.connect(str(tmp_path / "cand.db")) as db:
        await db.execute(
            "UPDATE memory_candidates SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (c.id,),
        )
        await db.commit()
    await store.enqueue(user_id="u_fresh", kind="note", key="", value="新", reason=None, source_turn_id=None)
    assert await store.list_users_with_stale_pending(max_age_days=30) == ["u_stale"]


@pytest.mark.asyncio
async def test_enqueue_outcome_says_why_nothing_was_queued(store):
    """清单 ④：None 有三种来由，第二个返回值必须把它们分开——
    判重命中的那张卡**存在**，被抑制的**不存在**，调用方要据此决定能不能说「去确认一下」。"""
    kw = {"user_id": "u1", "kind": "preference", "key": "k", "value": "月度口径", "reason": None}
    c, o1 = await store.enqueue_if_new_with_outcome(**kw, source_turn_id="t_1")
    assert c is not None and o1 == ENQUEUE_CREATED

    dup, o2 = await store.enqueue_if_new_with_outcome(**kw, source_turn_id="t_2")
    assert dup is None and o2 == ENQUEUE_DUPLICATE_PENDING

    await store.set_decision(cid=c.id, status="rejected", decided_by="u1")
    sup, o3 = await store.enqueue_if_new_with_outcome(**kw, source_turn_id="t_3")
    assert sup is None and o3 == ENQUEUE_SUPPRESSED

    # The thin wrapper preserves behavior for callers that do not need the reason.
    assert await store.enqueue_if_new(**kw, source_turn_id="t_4") is None


# ─── Suppression backfill scope keys (bare and prefixed keys found together on 116 on 2026-09-15) ──


async def _wipe_suppressions(store, *, then_backfill: bool):
    """只留下「用户做过裁决」这个事实，隔离出**回填**这一条路。"""
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DELETE FROM memory_candidate_suppressions")
        if then_backfill:
            await _backfill_candidate_suppressions(db)
        await db.commit()


@pytest.mark.asyncio
async def test_a_rejection_the_backfill_recovers_actually_blocks_the_next_proposal(store):
    """★★ 用户在抑制表出现**之前**点过的「不要」靠 `initialize` 里的回填补进来。
    但回填写的 `source_scope_key` 是裸的工作区 id，读侧用的是 `{origin}:{工作区}`
    ⇒ 补进来的每一行都读不出来：那条建议照旧再提，而且是**静默**的。

    钉的是用户可见的契约（「拒过的不再提」），不是字段相等。
    ★ 突变验证：把回填的 key 改回 `COALESCE(workspace_id,'__no_space__')` ⇒ 本用例红。
    """
    kw = {"user_id": "u1", "kind": "preference", "key": "k", "value": "月度口径", "reason": None, "workspace_id": "ws_1"}
    first = await store.enqueue_if_new(**kw, source_turn_id="t_1")
    await store.set_decision(cid=first.id, status="rejected", decided_by="u1")

    await _wipe_suppressions(store, then_backfill=True)
    again, outcome = await store.enqueue_if_new_with_outcome(**kw, source_turn_id="t_2")
    assert again is None and outcome == ENQUEUE_SUPPRESSED, "回填出来的抑制必须真的挡得住"


@pytest.mark.asyncio
async def test_without_the_backfill_the_same_thing_is_proposed_again(store):
    """负对照：同样抹掉抑制表但**不跑回填** ⇒ 照旧再提。
    没有它，上一条在「什么都挡」的实现下也会绿。"""
    kw = {"user_id": "u2", "kind": "preference", "key": "k", "value": "月度口径", "reason": None, "workspace_id": "ws_1"}
    first = await store.enqueue_if_new(**kw, source_turn_id="t_1")
    await store.set_decision(cid=first.id, status="rejected", decided_by="u2")

    await _wipe_suppressions(store, then_backfill=False)
    again, outcome = await store.enqueue_if_new_with_outcome(**kw, source_turn_id="t_2")
    assert again is not None and outcome == ENQUEUE_CREATED


@pytest.mark.asyncio
async def test_the_draft_pool_is_never_backfilled_into_a_suppression(store):
    """★ 起草档按设计**不写抑制**：用户的「改一下 / 不要」是他自己的诉求，
    不该变成对自己的 30 天封杀。回填加了 origin 前缀之后必须同时镜像 `_SUPPRESSION_ORIGINS` 过滤，
    否则等于把设计上明令不做的事补了回来。
    ★ 突变验证：去掉回填里的 `origin IN (...)` 过滤 ⇒ 本用例红。
    """
    kw = {"user_id": "u3", "kind": "preference", "key": "k", "value": "起草档口径", "reason": None, "workspace_id": "ws_1"}
    first = await store.enqueue_if_new(**kw, source_turn_id="t_1", origin=ORIGIN_FEEDBACK_DRAFT)
    await store.set_decision(cid=first.id, status="rejected", decided_by="u3")

    await _wipe_suppressions(store, then_backfill=True)
    again, outcome = await store.enqueue_if_new_with_outcome(**kw, source_turn_id="t_2", origin=ORIGIN_FEEDBACK_DRAFT)
    assert again is not None and outcome == ENQUEUE_CREATED, "起草档不许被回填封杀"


@pytest.mark.asyncio
async def test_the_backfilled_key_is_byte_identical_to_the_one_the_reader_builds(store):
    """★ 键的拼法在 SQL 与 Python 里各有一份（回填是集合式 INSERT...SELECT，没法逐行调 Python）。
    这条把两份钉在一起——它们一旦分家，失败形态就是**静默不生效**。"""
    kw = {"user_id": "u4", "kind": "preference", "key": "k", "value": "季度口径", "reason": None, "workspace_id": "ws_9"}
    first = await store.enqueue_if_new(**kw, source_turn_id="t_1")
    await store.set_decision(cid=first.id, status="rejected", decided_by="u4")
    await _wipe_suppressions(store, then_backfill=True)

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT source_scope_key FROM memory_candidate_suppressions WHERE user_id='u4'") as cur,
    ):
        keys = [r[0] for r in await cur.fetchall()]
    assert keys == [_suppression_scope_key("dreaming_light", "ws_9")]


@pytest.mark.asyncio
async def test_a_rejection_stays_recoverable_for_exactly_as_long_as_it_suppresses(store):
    """★★ 「回填幂等再生 ⇒ 用户的『不要』自动恢复」只有在**源行还在**时才成立。
    源行会被 `_redact_nonrecoverable_candidates` 脱敏（指纹置空），而回填要求指纹非空。

    两个窗口今天恰好都是 30 天（脱敏门槛 `decided_at < now-30d`；抑制 `_after_days(30)`），
    所以窗口内可重建、窗口外抑制也已过期，无损——但这是**两个常量各写一处、靠数值对齐**。
    谁把脱敏提前（比如 7 天），自愈就静默失效：用户某天又被问一次同一件事，不会有任何报错。
    这条把两个窗口钉在一起。

    ★ 突变验证：把脱敏门槛改成 `-7 days` ⇒ 本用例红。
    """
    kw = {"user_id": "u5", "kind": "preference", "key": "k", "value": "季度同比口径", "reason": None, "workspace_id": "ws_1"}
    cand = await store.enqueue_if_new(**kw, source_turn_id="t_1")
    await store.set_decision(cid=cand.id, status="rejected", decided_by="u5")

    # Backdate the decision to day 29, still inside the suppression window.
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE memory_candidates SET decided_at=datetime('now','-29 days') WHERE id=?",
            (cand.id,),
        )
        await db.execute("DELETE FROM memory_candidate_suppressions")
        await _redact_nonrecoverable_candidates(db)
        await _backfill_candidate_suppressions(db)
        await db.commit()

    again, outcome = await store.enqueue_if_new_with_outcome(**kw, source_turn_id="t_2")
    assert again is None and outcome == ENQUEUE_SUPPRESSED, "还在抑制窗口内，源行就不该被脱敏掉、抑制必须能重建"

    # Day 31: the source row is redacted and cannot be reconstructed, but suppression has expired anyway, so nothing is lost.
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE memory_candidates SET decided_at=datetime('now','-31 days'), content_redacted_at=NULL WHERE id=?",
            (cand.id,),
        )
        await db.execute("DELETE FROM memory_candidate_suppressions")
        await _redact_nonrecoverable_candidates(db)
        await _backfill_candidate_suppressions(db)
        async with db.execute("SELECT COUNT(*) FROM memory_candidate_suppressions WHERE user_id='u5'") as cur:
            regenerated = (await cur.fetchone())[0]
        await db.commit()
    assert regenerated == 0, "过了窗口就重建不出来——这正是上面那句断言必须成立的理由"


@pytest.mark.asyncio
async def test_the_rate_limited_skip_is_visible_in_the_log(store, caplog):
    """★★ 限流跳过是「这一轮什么都没产出」的**唯一痕迹**，必须 INFO 可见。

    2026-09-16 实测代价：116 上同一账号被另一个会话并发使用，我三轮里有两轮落在
    别人轮次之后 60 秒内 ⇒ 静默跳过；debug 级在 prod（LOG_LEVEL=INFO）什么都不打，
    「没跑」和「跑了但没提议」在日志里一模一样，查了一整轮才定位。
    ⚠ 限流按 **user** 计不按会话：排查时要看这个用户的全部轮次，不能只看自己这几轮。

    ★ 本用例**故意用默认 60s 间隔**（其余用例一律 `min_interval_seconds=0`，见下方说明）。
    ★ 突变验证：把这条 `logger.info` 改回 `logger.debug` ⇒ 本用例红。
    """
    import logging

    router = _JsonRouter([{"kind": "preference", "key": "prefer_qoq", "value": "QoQ", "reason": "said so"}])
    light = DreamingLight(router=router, candidate_store=store)  # Do not disable rate limiting: it is the subject of this test.

    first = await light.observe(_obs(turn_id="t_1"))
    assert len(first) == 1, "正对照：没被限流时确实会产出，否则下面的空只是整条路不通"

    with caplog.at_level(logging.INFO, logger="bowhead_memory.candidates"):
        second = await light.observe(_obs(turn_id="t_2", user_text="另一件完全不同的事"))

    assert second == [], "60s 内的第二轮应被限流"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("dreaming.skipped" in m and "rate_limited" in m for m in msgs), msgs


# ─── user-facing reasons never carry memory ids (116 1.5.19) ──────


def test_humanize_memory_ids_names_the_memory_by_its_text():
    from bowhead_memory.candidates import humanize_memory_ids

    texts = {"mem_7c729bd92a5c": "GMV 指含税成交额，统计口径按下单日"}
    out = humanize_memory_ids("本次对话与已有记忆 mem_7c729bd92a5c 矛盾，提议更新", texts)
    assert out == "本次对话与已有记忆「GMV 指含税成交额，统计口径按下单日」矛盾，提议更新"
    english = humanize_memory_ids(
        "Conflicts with existing memory mem_7c729bd92a5c.",
        {"mem_7c729bd92a5c": "Revenue means GMV including tax and shipping fees"},
    )
    assert english == "Conflicts with existing memory “Revenue means GMV includ…”."


def test_humanize_memory_ids_drops_unknown_ids_and_never_returns_one():
    from bowhead_memory.candidates import humanize_memory_ids

    assert humanize_memory_ids("与已有记忆 mem_abcdef123456 矛盾", {}) == "与已有记忆矛盾"
    assert humanize_memory_ids("覆盖旧记忆（mem_abcdef123456）", None) == "覆盖旧记忆"
    assert humanize_memory_ids("user said 我更喜欢看环比", None) == "user said 我更喜欢看环比"
    assert humanize_memory_ids(None) is None


def test_humanize_memory_ids_finds_ids_glued_to_cjk_text():
    """`\\b` does not separate CJK characters from ASCII; the id must still be found and resolved."""
    from bowhead_memory.candidates import humanize_memory_ids, memory_ids_in

    assert memory_ids_in("与已有记忆mem_7c729bd92a5c矛盾") == {"mem_7c729bd92a5c"}
    texts = {"mem_7c729bd92a5c": "GMV 指含税成交额"}
    assert humanize_memory_ids("与已有记忆mem_7c729bd92a5c矛盾", texts) == "与已有记忆「GMV 指含税成交额」矛盾"
    assert humanize_memory_ids("与已有记忆mem_7c729bd92a5c矛盾", {}) == "与已有记忆矛盾"
    # Not an id: part of a longer identifier.
    assert memory_ids_in("xmem_7c729bd92a5c and mem_7c729bd92a5cz") == set()


@pytest.mark.asyncio
async def test_observe_keeps_the_reason_as_written_and_resolves_ids_only_at_display(store):
    """Privacy: the stored reason must not copy another memory's text; display resolves it through the gated lookup."""
    router = _JsonRouter(
        [
            {
                "kind": "note",
                "key": "gmv_definition",
                "value": "GMV 指不含税成交额",
                "reason": "与已有记忆 mem_7c729bd92a5c 矛盾，用户改口",
            }
        ]
    )
    light = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)
    created = await light.observe(_obs(existing_memory="- 偏好:\n    · [mem_7c729bd92a5c] gmv_definition = GMV 指含税成交额"))
    assert created
    assert created[0].reason == "与已有记忆 mem_7c729bd92a5c 矛盾，用户改口"
    assert "GMV 指含税成交额" not in (created[0].reason or "")
