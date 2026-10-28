"""记忆重做 批1 —— FR3 取代 + FR9 置信度。

上游：`docs/sprints/memory-spike-2026-08-29/tech-design.md` §4.2。
闸：`FEATURE_MEMORY_CANONICAL`（默认 0；每条「闸关时今天怎样」都单独钉了 positive control，
否则「闸开后变了」可能只是因为 fixture 根本没种进去）。

本批要解的**根因不是「模型不够聪明」**：`DREAMING_SYSTEM` 判定规则 5 从 2026-07-22 起
就要求模型表达「取代」，而它唯一能写的 `reason` 是一个**没有任何代码解析的自由文本字段**
（`enqueue_if_new` 只把它原样落库）。于是净利率被原样提议了 5 次。
⇒ 第一动作是在**写入契约上开一个槽**（`supersedes`），并在后端给它一张裁决表。

交付判据（tech-design §10）两条，分别钉在：
- ④「改成 Top 5 → 旧的 Top 10 被取代，只剩一条」
  → `test_cross_key_supersede_leaves_exactly_one_active_entry`（store）
  + `test_remember_supersedes_the_old_entry`（工具）
- ⑤「low 想改 high → 落成候选而不是覆盖」
  → `test_low_confidence_cannot_overwrite_high`（store）
  + `test_approving_a_candidate_that_contradicts_a_user_statement_is_refused`（approve 路）

## 突变验证（30 条，逐条把实现改回去跑了一遍）

29 条打红了对应用例：裁决表放行 low→high / 置信度一律 high / 去标点不看数字间 /
分词复用去空白的规范化 / 摘掉逐字判重 / 覆盖不留上一版 / 不执行 supersedes（store 与
approve 两处各一）/ supersede 可以打运维行 / note 用 `text_v1` / 工具契约不看闸 /
缺 key 时替它编一个 / 摘掉 notes 双写兜底 / recall 只读 entries / approve 不查裁决表 /
蒸馏 prompt 恒 canonical / digest 不带 id / digest 不过滤运维行 / approve 不脱敏 supersedes /
`upsert` 不盖 confidence / `upsert` 闸关也盖 confidence / 手改不盖 high /
身份查找提前按作用域分片 / 逐字判重命中就早返回（store 与 approve 各一）/
回收站里的条目仍然拦着 / 蒸馏 prompt 闸关也塞新字段 / 闸关留下空占位 /
工具 description 闸关也改。

两条要留档：

- **「缺 key 时替它编一个」第一次没红，是突变没生效**（只加了个多余的返回字段，没改行为）。
  换成「真的按内容算一个 key 并写进去」才红。★ 突变没红先怀疑突变本身。
- **`_parse_supersedes` 的去重是等价突变**：写入侧 `_supersedes_json` 已经去过一次，
  库里不会有重复项 ⇒ 读出来再去一次打不红任何用例。留着是给「不经过本模块写进去的行」
  兜底，不是活判据 —— 别因为它绿着就以为它被测到了。
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from bowhead_core.memory_notes import MemoryNotesStore
from bowhead_core.memory_tools import _recall, _remember, register_memory_tools
from bowhead_core.memory_write_policy import (
    can_overwrite,
    confidence_for_source,
    dedup_tokens,
    is_near_duplicate,
    is_verbatim_duplicate,
    jaccard,
    normalize_for_dedup,
    normalize_memory_type,
)
from bowhead_core.personal_memory_governance import PersonalMemoryGovernanceStore
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_core.user_memory import MemoryConfidenceConflictError, UserMemoryStore
from bowhead_memory.candidates import MemoryCandidateStore


@pytest.fixture
async def store(tmp_path):
    s = UserMemoryStore(tmp_path / "memory.db")
    await s.initialize()
    return s


# ═══ A · Pure logic: confidence derives from write identity, never model self-claims ═══════════════


def test_confidence_comes_from_the_write_identity():
    """★ 模型自评置信度是它自己的输出。拿它做「谁能覆盖谁」的判据
    = 让写入方给自己发通行证（tech-design §8b）。所以判据只认写入身份串。"""
    assert confidence_for_source("user_directive") == "high"  # remember tool
    assert confidence_for_source("user_explicit") == "high"  # Create on /memory page
    assert confidence_for_source("user_edit") == "high"  # Manual edit on /memory page
    assert confidence_for_source("dreaming_light_approved") == "low"  # Background inference
    # Unknown source falls to low: fail-safe denies overwrite authority rather than granting it by default.
    assert confidence_for_source("some_future_writer") == "low"
    assert confidence_for_source(None) == "low"


def test_the_only_refused_cell_is_low_over_high():
    """四格裁决表。突变：把这一格放行 ⇒ 下面 store/approve 两条用例同时红。"""
    assert can_overwrite(new_confidence="high", existing_confidence="high") is True
    assert can_overwrite(new_confidence="high", existing_confidence="low") is True
    assert can_overwrite(new_confidence="low", existing_confidence="low") is True
    assert can_overwrite(new_confidence="low", existing_confidence="high") is False
    # Unannotated legacy rows (NULL) count as low: lacking evidence of direct user statements,
    # they must not gain authority to block new writes (which would turn every pre-migration row into an immutable tombstone).
    assert can_overwrite(new_confidence="low", existing_confidence=None) is True


def test_memory_type_is_fail_safe_to_semantic():
    assert normalize_memory_type("procedural") == "procedural"
    assert normalize_memory_type("SEMANTIC") == "semantic"
    assert normalize_memory_type("episodic") == "semantic"  # Type removed by r3
    assert normalize_memory_type(None) == "semantic"


# ═══ A2 · Pure logic: deduplication ═══════════════════════════════════════════════════


def test_verbatim_dedup_ignores_punctuation_case_and_width():
    assert is_verbatim_duplicate("排名图用 Top 10。", "排名图用Top10")
    assert is_verbatim_duplicate("Prefer QoQ", "prefer  qoq!")
    assert is_verbatim_duplicate("ＫＰＩ 用百万", "KPI用百万")  # Full-width → NFKC
    assert not is_verbatim_duplicate("排名图用 Top 10", "排名图用 Top 5")
    assert not is_verbatim_duplicate("", "")  # Empty strings do not match, or the first write would be swallowed.


def test_digits_keep_their_separators_so_5_5_is_not_55():
    """★ 标点在数据里兼职：千分位与小数点。一律去标点会把 `5.5` 判成 `55`
    ——那是「看着像真的错的数」那一族的形态，本仓踩过。"""
    assert normalize_for_dedup("阈值 5.5") == "阈值5.5"
    assert not is_verbatim_duplicate("阈值 5.5", "阈值 55")


def test_tokenizer_does_not_reuse_the_whitespace_stripped_normalization():
    """★ 分词若复用 `normalize_for_dedup`（它去空白），整句英文会缩成**一个** token，
    两句只差一个 `the` 的英文偏好相似度会算成 0.0。这条是那次真踩坑的 regression 钉。"""
    tokens = dedup_tokens("always show QoQ in trend reports")
    assert len(tokens) > 1, tokens
    assert "always" in tokens
    assert jaccard("always show QoQ in trend reports", "always show qoq in the trend reports") == 1.0


def test_near_duplicate_separates_rewording_from_a_different_metric():
    """判据分界线。上线前在真实偏好串上空跑过：命中的都是换措辞，
    不命中的都是换了指标/换了数 —— 这就是那次空跑的结论，别改阈值不改这条。"""
    assert is_near_duplicate("排名图一律用横向条形图", "排名图都用横向条形图")
    assert is_near_duplicate("报告先结论后依据", "报告要先结论后依据")
    assert not is_near_duplicate("内部盈利只看净利率", "内部盈利只看毛利率")
    assert not is_near_duplicate("趋势图看环比", "趋势图看同比")
    assert not is_near_duplicate("排名图用 Top 10", "排名图用 Top 5")


# ═══ B · Store decisions (commit_memory_write) ══════════════════════════════════════


async def _write(store, **kw):
    base = {
        "user_id": "u1",
        "kind": "preference",
        "source": "user_directive",
        "memory_type": "procedural",
    }
    return await store.commit_memory_write(**{**base, **kw})


@pytest.mark.asyncio
async def test_a_new_write_is_product_memory_with_confidence_and_confirmed_at(store):
    r = await _write(store, key="kpi_money_unit", value="KPI 营收用百万")
    assert r.action == "created"
    assert r.entry.confidence == "high"
    assert r.entry.memory_type == "procedural"
    assert r.entry.last_confirmed_at is not None
    # Must pass policy, or injection silently drops it after the gate opens (a wasted write).
    assert [e.id for e in await store.list_product_for_user("u1")] == [r.entry.id]


@pytest.mark.asyncio
async def test_note_kind_gets_note_text_v1_or_it_silently_leaves_the_pack(store):
    """`kind='note'` 的 policy 键是 `note_text_v1`，`_value_schema_for` 给的是
    `text_v1`。错一个字 `resolve_memory_policy` 就判它不可见 ⇒ 条目还在库里，
    但**再也不进 `<remembered_about_user>`**。只断言「写进去了」看不出这件事。"""
    r = await _write(store, kind="note", key="boss_name", value="老板叫张总")
    assert r.entry.value_schema == "note_text_v1"
    assert r.entry.id in {e.id for e in await store.list_product_for_user("u1")}


@pytest.mark.asyncio
async def test_verbatim_repeat_bumps_last_confirmed_at_without_adding_a_row(store):
    """FR8-3 / §4.2 d-1：这条直接解掉「净利率被提议 5 次」。
    断言 version **没**加 1 —— 值没变，这不是一次修改，是一次「又说了一遍」。"""
    first = await _write(store, key="ranking_top_n", value="排名图只看 Top 10")
    again = await _write(store, key="ranking_top_n", value="排名图只看Top10。")
    assert again.action == "confirmed"
    assert again.entry.id == first.entry.id
    assert again.entry.version == first.entry.version == 1
    assert again.entry.last_confirmed_at >= first.entry.last_confirmed_at
    assert len(await store.list_for_user("u1")) == 1


@pytest.mark.asyncio
async def test_verbatim_repeat_under_a_different_key_still_does_not_add_a_row(store):
    """换 key 复述同一句话也不该变成第二条 —— 存量里 35 条重复主张正是这个形态。"""
    first = await _write(store, key="ranking_top_n", value="排名图只看 Top 10")
    again = await _write(store, key="ranking_scope", value="排名图只看 Top 10")
    assert again.action == "confirmed"
    assert again.entry.id == first.entry.id
    assert len(await store.list_for_user("u1")) == 1


@pytest.mark.asyncio
async def test_same_key_overwrite_keeps_exactly_one_previous_version(store):
    first = await _write(store, key="ranking_top_n", value="排名图只看 Top 10")
    second = await _write(store, key="ranking_top_n", value="排名图只看 Top 5")
    assert second.action == "updated"
    assert second.entry.id == first.entry.id
    assert second.entry.version == 2
    assert json.loads(second.entry.previous_value) == "排名图只看 Top 10"
    assert second.entry.previous_value_at is not None
    # Retain one previous version: another edit updates previous to the last version, not the first (one-step undo, not a version chain).
    third = await _write(store, key="ranking_top_n", value="排名图只看 Top 3")
    assert json.loads(third.entry.previous_value) == "排名图只看 Top 5"


@pytest.mark.asyncio
async def test_cross_key_supersede_leaves_exactly_one_active_entry(store):
    """★ 交付判据 ④：「改成 Top 5」→ 旧的 Top 10 被取代，`/memory/items` 只剩一条。
    软取代：旧条进回收站可恢复，不是硬删。"""
    old = await _write(store, key="ranking_top_n", value="排名图只看 Top 10")
    new = await _write(
        store,
        kind="constraint",
        key="ranking_scope",
        value="排名一律只取前五名",
        supersedes=[old.entry.id],
    )
    assert new.superseded == (old.entry.id,)
    assert [e.id for e in await store.list_product_for_user("u1")] == [new.entry.id]
    [trashed] = await store.list_trashed("u1")
    assert trashed.id == old.entry.id
    assert trashed.superseded_by == new.entry.id
    assert trashed.deleted_at is not None


@pytest.mark.asyncio
async def test_low_confidence_cannot_overwrite_high(store):
    """★ 交付判据 ⑤ 的 store 面：后台推断不许悄悄改掉用户亲口说的。"""
    mine = await _write(store, key="profit_metric", value="内部盈利只看净利率")
    blocked = await _write(
        store,
        key="profit_metric",
        value="内部盈利只看毛利率",
        source="dreaming_light_approved",
    )
    assert blocked.action == "blocked"
    assert blocked.entry is None
    assert blocked.blocked_by.id == mine.entry.id
    # Value remains untouched (asserting action alone is insufficient; prove no write occurred).
    [current] = await store.list_product_for_user("u1")
    assert current.parsed_value() == "内部盈利只看净利率"
    assert current.version == 1


@pytest.mark.asyncio
async def test_high_overwrites_low_and_low_overwrites_low(store):
    inferred = await _write(store, key="money_unit", value="营收用百万", source="dreaming_light_approved")
    assert inferred.entry.confidence == "low"
    again = await _write(store, key="money_unit", value="营收用千万", source="dreaming_light_approved")
    assert again.action == "updated"
    mine = await _write(store, key="money_unit", value="营收用亿")
    assert mine.action == "updated"
    assert mine.entry.confidence == "high"


@pytest.mark.asyncio
async def test_a_low_write_cannot_supersede_a_high_entry_either(store):
    """裁决表管的是「谁能顶掉谁」，不只是同 key 覆盖。
    否则 `supersedes` 就成了绕过裁决表的后门。"""
    mine = await _write(store, key="profit_metric", value="内部盈利只看净利率")
    other = await _write(
        store,
        key="profit_note",
        value="盈利口径以毛利率为准",
        source="dreaming_light_approved",
        supersedes=[mine.entry.id],
    )
    assert other.supersede_blocked == (mine.entry.id,)
    assert other.superseded == ()
    assert {e.id for e in await store.list_product_for_user("u1")} == {mine.entry.id, other.entry.id}


@pytest.mark.asyncio
async def test_supersede_cannot_reach_operational_rows(store):
    """运维行（BI 上下文快照）与真记忆同住一张表。让 `supersedes` 打得到它，
    模型一次幻觉就能把用户的 BI 上下文状态删掉。"""
    await store.set_tool_context("u1", "bi", context_id="1", context_name="商超零售场景")
    [op] = [e for e in await store.list_for_user("u1") if e.kind == "tool_context"]
    r = await _write(store, key="kpi_money_unit", value="KPI 营收用百万", supersedes=[op.id])
    assert r.superseded == ()
    assert op.id in {e.id for e in await store.list_for_user("u1")}


@pytest.mark.asyncio
async def test_near_duplicates_are_named_never_merged(store):
    """v1 只提示不合并：自动合并的误伤方向是「静默吃掉一条真新偏好」，比多留一条更伤。"""
    first = await _write(store, key="ranking_chart", value="排名图一律用横向条形图")
    second = await _write(store, key="ranking_chart_2", value="排名图都用横向条形图")
    assert second.action == "created"
    assert second.near_duplicates == (first.entry.id,)
    assert len(await store.list_product_for_user("u1")) == 2


# ═══ C · `remember` tool ═══════════════════════════════════════════════════════════


def _tool_ctx(*, user_memory_store=None, notes_store=None) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws",
        member_uid="m",
        user_id="u1",
        turn_id="t1",
        datarc=None,
        memory_notes_store=notes_store,
        user_memory_store=user_memory_store,
    )


@pytest.fixture
async def notes(tmp_path):
    s = MemoryNotesStore(tmp_path / "notes.db")
    await s.initialize()
    return s


def _remember_tool():
    registry = build_default_registry()
    register_memory_tools(registry)
    return registry.get("remember").schema


def _remember_schema() -> dict:
    return _remember_tool().input_schema


#: **Verbatim original** `remember` description before batch 1 (from the preceding commit).
#: Tool descriptions, like input_schema, are static contracts sent to the LLM each turn;
#: gate-off invariance must compare the **pre-change** string, not one assembled by this implementation (self-validation).
_REMEMBER_DESCRIPTION_BEFORE_BATCH1 = """把用户的一条持久信息存入长期记忆（跨会话保留）。**仅当**用户明确要你记住、或陈述一条会长期生效的偏好/规则/事实/称谓时调用（如『以后 head to head 都对比同环比』『我的预算口径是…』『我老板叫张总』）。kind: rule(规则)/preference(偏好)/fact(事实)/relationship(称谓)/note(备注)；text: 用一句话写清要记住什么，用用户的原意。存到用户个人记忆，可在『记忆』页查看/修改。一次性、带具体日期的事（如『下周三去北京』）不要存，那属于定时任务。**只有在返回 ok=True 后**才能对用户说『已记住』，并引用返回的 stored 字段确认存了什么。"""


def test_remember_contract_supports_explicit_supersedes():
    """★ 根因就在这里：模型一直被要求表达「取代」，却只有 `reason` 这条
    **没人解析**的通道。这条断言的是「槽真的在契约上」。"""
    schema = _remember_schema()
    assert set(schema["properties"]) == {"kind", "text", "key", "memory_type", "supersedes"}
    assert schema["required"] == ["kind", "key", "text", "memory_type"]
    assert schema["properties"]["supersedes"]["type"] == "array"
    assert schema["properties"]["memory_type"]["enum"] == ["procedural", "semantic"]


@pytest.mark.asyncio
async def test_remember_writes_an_addressable_entry_when_the_flag_is_on(notes, store):
    res = await _remember(
        {"kind": "preference", "key": "kpi_money_unit", "text": "KPI 卡片里营收用百万", "memory_type": "procedural"},
        _tool_ctx(user_memory_store=store, notes_store=notes),
    )
    assert res["ok"] is True
    assert res["id"].startswith("mem_")
    assert res["action"] == "created"
    [entry] = await store.list_product_for_user("u1")
    assert entry.id == res["id"]
    assert entry.key == "kpi_money_unit"
    assert entry.confidence == "high"
    assert entry.created_source_turn_id == "t1"
    # Legacy notes path is no longer written, avoiding duplicate preferences in the pack.
    assert await notes.get(user_id="u1") is None


@pytest.mark.asyncio
async def test_remember_refuses_to_invent_a_key(notes, store):
    """★ 缺 key 时**拒写**而不是替它编一个。编出来的 key 只能来自内容，
    而内容一变就是新主题 —— `kind='note'` 的 `_stable_note_key`（内容哈希）
    正是这个病：换一个字就是另一个 key ⇒ 自由文本记忆永远判不出同主题。"""
    res = await _remember(
        {"kind": "preference", "text": "KPI 卡片里营收用百万", "memory_type": "procedural"},
        _tool_ctx(user_memory_store=store, notes_store=notes),
    )
    assert res["ok"] is False
    assert res["error"] == "missing_key"
    assert await store.list_for_user("u1") == []


@pytest.mark.asyncio
async def test_remember_supersedes_the_old_entry(notes, store):
    """★ 交付判据 ④ 的工具面（dogfood ④ 的单测版）。"""
    ctx = _tool_ctx(user_memory_store=store, notes_store=notes)
    first = await _remember(
        {"kind": "preference", "key": "ranking_top_n", "text": "排名图只看 Top 10", "memory_type": "procedural"},
        ctx,
    )
    second = await _remember(
        {
            "kind": "constraint",
            "key": "ranking_scope",
            "text": "排名一律只取前五名",
            "memory_type": "procedural",
            "supersedes": [first["id"]],
        },
        ctx,
    )
    assert second["superseded"] == [first["id"]]
    assert [e.id for e in await store.list_product_for_user("u1")] == [second["id"]]


@pytest.mark.asyncio
async def test_remember_does_not_double_write_something_already_in_legacy_notes(notes, store):
    """⚠ §5 存量迁移作业跑完之前，`build_memory_pack` 的 notes 段仍在注入
    （迁移不清空 notes，那是回滚位）。不查一下就写，同一条偏好会在
    `<remembered_about_user>` 里出现两次。"""
    await notes.upsert_content(user_id="u1", content="• 偏好：KPI 卡片里营收用百万", source="user_directive")
    res = await _remember(
        {"kind": "preference", "key": "kpi_money_unit", "text": "KPI 卡片里营收用百万", "memory_type": "procedural"},
        _tool_ctx(user_memory_store=store, notes_store=notes),
    )
    assert res["ok"] is True
    assert res["deduped"] is True
    assert await store.list_for_user("u1") == []


@pytest.mark.asyncio
async def test_recall_reads_entries_with_ids_and_still_reads_legacy_notes(notes, store):
    """★ 只读 entries 会让 §5 迁移前的老用户 `recall` **直接返空**（用户故事 4 当场回归）；
    只读 notes 就是没改。两段都要在，且 canonical 条目要带 `[mem_xxx]`（supersedes 的把手）。"""
    written = await store.commit_memory_write(
        user_id="u1",
        kind="preference",
        key="kpi_money_unit",
        value="KPI 卡片里营收用百万",
        source="user_directive",
    )
    await notes.upsert_content(user_id="u1", content="• 规则：周报必带 SEC cross-check", source="user_directive")
    res = await _recall({}, _tool_ctx(user_memory_store=store, notes_store=notes))
    assert res["found"] is True
    joined = "\n".join(res["items"])
    assert f"[{written.entry.id}]" in joined
    assert "周报必带 SEC cross-check" in joined


@pytest.mark.asyncio
async def test_recall_of_a_notes_only_user_is_not_empty_under_the_flag(notes, store):
    await notes.upsert_content(user_id="u1", content="• 规则：周报必带 SEC cross-check", source="user_directive")
    res = await _recall({}, _tool_ctx(user_memory_store=store, notes_store=notes))
    assert res["found"] is True
    assert any("SEC cross-check" in item for item in res["items"])


# ═══ D · Candidate approval path ═══════════════════════════════════════════════════


@pytest.fixture
async def governance_stores(tmp_path):
    db_path = tmp_path / "memory.db"
    memory = UserMemoryStore(db_path)
    await memory.initialize()
    candidates = MemoryCandidateStore(db_path=str(db_path), fingerprint_key="test-fp")
    await candidates.initialize()
    governance = PersonalMemoryGovernanceStore(db_path, root_secret="test-gov")
    return memory, candidates, governance


@pytest.mark.asyncio
async def test_candidates_persist_the_supersedes_slot(governance_stores):
    """`supersedes` 落库才叫「开了个槽」。此前模型只能写进 `reason`
    —— `enqueue_if_new` 把它原样落库，**没有任何代码读它**。"""
    _, candidates, _ = governance_stores
    cand = await candidates.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="profit_metric",
        value="毛利率",
        reason="user changed their mind",
        source_turn_id="t1",
        memory_type="semantic",
        supersedes=["mem_aaaaaaaaaaaa", "mem_aaaaaaaaaaaa", ""],
    )
    assert cand.supersedes == ("mem_aaaaaaaaaaaa",)
    assert cand.memory_type == "semantic"
    reloaded = await candidates.get(cand.id)
    assert reloaded.supersedes == ("mem_aaaaaaaaaaaa",)


@pytest.mark.asyncio
async def test_approving_a_candidate_that_contradicts_a_user_statement_is_refused(governance_stores):
    """★ 交付判据 ⑤：`low` 想改 `high` ⇒ 不覆盖。

    事务整体回滚 ⇒ **候选保持 pending**（= 落成候选而不是覆盖），用户可以先去改/删
    那条旧记忆再来批。路由把它映射成 409 `memory_confidence_conflict`。
    """
    memory, candidates, governance = governance_stores
    mine = await memory.commit_memory_write(
        user_id="u1",
        kind="preference",
        key="profit_metric",
        value="内部盈利只看净利率",
        source="user_directive",
    )
    cand = await candidates.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="profit_metric",
        value="内部盈利只看毛利率",
        reason="观察到用户提到毛利率",
        source_turn_id="t2",
    )
    with pytest.raises(MemoryConfidenceConflictError):
        await governance.approve_candidate(user_id="u1", candidate_id=cand.id, target="structured", decided_by="u1")

    [current] = await memory.list_product_for_user("u1")
    assert current.id == mine.entry.id
    assert current.parsed_value() == "内部盈利只看净利率"
    assert (await candidates.get(cand.id)).status == "pending"


@pytest.mark.asyncio
async def test_approving_executes_the_declared_supersede(governance_stores):
    """槽真的被执行：不再是「模型说了、没人听」。"""
    memory, candidates, governance = governance_stores
    old = await memory.commit_memory_write(
        user_id="u1",
        kind="preference",
        key="ranking_top_n",
        value="排名图只看 Top 10",
        source="dreaming_light_approved",
    )
    cand = await candidates.enqueue_if_new(
        user_id="u1",
        kind="constraint",
        key="ranking_scope",
        value="排名一律只取前五名",
        reason="用户改口",
        source_turn_id="t2",
        memory_type="procedural",
        supersedes=[old.entry.id],
    )
    approved = await governance.approve_candidate(user_id="u1", candidate_id=cand.id, target="structured", decided_by="u1")
    active = await memory.list_product_for_user("u1")
    assert [e.key for e in active] == ["ranking_scope"]
    assert active[0].confidence == "low"  # Candidate source ⇒ low, not promoted just because the user approved it.
    assert active[0].memory_type == "procedural"
    [trashed] = await memory.list_trashed("u1")
    assert trashed.id == old.entry.id
    assert trashed.superseded_by == active[0].id
    assert approved.status == "approved"


@pytest.mark.asyncio
async def test_approving_a_verbatim_repeat_adds_no_row(governance_stores):
    memory, candidates, governance = governance_stores
    existing = await memory.commit_memory_write(
        user_id="u1",
        kind="preference",
        key="ranking_top_n",
        value="排名图只看 Top 10",
        source="dreaming_light_approved",
    )
    cand = await candidates.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="ranking_top_n_v2",
        value="排名图只看Top10。",
        reason="又提了一次",
        source_turn_id="t2",
    )
    approved = await governance.approve_candidate(user_id="u1", candidate_id=cand.id, target="structured", decided_by="u1")
    assert approved.approved_resource_id == existing.entry.id
    assert len(await memory.list_for_user("u1")) == 1


@pytest.mark.asyncio
async def test_approval_redacts_the_supersedes_slot(governance_stores):
    """`supersedes` 也是内容（它暴露用户记过什么）。批准后候选行只留决策账，
    与 `key` / `value_json` / `reason` 同批脱敏。"""
    memory, candidates, governance = governance_stores
    old = await memory.commit_memory_write(user_id="u1", kind="preference", key="k1", value="旧的", source="dreaming_light_approved")
    cand = await candidates.enqueue_if_new(
        user_id="u1", kind="preference", key="k2", value="新的", reason="r", source_turn_id="t", supersedes=[old.entry.id]
    )
    await governance.approve_candidate(user_id="u1", candidate_id=cand.id, target="structured", decided_by="u1")
    async with aiosqlite.connect(candidates.db_path) as db:
        [(raw,)] = await db.execute_fetchall("SELECT supersedes_json FROM memory_candidates WHERE id=?", (cand.id,))
    assert raw is None


# ═══ E · Distillation: slots and ids must share one gate ════════════════════════════


#: **Verbatim original** pre-batch-1 `DREAMING_SYSTEM` (from `git show <before batch 1>:candidates.py`).
#: ★ This copy is the byte-invariance control for gate-off. Comparing `build_dreaming_system(canonical=False)`
#: to `DREAMING_SYSTEM` is self-validation: the latter is built by the former and always equals it.
_PROMPT_BEFORE_BATCH1 = """你是 Bowhead 的 Dreaming Light 观察员。你看完了一次数据分析的对话，需要提议 0-3 条**候选用户偏好**记录到长期记忆。

