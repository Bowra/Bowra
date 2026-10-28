"""MemoryCandidateStore + DreamingLight 单测."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter

from bowhead_memory import (
    DreamingLight,
    DreamObservation,
    MemoryCandidateStore,
)
from bowhead_router import (
    AgentBinding,
    LLMRequest,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
)

if TYPE_CHECKING:
    from pathlib import Path


async def test_store_lifecycle(tmp_path: Path) -> None:
    s = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await s.initialize()

    c = await s.enqueue(
        user_id="u",
        kind="preference",
        key="prefer_qoq",
        value="environment-over-year",
        reason="user said so",
        source_turn_id="t1",
    )
    assert c.status == "pending"

    pending = await s.list_pending(user_id="u")
    assert len(pending) == 1

    decided = await s.set_decision(
        cid=c.id,
        status="approved",
        decided_by="admin",
    )
    assert decided is not None and decided.status == "approved"

    # Idempotent: a second decision returns the current status.
    again = await s.set_decision(
        cid=c.id,
        status="rejected",
        decided_by="admin2",
    )
    assert again is not None and again.status == "approved"

    # unknown id
    assert await s.set_decision(cid="nope", status="approved", decided_by="x") is None


def _scripted_router(*rs: LLMResponse) -> Router:
    a = MockAdapter(provider_name="p")
    a.scripted_responses = list(rs)
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p", type="mock")])
    reg._adapters["p"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    # Model tiers: DreamingLight defaults to echo (fast tier, fire-and-forget memory candidates).
    r.bind(AgentBinding(agent_type="echo", primary=("p", "mock")))
    return r


async def test_dreaming_parses_candidates(tmp_path: Path) -> None:
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    router = _scripted_router(
        LLMResponse(content=('{"candidates":[{"kind":"preference","key":"prefer_qoq","value":"always show QoQ","reason":"user explicitly asked"}]}'))
    )
    dl = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)
    created = await dl.observe(
        DreamObservation(
            turn_id="t1",
            user_id="u",
            user_text="以后都用环比别用同比",
            final_answer="好的，会按环比展示",
            intent="lookup",
            tool_chain=["bi_ask_question"],
        )
    )
    assert len(created) == 1
    assert created[0].key == "prefer_qoq"
    assert created[0].source_turn_id == "t1"

    pending = await store.list_pending(user_id="u")
    assert len(pending) == 1


async def test_dreaming_skips_invalid_candidates(tmp_path: Path) -> None:
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    router = _scripted_router(
        LLMResponse(
            content=(
                '{"candidates":['
                '{"kind":"nope","key":"x","value":"y"},'  # Invalid kind
                '{"kind":"preference","key":"","value":"y"},'  # Empty key
                '{"kind":"preference","key":"ok","value":"v"}'
                "]}"
            )
        )
    )
    dl = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)
    created = await dl.observe(
        DreamObservation(
            turn_id="t",
            user_id="u",
            user_text="x",
            final_answer="y",
            intent="lookup",
            tool_chain=[],
        )
    )
    assert len(created) == 1
    assert created[0].key == "ok"


async def test_dreaming_empty_on_garbage_output(tmp_path: Path) -> None:
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    router = _scripted_router(LLMResponse(content="not json"))
    dl = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)
    assert (
        await dl.observe(
            DreamObservation(
                turn_id="t",
                user_id="u",
                user_text="x",
                final_answer="y",
                intent="lookup",
                tool_chain=[],
            )
        )
        == []
    )


# ─── live Qwen ─────────────────────────────────────────────
@pytest.mark.live
async def test_live_dreaming_light(tmp_path: Path) -> None:
    key = os.environ.get("ALIYUN_DASHSCOPE_API_KEY")
    if not key:
        pytest.skip("ALIYUN_DASHSCOPE_API_KEY not set")
    adapter = OpenAICompatibleAdapter(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=key,
        provider_name="aliyun_qwen",
    )
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="aliyun_qwen", type="mock")])
    reg._adapters["aliyun_qwen"] = adapter  # type: ignore[attr-defined]
    r = Router(reg)
    r.bind(AgentBinding(agent_type="echo", primary=("aliyun_qwen", "qwen-plus")))  # Model tiers: DreamingLight → echo.

    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    dl = DreamingLight(router=r, candidate_store=store, min_interval_seconds=0)

    # An explicit user preference should be remembered.
    c1 = await dl.observe(
        DreamObservation(
            turn_id="live_1",
            user_id="u1",
            user_text="以后分析销售额都按渠道拆分，不要只看总数。",
            final_answer="好的，下次会默认按渠道拆分。",
            intent="lookup",
            tool_chain=["bi_ask_question"],
        )
    )
    for _c in c1:
        pass

    # A purely one-off query should not be remembered.
    c2 = await dl.observe(
        DreamObservation(
            turn_id="live_2",
            user_id="u1",
            user_text="今年 Q1 的 GMV 是多少？",
            final_answer="Q1 GMV 是 29.3 亿。",
            intent="lookup",
            tool_chain=["bi_ask_question"],
        )
    )
    # Non-strict assertion (LLM behavior may change): preferences should yield >= 1 candidate, one-off queries 0.
    # Only verify that it does not crash.
    assert c1 is not None and c2 is not None


async def test_dreaming_observe_router_override_wins(tmp_path: Path) -> None:
    """credential-readiness D2.1：observe(router=…) 用本 turn 的团队 router，
    构造期 self.router 不被触碰（BYO 团队的蒸馏必须走团队模型）。"""

    class _BoomRouter:
        async def invoke(self, agent_type: str, req: object) -> LLMResponse:
            raise AssertionError("构造期 router 不应被调用")

    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    turn_router = _scripted_router(LLMResponse(content='{"candidates":[{"kind":"preference","key":"k1","value":"v1","reason":"r"}]}'))
    dl = DreamingLight(router=_BoomRouter(), candidate_store=store, min_interval_seconds=0)
    created = await dl.observe(
        DreamObservation(
            turn_id="t_byo",
            user_id="u",
            user_text="记住我偏好",
            final_answer="好的",
            intent="lookup",
            tool_chain=[],
        ),
        router=turn_router,
    )
    assert len(created) == 1 and created[0].key == "k1"


class _CapturingRouter:
    """假 router：捕获蒸馏 prompt，返回空候选。"""

    def __init__(self) -> None:
        self.captured: list[LLMRequest] = []

    async def invoke(self, agent_type: str, req: LLMRequest) -> LLMResponse:
        self.captured.append(req)
        return LLMResponse(content='{"candidates":[]}')


def _obs(**kw) -> DreamObservation:
    base: dict = {
        "turn_id": "t1",
        "user_id": "u",
        "user_text": "Answer in English only. Refund top 10 by brand.",
        "final_answer": "Done.",
        "intent": "lookup",
        "tool_chain": ["bi_query"],
    }
    base.update(kw)
    return DreamObservation(**base)


async def test_dreaming_prompt_carries_en_language_rule(tmp_path: Path) -> None:
    """memory-i18n：候选是用户可见文本（确认卡/记忆页），语言必须跟 output_language
    设置——此前蒸馏语言只跟对话内容走（116 实测 8-16 中文轮 reason 是中文）。"""
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    router = _CapturingRouter()
    dl = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)
    await dl.observe(_obs(output_language="en-US"))
    prompt = router.captured[-1].messages[0].content
    assert "Output language: write every candidate's value and reason in English" in prompt


async def test_dreaming_prompt_carries_zh_language_rule(tmp_path: Path) -> None:
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    router = _CapturingRouter()
    dl = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)
    await dl.observe(_obs(output_language="zh-CN"))
    prompt = router.captured[-1].messages[0].content
    assert "输出语言：所有候选的 value 与 reason 一律用中文写" in prompt


async def test_dreaming_prompt_unchanged_without_language(tmp_path: Path) -> None:
    """positive control 的反面：语言未知（language_context=None 链路）→ prompt 不加
    语言行，现状逐字节不变。"""
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    router = _CapturingRouter()
    dl = DreamingLight(router=router, candidate_store=store, min_interval_seconds=0)
    await dl.observe(_obs())
    prompt = router.captured[-1].messages[0].content
    assert "Output language" not in prompt
    assert "输出语言" not in prompt


# ─── Track B batch 1b: origin allowlist ──────────────────────────


@pytest.mark.asyncio
async def test_a_feedback_draft_is_not_auto_rejected_when_it_goes_stale(tmp_path):
    """★ brief 地雷 3：过期自动驳回是给**后台蒸馏**候选设计的。

    起草档（`origin='feedback_draft'`）是**用户显式诉求** —— 放着没管就被判死，
    等于把用户明说的话静默压掉。白名单（不是黑名单）：以后再加来源默认也不被它碰。

    ★ 突变验证：把驳回 UPDATE 里的 `COALESCE(origin,...)='dreaming_light'` 去掉
    ⇒ 本用例红。⚠ 只在挑用户的那句 SELECT 上过滤是**不够的** ——
    那只是挑人，UPDATE 才是动数据的地方，少一处就等于没过滤。
    """
    from bowhead_memory.candidates import ORIGIN_DREAMING, ORIGIN_FEEDBACK_DRAFT, MemoryCandidateStore

    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    old_ts = "2020-01-01T00:00:00+00:00"

    a = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="k_dream",
        value="后台猜的",
        reason=None,
        source_turn_id="t1",
        origin=ORIGIN_DREAMING,
    )
    b = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="k_draft",
        value="用户明说的",
        reason=None,
        source_turn_id="t1",
        origin=ORIGIN_FEEDBACK_DRAFT,
    )
    import aiosqlite

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE memory_candidates SET created_at=?", (old_ts,))
        await db.commit()

    await store.auto_reject_stale(max_age_days=30)

    assert (await store.get(a.id)).status == "rejected", "后台蒸馏候选照旧过期"
    assert (await store.get(b.id)).status == "pending", "用户显式诉求不该被自动判死"


@pytest.mark.asyncio
async def test_a_dreaming_rejection_does_not_silence_the_user_saying_it_out_loud(tmp_path):
    """★★ brief 地雷 3 的后半：两池抑制不串扰。

    场景：后台猜过一条、用户驳回了 ⇒ 写进 30 天逐字抑制。
    然后**用户自己明说**同一件事 —— 如果起草档也读那份抑制，
    用户明说的话会被自己上一次「不用你猜」静默压掉 30 天。

    ★ 突变验证：把 `_suppression_scope_key` 的 origin 前缀去掉（两池共用一个键）⇒ 本用例红。
    ★★ 这里原本还有第二道防线（读侧 `if origin in _SUPPRESSION_ORIGINS` 整段跳过），
    突变发现**两者互为冗余** —— 单独拆任何一个用例都不红，两个一起拆才红。
    也就是说建了两处实现、而且哪一处都没被单独测到。已只留 key 前缀这一套
    （读写共用同一个函数，不会漂；对未来新增来源自动生效）。
    """
    from bowhead_memory.candidates import ORIGIN_DREAMING, ORIGIN_FEEDBACK_DRAFT, MemoryCandidateStore

    store = MemoryCandidateStore(db_path=str(tmp_path / "s.db"))
    await store.initialize()

    a = await store.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="k",
        value="排名图用横向条形图",
        reason=None,
        source_turn_id="t1",
        origin=ORIGIN_DREAMING,
    )
    assert a is not None
    await store.set_decision(cid=a.id, status="rejected", decided_by="u1")

    # Background guesses the same content again; suppression blocks it (positive control: suppression actually works).
    again = await store.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="k",
        value="排名图用横向条形图",
        reason=None,
        source_turn_id="t2",
        origin=ORIGIN_DREAMING,
    )
    assert again is None, "同一池内抑制必须仍然生效，否则下面那条证明不了什么"

    # The user explicitly states it; another pool's suppression must not block it.
    mine = await store.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="k",
        value="排名图用横向条形图",
        reason=None,
        source_turn_id="t3",
        origin=ORIGIN_FEEDBACK_DRAFT,
    )
    assert mine is not None, "用户明说的话被后台那池的抑制压掉了"


@pytest.mark.asyncio
async def test_deciding_a_feedback_draft_writes_no_suppression(tmp_path):
    """★ 起草档**不写**抑制：「改一下/重起草」不该变成 30 天逐字封杀，
    否则用户下次说同一件事会被自己上一次的「改一下」挡掉。

    ★ 突变验证：把写侧的 `_row_origin in _SUPPRESSION_ORIGINS` 去掉 ⇒ 本用例红。
    ★★ 它第一次跑就是红的，而**守卫本身写对了** —— 真因是产生 `row` 的那句 SELECT
    只查了 5 列、没带 `origin`，于是 `_optional_row_value` 返 None、回退成默认的
    dreaming ⇒ 守卫形同虚设。这类失效是**静默**的：代码读起来完全正确。
    第二个突变就是钉它：把 SELECT 里的 `origin` 去掉 ⇒ 本用例同样红。
    """
    import aiosqlite
    from bowhead_memory.candidates import ORIGIN_FEEDBACK_DRAFT, MemoryCandidateStore

    store = MemoryCandidateStore(db_path=str(tmp_path / "s2.db"))
    await store.initialize()
    c = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="k",
        value="v",
        reason=None,
        source_turn_id="t1",
        origin=ORIGIN_FEEDBACK_DRAFT,
    )
    await store.set_decision(cid=c.id, status="rejected", decided_by="u1")

    async with aiosqlite.connect(store.db_path) as db:
        [(n,)] = await db.execute_fetchall("SELECT COUNT(*) FROM memory_candidate_suppressions")
    assert n == 0, "起草档不该写抑制"


@pytest.mark.anyio
async def test_a_newer_take_on_the_same_topic_replaces_the_older_one(tmp_path):
    """★★ 同一主题只留最新的一条。

    116 实测（2026-09-09）：38 条待确认里 `summary_max_sentences` 出现**三次**
    ——「3-4 sentences」「三句话以内」「两句话以内」，三个互相打架的版本
    同时排队等用户裁决。用户不会照着上一次的措辞重说一遍，他会换个说法，
    而判重用的 `content_fingerprint` 不含 key，防不住这个。

    ★ 突变验证：把入队处那段 UPDATE 删掉 ⇒ 本用例红（队列里留下两条）。
    """
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    kw = {"user_id": "u1", "kind": "preference", "key": "summary_max_sentences", "reason": None, "source_turn_id": "t1"}
    old = await store.enqueue_if_new(value="报告摘要控制在三句话以内", **kw)
    new = await store.enqueue_if_new(value="报告摘要控制在两句话以内", **kw)
    assert old is not None and new is not None

    pending = await store.list_pending(user_id="u1")
    assert [c.id for c in pending] == [new.id], "同一主题不该同时排两条"

    prior = await store.get(old.id)
    assert prior is not None and prior.status == "rejected"
    # ★ Decisive distinction: this is **supersession**, not **rejection**. Recording user_rejected
    # makes the audit say "the user does not want this", although they merely restated the same request differently.
    assert prior.decision_kind == "superseded_by_newer"


@pytest.mark.anyio
async def test_being_superseded_never_writes_a_suppression(tmp_path):
    """★★★ 让位**一条抑制都不许写**。

    写了的话：用户把同一件事换个说法讲三遍，前两遍会把**他自己的诉求**
    封杀 30 天 —— 下次他再提，系统直接静默压掉。

    ★ 它**结构上**写不了抑制：写抑制的代码只存在于 `set_decision` 里，
    而入队折叠是一条直接 UPDATE，根本不经过它（`set_decision` 自己开连接，
    在入队事务里调是嵌套事务）。
    ⚠ **等价突变（实测记录）**：把 `superseded_by_newer` 加进 `set_decision`
    写抑制那个元组 —— 本用例**不红**，因为那段代码这条路上到不了。
    我初版把它当成决定性突变写在注释里，是错的；顺带发现我在 `set_decision`
    里加的那个 `SUPERSEDE_ACTOR` 分支是**死码**，已删。
    """
    import aiosqlite

    store = MemoryCandidateStore(db_path=str(tmp_path / "c2.db"))
    await store.initialize()
    kw = {"user_id": "u1", "kind": "preference", "key": "summary_max_sentences", "reason": None, "source_turn_id": "t1"}
    await store.enqueue_if_new(value="三句话以内", **kw)
    await store.enqueue_if_new(value="两句话以内", **kw)

    async with aiosqlite.connect(store.db_path) as db:
        row = await (await db.execute("SELECT COUNT(*) FROM memory_candidate_suppressions")).fetchone()
    assert row[0] == 0


@pytest.mark.anyio
async def test_a_different_topic_is_not_swallowed(tmp_path):
    """负对照：折叠只按**同一个 key**。不同主题各排各的队 ——
    否则「只留最新一条」会变成「只留最新一条建议」，把别的话题吃掉。
    """
    store = MemoryCandidateStore(db_path=str(tmp_path / "c3.db"))
    await store.initialize()
    base = {"user_id": "u1", "kind": "preference", "reason": None, "source_turn_id": "t1"}
    a = await store.enqueue_if_new(key="summary_max_sentences", value="三句话以内", **base)
    b = await store.enqueue_if_new(key="ranking_chart_type", value="用横向条形图", **base)
    pending = await store.list_pending(user_id="u1")
    assert {c.id for c in pending} == {a.id, b.id}


@pytest.mark.anyio
async def test_enqueue_returns_an_object_that_knows_its_own_origin(tmp_path):
    """★★★ 入队**返回的对象**必须带 origin，不能只有库里那行对。

    116 真机（1.2.61）抓到：点「改一下」→ 后端 200、库里 `feedback_draft`，
    而回给前端的 DTO 是 `dreaming_light` ⇒ **新卡上的「改一下」按钮当场消失**
    （按钮按 origin 渲染）。根因是构造返回值时漏抄了这个字段。

    ⚠ 同一形状的**第五次**（origin dataclass / approved_resource_id /
    feedback_confirmed / import / 这次的构造点）：**加了字段只追一条读路，
    另一条照旧**。`_row()` 与两个 `enqueue` 是三条独立的装配路径。

    ★ 突变验证：拿掉任一 `enqueue` 里的 `origin=origin` ⇒ 本用例红。
    """
    from bowhead_memory.candidates import ORIGIN_FEEDBACK_DRAFT

    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    base = {"user_id": "u1", "kind": "preference", "reason": None, "source_turn_id": "t1"}

    a = await store.enqueue(key="k1", value="v1", origin=ORIGIN_FEEDBACK_DRAFT, **base)
    b = await store.enqueue_if_new(key="k2", value="v2", origin=ORIGIN_FEEDBACK_DRAFT, **base)
    assert a.origin == ORIGIN_FEEDBACK_DRAFT, "enqueue 的返回值丢了 origin"
    assert b is not None and b.origin == ORIGIN_FEEDBACK_DRAFT, "enqueue_if_new 的返回值丢了 origin"
    # All three assembly paths must agree: two construction sites and `_row()`.
    assert (await store.get(a.id)).origin == ORIGIN_FEEDBACK_DRAFT
    # Negative control: omission defaults to the distillation pool; preserve this default too.
    d = await store.enqueue(key="k3", value="v3", **base)
    assert d.origin == "dreaming_light"


@pytest.mark.anyio
async def test_enqueue_returns_exactly_what_a_later_read_will_give(tmp_path):
    """★★★ 只剩**一条装配路径**：入队返回的对象 == 之后读回来的对象。

    此前 `MemoryCandidate` 有三条（两个 enqueue 手拼 + `_row`），
    两天里这条根因产出过三个真 bug：`origin` 没进 dataclass（端点 400）、
    `origin` 没进两个构造点（116 真机：点完「改一下」按钮当场消失）、
    以及这条**埋着的**——手拼返回的是**入参** value，而库里存的是
    `json.dumps(default=str)` 之后的。

    ★ 用 **tuple** 当探针：JSON 没有元组，存进去读回来是 list ⇒ 手拼路返回
    `("a","b")`、读回路返回 `["a","b"]`，两者不等 —— 正好逮住这个差别。
    ⚠ 探针要挑**会暴露差别**的输入：普通字符串两条路一模一样，
    用它当断言的话，真出问题那天这条用例也是绿的。
    ⚠ 本来想用 `date`（`default=str` 会改写它），但 `enqueue_if_new` 走的
    `_write_gate_allows` 里那个 `json.dumps` **没带 `default=str`**，
    对 date 直接 TypeError —— 两条入队路接受的输入类型都不一样。
    那是**另一个**同族的潜伏不一致，已记进 spike，本次不顺手改。

    ★ 突变验证：把任一 enqueue 改回手写构造 ⇒ 本用例红。
    """
    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    base = {"user_id": "u1", "kind": "preference", "reason": None, "source_turn_id": "t1"}

    a = await store.enqueue(key="k1", value=("a", "b"), **base)
    assert a.value == (await store.get(a.id)).value

    b = await store.enqueue_if_new(key="k2", value=("c", "d"), **base)
    assert b is not None
    assert b.value == (await store.get(b.id)).value

    # Compare all fields: both paths must produce objects identical field by field.
    assert a == await store.get(a.id)
    assert b == await store.get(b.id)