候选字段：
- kind: preference | constraint | datarc_binding | note
- key:  短标识（英文/拼音，snake_case）；kind=note 时可空字符串
- value: 值（字符串 / 对象）。kind=note 时是一句完整的中文/英文 bullet text (≤200 char)
- reason: 一句话解释为什么要记下来

kind 选择 (V3.5 PRD-C 加 'note'):
- 结构化 (preference / constraint / datarc_binding): key=value 形式, 适合明确字段映射
- note: 自由文本 bullet, 适合"用户老板叫张总" / "周报必带 SEC cross-check" 这类难塞 key=value 的偏好

判定规则：
1. 非常严格。只在用户**明确表达偏好**时才记（不是一次性用法）。例：用户说"我更喜欢看环比而不是同比" → 记。
2. 不记一次性查询（"今年 GMV"）。
3. 不记敏感数据（手机号、邮箱、金额）。
4. 不记 LLM 自己的假设。
5. 如果输入里带了「已有记忆与待确认项」：**不要重复提议其中已覆盖的内容**（含换措辞的同义重复）；
   如果本次对话与已有记忆矛盾（用户变心），提议**更新**——输出新值，reason 里注明覆盖了哪条旧记忆。

输出 JSON：
{"candidates": [
  {"kind": "preference", "key": "prefer_qoq", "value": "always show QoQ in trend reports",
   "reason": "user said `我更喜欢看环比` in this turn"},
  {"kind": "note", "key": "", "value": "用户老板叫张总, 周报里别提 GMV 提交易额",
   "reason": "user mentioned boss naming + reporting style"}
]}

如果没有值得记的 → {"candidates": []}。只输出 JSON。"""


def test_the_flag_replaces_the_free_text_instruction_with_a_parsed_slot():
    """★ 这就是 FR3 的根因所在：规则 5 一直让模型「在 reason 里注明取代了哪条」，
    而 `reason` 没有任何代码解析 ⇒ 「提议更新」永远只是句话。"""
    from bowhead_memory.candidates import build_dreaming_system

    prompt = build_dreaming_system(canonical=True)
    assert "reason 里注明覆盖了哪条旧记忆" not in prompt
    assert "填进 `supersedes`" in prompt
    assert "只写在 reason 里没有用" in prompt
    assert "- memory_type:" in prompt
    assert '"supersedes": ["mem_' in prompt  # Examples need it too, or the model copies [] from them.


class _DigestOrch:
    def __init__(self, memory_store):
        self.user_memory_store = memory_store
        self.memory_candidate_store = None
        self.memory_notes_store = None


@pytest.mark.asyncio
async def test_the_digest_hands_the_model_the_ids_it_is_asked_to_fill(store):
    """★ 槽与 id 必须**同一个闸**：模型只有在「已有记忆」里看得见 `[mem_xxx]`
    才填得出 `supersedes`。只改 prompt 不给 id = 让模型编一个 id 出来。
    顺带：运维行不该被当成可取代的记忆递过去。"""
    from bowhead_core.turn_runner import _existing_memory_digest

    written = await store.commit_memory_write(user_id="u1", kind="preference", key="kpi_money_unit", value="KPI 营收用百万", source="user_directive")
    await store.set_tool_context("u1", "bi", context_id="1", context_name="商超零售场景")
    digest = await _existing_memory_digest(_DigestOrch(store), "u1")
    assert f"[{written.entry.id}]" in digest
    assert "商超零售场景" not in digest


# ═══ F · UI writes (/memory page) must receive high too ══════════════════════════════


@pytest.mark.asyncio
async def test_manual_creation_from_the_memory_page_is_high_confidence(store):
    """★ 少了这条，用户在 `/memory` 页亲手写下的记忆在裁决表里只算 `low`，
    后台推断的候选一 approve 就能把它盖掉 —— 那正是这张表要挡的那一格。
    这条走的是 `upsert`（路由 POST /memory/user/remember 的路），不是 `commit_memory_write`。"""
    entry = await store.upsert_product_memory(user_id="u1", kind="preference", key="k1", value="百万", source="user_explicit")
    assert entry.confidence == "high"
    assert entry.last_confirmed_at is not None
    blocked = await store.commit_memory_write(user_id="u1", kind="preference", key="k1", value="亿", source="dreaming_light_approved")
    assert blocked.action == "blocked"


@pytest.mark.asyncio
async def test_editing_on_the_memory_page_is_high_and_keeps_the_previous_value(store):
    created = await store.upsert_product_memory(user_id="u1", kind="preference", key="k1", value="百万", source="user_explicit")
    edited = await store.patch_product_value(user_id="u1", entry_id=created.id, expected_version=created.version, edit_schema="text_v1", value="亿")
    assert edited.confidence == "high"
    assert json.loads(edited.previous_value) == "百万"


@pytest.mark.asyncio
async def test_batch1_does_not_split_one_key_across_scopes(store):
    """⚠ 作用域语义归 批4（`FEATURE_MEMORY_SCOPE_WRITE`），本批一个字不改。

    如果这里提前按作用域分片，同一个 key 会在全局与空间各留一条，
    而注入面读的是「全局 ∪ 当前空间」⇒ 两条一起进 prompt = 用取代做出了矛盾。
    """
    scoped = await store.upsert_product_memory(
        user_id="u1", kind="preference", key="ranking_top_n", value="Top 10", source="user_explicit", workspace_id="ws_a", scope_specified=True
    )
    written = await store.commit_memory_write(user_id="u1", kind="preference", key="ranking_top_n", value="Top 5", source="user_directive")
    assert written.entry.id == scoped.id
    assert written.entry.workspace_id == "ws_a"
    assert len(await store.list_for_user("u1")) == 1


@pytest.mark.asyncio
async def test_a_verbatim_repeat_still_executes_a_declared_supersede(store):
    """「还是只看 Top 5，把 Top 10 那条去掉」—— 复述旧口径 **同时** 点名取代另一条。
    逐字判重命中后如果早返回，`supersedes` 会被静默吞掉，旧条继续进 prompt。"""
    old = await _write(store, key="ranking_top_n", value="排名图只看 Top 10")
    keep = await _write(store, kind="constraint", key="ranking_scope", value="排名一律只取前五名")
    again = await _write(
        store,
        kind="constraint",
        key="ranking_scope",
        value="排名一律只取前五名。",
        supersedes=[old.entry.id],
    )
    assert again.action == "confirmed"
    assert again.entry.id == keep.entry.id
    assert again.superseded == (old.entry.id,)
    assert [e.id for e in await store.list_product_for_user("u1")] == [keep.entry.id]


@pytest.mark.asyncio
async def test_a_trashed_high_entry_no_longer_blocks_a_low_write(store):
    """用户已经把它删了，让它继续拦着等于让一条被删除的记忆替用户做主。"""
    mine = await _write(store, key="profit_metric", value="内部盈利只看净利率")
    assert await store.soft_delete(user_id="u1", entry_id=mine.entry.id)
    revived = await _write(store, key="profit_metric", value="内部盈利只看毛利率", source="dreaming_light_approved")
    assert revived.action == "updated"
    assert revived.entry.confidence == "low"
    assert revived.entry.deleted_at is None


@pytest.mark.asyncio
async def test_approving_a_verbatim_repeat_still_executes_its_supersede(governance_stores):
    """approve 路与 `commit_memory_write` 同一条纪律：逐字判重命中后不早返回，
    否则候选带的 `supersedes` 被静默吞掉、被取代那条继续进 prompt。"""
    memory, candidates, governance = governance_stores
    old = await memory.commit_memory_write(user_id="u1", kind="preference", key="ranking_top_n", value="Top 10", source="dreaming_light_approved")
    keep = await memory.commit_memory_write(
        user_id="u1", kind="constraint", key="ranking_scope", value="只取前五名", source="dreaming_light_approved"
    )
    cand = await candidates.enqueue_if_new(
        user_id="u1",
        kind="constraint",
        key="ranking_scope_v2",
        value="只取前五名。",
        reason="又提了一次，并且要把 Top 10 去掉",
        source_turn_id="t3",
        supersedes=[old.entry.id],
    )
    approved = await governance.approve_candidate(user_id="u1", candidate_id=cand.id, target="structured", decided_by="u1")
    assert approved.approved_resource_id == keep.entry.id
    assert [e.id for e in await memory.list_product_for_user("u1")] == [keep.entry.id]
    [trashed] = await memory.list_trashed("u1")
    assert trashed.id == old.entry.id
    assert trashed.superseded_by == keep.entry.id


# ═══ Actual gaps found by batch 1 review ════════════════════════════════════════════


def test_comparison_operators_are_not_erased_as_punctuation():
    """★★ 最狠的一条：`_normalize` 把所有标点当排版符号抹掉，于是
    `毛利率 ≥ 30%` 与 `毛利率 ≤ 30%` 都变成 `毛利率30` ⇒ 逐字判重命中 ⇒
    **新值根本没写进去**，而回执还告诉用户「已记住」。两条相反的记忆判等，
    是这套判重能犯的最严重的错。"""
    for a, b in [
        ("毛利率 ≥ 30%", "毛利率 ≤ 30%"),
        ("同比增长 +5%", "同比增长 -5%"),
        ("预算 > 100 万", "预算 < 100 万"),
    ]:
        assert not is_verbatim_duplicate(a, b), (a, b)
    # positive control: genuinely identical statements remain equal (whitespace/Chinese punctuation still ignored).
    assert is_verbatim_duplicate("毛利率 ≥ 30%", "毛利率≥30%。")


@pytest.mark.asyncio
async def test_upsert_never_downgrades_a_high_confidence_row(store):
    """★ `upsert` 不过裁决表。它若直接 COALESCE 写 confidence，就会把用户手改出来的
    `high` 悄悄写回 `low` —— 真实调用点是 undo-reset 恢复（用行里的旧 source 回灌）。
    降级之后，后台推断就能盖掉用户亲手写的那一版，正是裁决表要挡的那一格。"""
    created = await store.upsert_product_memory(user_id="u1", kind="preference", key="k1", value="百万", source="user_explicit")
    assert created.confidence == "high"
    # Simulate undo-reset: restore the same entry using a candidate-source value.
    restored = await store.upsert(user_id="u1", kind="preference", key="k1", value="百万", source="dreaming_light_approved")
    assert restored.confidence == "high", "high 不许被降级"


@pytest.mark.asyncio
async def test_reviving_a_superseded_key_also_supersedes_its_successor(store):
    """★ 复活语义只对「用户自己删掉的行」成立。被**取代**才躺在回收站里的行，
    直接 `deleted_at=NULL` 会让它和继任者同时活着 —— 取代机制自己造出一对矛盾。

    场景：先「只看 Top 5」（top_n 被 top_n_v2 取代），再「改成 Top 3」而模型
    **按提示词复用了 top_n**（提示词正是这么要求的）⇒ prompt 里同时出现 Top 3 与 Top 5。
    """
    old = await _write(store, key="top_n", value="排名图只看 Top 10")
    newer = await _write(store, kind="constraint", key="top_n_v2", value="排名一律只取前五名", supersedes=[old.entry.id])
    assert newer.superseded == (old.entry.id,)

    revived = await _write(store, key="top_n", value="排名图只看 Top 3")
    active = await store.list_product_for_user("u1")
    assert [e.parsed_value() for e in active] == ["排名图只看 Top 3"], [e.parsed_value() for e in active]
    assert newer.entry.id in revived.superseded


@pytest.mark.asyncio
async def test_the_legacy_notes_shortcut_does_not_swallow_supersedes(notes, store):
    """★ 同一个形态第三次出现（前两处已在 store 与 approve 各修过一次）：
    「还是只看 Top 5，把 Top 10 那条去掉」—— 话被 notes 兜底当成重复直接返回，
    `supersedes` 整个丢掉，Top 10 继续进 prompt。"""
    old = await store.commit_memory_write(user_id="u1", kind="preference", key="top_n", value="排名图只看 Top 10", source="user_directive")
    await notes.upsert_content(user_id="u1", content="• 偏好：只看 Top 5", source="user_directive")
    res = await _remember(
        {
            "kind": "preference",
            "key": "ranking_scope",
            "text": "只看 Top 5",
            "memory_type": "procedural",
            "supersedes": [old.entry.id],
        },
        _tool_ctx(user_memory_store=store, notes_store=notes),
    )
    assert res["ok"] is True
    assert res.get("superseded") == [old.entry.id], res
    assert [e.id for e in await store.list_trashed("u1")] == [old.entry.id]


@pytest.mark.asyncio
async def test_the_canonical_path_still_enforces_a_length_limit(notes, store):
    """★ notes 路有 `MAX_BULLET_CHARS`（超了抛 `NotesCharLimitError`），改道之后
    这层保护整个没了：8KB 正文原样落库，然后**每一轮**都注入进 prompt；
    而回执里的 `stored` 是截断到 500 字的 bullet，工具描述又要模型「引用 stored
    确认存了什么」⇒ 模型确认的字与真正存下去的**不一样**。"""
    res = await _remember(
        {"kind": "note", "key": "long_note", "text": "字" * 800, "memory_type": "semantic"},
        _tool_ctx(user_memory_store=store, notes_store=notes),
    )
    assert res["ok"] is False
    assert res["error"] == "rejected"
    assert await store.list_for_user("u1") == []


@pytest.mark.asyncio
async def test_the_distill_digest_never_hands_over_half_an_id(store):
    """★ 条目带 id 之后每行多 18 字符，`block[:600]` 会把 `[mem_1bf370c9c86a]`
    切成 `[mem_1bf370`。模型照抄进 `supersedes` ⇒ 目标查不到 ⇒ 静默丢弃，
    看起来又像「模型不够聪明」—— 而这批存在的理由就是要停止那样归因。"""
    import re

    from bowhead_core.turn_runner import _existing_memory_digest

    for i in range(40):
        await store.commit_memory_write(
            user_id="u1", kind="preference", key=f"k{i:02d}", value=f"这是第 {i} 条足够长的偏好用来把预算撑爆" * 2, source="user_directive"
        )
    digest = await _existing_memory_digest(_DigestOrch(store), "u1")
    ids = re.findall(r"\[mem_[0-9a-f]*\]?", digest)
    assert ids, digest
    for token in ids:
        assert re.fullmatch(r"\[mem_[0-9a-f]{12}\]", token), f"截断切碎了 id: {token!r}"
    # ★ Pin the **call site** too: helper tests alone still pass if the call reverts to `block[:600]`
    # when the hard cutoff happens inside a value, escaping the id checks above.
    # Id prefixes consume about 30% of the old budget, so canonical tier must expand to 900,
    # or it removes existing memories used by rule 5 to prevent repeated proposals.
    assert len(digest) > 620, f"canonical 档的 digest 预算没放宽（len={len(digest)}）"


def test_truncating_the_digest_drops_whole_lines_not_half_an_id():
    """★ 直接钉助手函数：整合用例（40 条真记忆）**不可靠** —— 硬切正好落在
    id 中间才会红，落在 value 里就绿了，突变实测就是这样漏过去的。
    这里把切点**精确摆在 id 中间**。"""
    from bowhead_core.turn_runner import _truncate_whole_lines

    block = "- User preferences:\n    · [mem_1bf370c9c86a] a = 1\n    · [mem_52ab7fede0b3] b = 2"
    # Budget ends inside the second line's id: hard truncation yields `· [mem_1bf370`.
    budget = block.index("mem_1bf370c9c86a") + 8
    out = _truncate_whole_lines(block, budget)
    assert "mem_1bf370" not in out or "[mem_1bf370c9c86a]" in out, out
    assert out == "- User preferences:", out
    # Enough budget returns text unchanged.
    assert _truncate_whole_lines(block, 10_000) == block


@pytest.mark.asyncio
async def test_a_corrected_proposal_carrying_supersedes_is_not_suppressed_by_the_stale_one(tmp_path):
    """★ 这是**本批上线路径上**的 bug：翻闸前 Dreaming 已把「净利率口径=X」入过队
    （`supersedes_json=NULL`，pending 去重没有时间窗，会一直躺着）。翻闸后模型终于
    补上 `supersedes:['mem_abc']`，值一模一样 ⇒ 指纹命中 ⇒ `return None`，
    **取代声明被永久丢弃**，而那条陈旧候选被批准时什么也取代不了。"""
    from bowhead_memory.candidates import MemoryCandidateStore

    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"), fingerprint_key="fp")
    await store.initialize()
    stale = await store.enqueue_if_new(user_id="u1", kind="preference", key="profit", value="内部盈利只看毛利率", reason="r", source_turn_id="t1")
    assert stale is not None
    # Same value again without supersedes ⇒ still deduplicated (existing behavior preserved).
    assert (
        await store.enqueue_if_new(user_id="u1", kind="preference", key="profit", value="内部盈利只看毛利率", reason="r", source_turn_id="t2") is None
    )
    # Same value **with a supersession declaration** ⇒ a new claim, which must be admitted.
    corrected = await store.enqueue_if_new(
        user_id="u1", kind="preference", key="profit", value="内部盈利只看毛利率", reason="r", source_turn_id="t3", supersedes=["mem_abc123456789"]
    )
    assert corrected is not None
    assert corrected.supersedes == ("mem_abc123456789",)


@pytest.mark.asyncio
async def test_the_boot_backfill_keeps_the_supersedes_aware_fingerprint(tmp_path):
    """★ `_backfill_candidate_fingerprints` 在每次 `initialize()` 时给缺指纹的行补算。
    它若漏了 `supersedes_json`，下次启动就把带取代声明的行重算成**不带**的指纹，
    刚修好的碰撞当场复活 —— 而且是在无人注意的启动路径上。"""
    import aiosqlite
    from bowhead_memory.candidates import MemoryCandidateStore, _candidate_content_fingerprint

    db_path = str(tmp_path / "c.db")
    store = MemoryCandidateStore(db_path=db_path, fingerprint_key="fp")
    await store.initialize()
    # Create a row with supersedes but no calculated fingerprint (the upgraded legacy-db shape).
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO memory_candidates(id,user_id,kind,key,value_json,reason,source_turn_id,"
            "created_at,updated_at,content_fingerprint,supersedes_json) "
            "VALUES ('c_old','u1','preference','k','\"值\"','r','t','2026-08-01','2026-08-01',NULL,'[\"mem_aaaaaaaaaaaa\"]')",
        )
        await db.commit()

    await MemoryCandidateStore(db_path=db_path, fingerprint_key="fp").initialize()

    async with aiosqlite.connect(db_path) as db:
        [(fp,)] = await db.execute_fetchall("SELECT content_fingerprint FROM memory_candidates WHERE id='c_old'")
    without = _candidate_content_fingerprint(secret=store._fingerprint_secret, kind="preference", value_json='"值"', workspace_id=None)
    with_sup = _candidate_content_fingerprint(
        secret=store._fingerprint_secret, kind="preference", value_json='"值"', workspace_id=None, supersedes_json='["mem_aaaaaaaaaaaa"]'
    )
    assert fp == with_sup, "回填漏了 supersedes_json"
    assert fp != without
