"""记忆条目详情面的后端读路（`docs/sprints/memory-spike-2026-08-29/memory-item-detail-design.md`）。

批0/批1 往 `memory_entries` 写了一批 per-条元数据（`confidence` / `memory_type` /
`previous_value` / `previous_value_at` / `superseded_by`），但**没有任何界面读得到**——
`CanonicalMemoryItemDTO` 上一个字段都没有。这里钉住那条读路。

★ 每条都做过突变验证（把实现改回去确认变红），等价突变结论记在各用例头上。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from test_memory_routes_v3 import (  # noqa: F401
    _client,
    _enable_memory_v2_stores,
    _hdrs,
    _seed_product_memory,
    app_ctx,
)

if TYPE_CHECKING:
    from bowhead_core import UserMemoryStore


async def _items(app, uid: str, state: str = "active") -> list[dict]:
    await _enable_memory_v2_stores(app)
    async with _client(app) as c:
        r = await c.get(f"/api/v1/memory/items?state={state}", headers=_hdrs(uid))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _set_cols(app, entry_id: str, **cols) -> None:
    """直接写批0 那几列——它们只有 `commit_memory_write` 走 canonical 闸时才会被写，
    而本文件测的是**读路**，不该把闸的状态混进来当前置。"""
    mem: UserMemoryStore = app.state.user_memory_store
    import aiosqlite

    sets = ", ".join(f"{k}=?" for k in cols)
    async with aiosqlite.connect(mem._db_path) as db:  # noqa: SLF001
        await db.execute(f"UPDATE memory_entries SET {sets} WHERE id=?", (*cols.values(), entry_id))
        await db.commit()


@pytest.mark.anyio
async def test_superseded_by_is_resolved_to_the_key_not_the_raw_id(app_ctx):  # noqa: F811
    """★ 突变验证：把 `_entry_to_canonical` 里的 `superseded_keys=superseded_keys`
    去掉（回到批0 的形态）⇒ key 变 None，本用例红。

    等价突变：把 `_superseded_key_map` 的返回改成 `{}`，同样红 —— 说明断言咬的是
    「真的解析了」，不是「DTO 上有这个字段」。
    """
    app = app_ctx
    uid = "u_sup"
    winner = await _seed_product_memory(app, user_id=uid, key="ranking_scope", value="Top 5")
    loser = await _seed_product_memory(app, user_id=uid, key="default_rank_limit", value="Top 10")
    await _set_cols(app, loser, superseded_by=winner, deleted_at="2026-09-01T00:00:00+00:00")

    trashed = await _items(app, uid, state="trashed")
    row = next(i for i in trashed if i["id"] == loser)
    assert row["superseded_by"]["id"] == winner
    # Design says '已被『ranking_scope』取代': must obtain the key, not mem_xxx.
    assert row["superseded_by"]["key"] == "ranking_scope"


@pytest.mark.anyio
async def test_superseded_by_of_a_hard_deleted_target_resolves_to_none_not_a_made_up_key(app_ctx):  # noqa: F811
    """取代方被硬删之后，后端**不能**编一个 key 出来；前端据此走兜底文案。

    ★ 突变验证：让 `get_entries_by_ids` 对查不到的 id 回填 `{"key": entry_id}`
    （即「解不出来就把 id 当 key」）⇒ 本用例红。
    """
    app = app_ctx
    uid = "u_gone"
    loser = await _seed_product_memory(app, user_id=uid, key="k_loser", value="v")
    await _set_cols(app, loser, superseded_by="mem_deadbeefdeadbeef", deleted_at="2026-09-01T00:00:00+00:00")

    row = next(i for i in await _items(app, uid, state="trashed") if i["id"] == loser)
    assert row["superseded_by"]["id"] == "mem_deadbeefdeadbeef"
    assert row["superseded_by"]["key"] is None


@pytest.mark.anyio
async def test_id_to_key_resolution_does_not_cross_users(app_ctx):  # noqa: F811
    """★ 这是本文件里唯一的安全边界用例。

    突变验证：把 `get_entries_by_ids` 的 SQL 里 `subject_id=?` 去掉 ⇒ 本用例红
    （B 的 key 会被解析进 A 的响应）。id 全局唯一，少这个条件就是跨用户读。
    """
    app = app_ctx
    other = await _seed_product_memory(app, user_id="u_b", key="b_secret_key", value="v")
    mine = await _seed_product_memory(app, user_id="u_a", key="a_key", value="v")
    await _set_cols(app, mine, superseded_by=other, deleted_at="2026-09-01T00:00:00+00:00")

    row = next(i for i in await _items(app, "u_a", state="trashed") if i["id"] == mine)
    assert row["superseded_by"]["id"] == other  # Echo ID unchanged (already stored in this own row).
    assert row["superseded_by"]["key"] is None  # But **cannot resolve** it: another user's entry.


@pytest.mark.anyio
async def test_previous_value_is_decoded_not_the_raw_json_string(app_ctx):  # noqa: F811
    """★ 突变验证：把 `_display_previous_value` 换成 `return raw` ⇒ 拿到 `"Top 10"`
    （带引号），本用例红。同一张卡上「当前值」是解码过的，上一版不解码就会两种渲染并存。
    """
    app = app_ctx
    uid = "u_prev"
    eid = await _seed_product_memory(app, user_id=uid, key="rank_limit", value="Top 5")
    await _set_cols(
        app,
        eid,
        previous_value=json.dumps("Top 10"),
        previous_value_at="2026-08-20T10:00:00+00:00",
    )

    row = next(i for i in await _items(app, uid) if i["id"] == eid)
    assert row["previous_value"] == "Top 10"
    assert '"' not in row["previous_value"]
    assert row["previous_value_at"] == "2026-08-20T10:00:00+00:00"


@pytest.mark.anyio
async def test_unlabelled_row_reports_null_not_a_defaulted_value(app_ctx):  # noqa: F811
    """画面 G（未标注态）：迁移前写入的行 `confidence` / `memory_type` 都是 NULL。

    ★ 突变验证：把 DTO 上的 `memory_type` 兜成 `normalize_memory_type(...)` 或
    `"semantic"` ⇒ 本用例红。把没人判过的值当事实展示，比留空更糟（设计口径 §71）。
    """
    app = app_ctx
    uid = "u_bare"
    eid = await _seed_product_memory(app, user_id=uid, key="legacy_key", value="v")
    # ⚠ canonical defaults on since 2026-09-04; `upsert_product_memory` also stamps confidence,
    # so this row is no longer naturally unannotated. Explicitly clear it to test **read-path NULL rendering**,
    # independent of writes accidentally leaving it empty, as the test should have done.
    await _set_cols(app, eid, confidence=None, memory_type=None)

    row = next(i for i in await _items(app, uid) if i["id"] == eid)
    assert row["confidence"] is None
    assert row["memory_type"] is None
    assert row["previous_value"] is None
    assert row["superseded_by"] is None


@pytest.mark.anyio
async def test_labelled_row_passes_confidence_and_type_through(app_ctx):  # noqa: F811
    """★ 突变验证：把 `confidence=getattr(entry, "confidence", None)` 删掉 ⇒ 红。"""
    app = app_ctx
    uid = "u_lab"
    eid = await _seed_product_memory(app, user_id=uid, key="k", value="v")
    await _set_cols(app, eid, confidence="high", memory_type="procedural")

    row = next(i for i in await _items(app, uid) if i["id"] == eid)
    assert row["confidence"] == "high"
    assert row["memory_type"] == "procedural"


@pytest.mark.anyio
async def test_candidate_supersedes_is_resolved_to_key_and_current_value(app_ctx):  # noqa: F811
    """画面 D：「保存后会取代 N 条现有记忆」要说清楚被取代的是哪一条、现在写着什么。

    ★ 突变验证：把 `candidate_to_dto` 里的 `supersedes=[...]` 改回 `supersedes=[]`
    （即批1 review 轮之后 DTO 一直的样子）⇒ 本用例红。
    等价突变：把列表端点的 `supersedes_items=supersedes_items` 去掉 ⇒ 同样红，
    说明断言咬的是「真解析了」而不是「字段存在」。
    """
    app = app_ctx
    await _enable_memory_v2_stores(app)
    uid = "u_cand"
    target = await _seed_product_memory(app, user_id=uid, key="default_rank_limit", value="Top 10")

    await app.state.memory_candidate_store.enqueue(
        user_id=uid,
        kind="preference",
        key="ranking_scope",
        value="Top 5",
        reason="用户改了口径",
        source_turn_id="turn_1",
        supersedes=(target,),
    )

    async with _client(app) as c:
        r = await c.get("/api/v1/memory/candidates", headers=_hdrs(uid))
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert [s["id"] for s in item["supersedes"]] == [target]
    assert item["supersedes"][0]["key"] == "default_rank_limit"
    assert item["supersedes"][0]["current_value"] == "Top 10"


@pytest.mark.anyio
async def test_candidate_supersedes_drops_ids_that_no_longer_exist(app_ctx):  # noqa: F811
    """★ 解不出来的 id 必须**丢掉**，不能补空壳。

    突变验证：把 `candidate_to_dto` 的推导式改成对解不出来的 id 回填
    `MemoryCandidateSupersedesItemDTO(id=sid)` ⇒ 本用例红。
    留着它，卡片会报一个「会取代 1 条」——而那条记忆已经不在了，取代不会发生。
    """
    app = app_ctx
    await _enable_memory_v2_stores(app)
    uid = "u_cand_gone"
    await app.state.memory_candidate_store.enqueue(
        user_id=uid,
        kind="preference",
        key="ranking_scope",
        value="Top 5",
        reason="r",
        source_turn_id="turn_1",
        supersedes=("mem_deadbeefdeadbeef",),
    )

    async with _client(app) as c:
        r = await c.get("/api/v1/memory/candidates", headers=_hdrs(uid))
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["supersedes"] == []


@pytest.mark.anyio
async def test_migrated_entry_says_where_it_came_from(app_ctx):  # noqa: F811
    """§5 迁移进来的条目，来源是**确切知道**的，不该显示「最初来源不可用」。

    ★ 116 真机暴露的（2026-09-04 迁移跑完后）：5 条真条目全落到兜底文案上。
    写侧 `created_source_type=legacy_notes_migration` 早就修好了，读侧的
    `_created_from_label` 一直没跟上 —— 数据对、界面错。

    ★ 突变验证：把 `_created_from_label` 里那个分支删掉 ⇒ 本用例红
    （拿回「最初来源不可用」）。
    """
    app = app_ctx
    uid = "u_migrated"
    eid = await _seed_product_memory(app, user_id=uid, key="profit_metric", value="只看净利率")
    await _set_cols(app, eid, created_source_type="legacy_notes_migration")

    row = next(i for i in await _items(app, uid) if i["id"] == eid)
    assert row["provenance"]["created_from"]["type"] == "legacy_notes_migration"
    assert row["provenance"]["created_from"]["source_label"] == "迁移自旧版记忆"


@pytest.mark.anyio
async def test_an_auto_captured_entry_does_not_claim_the_user_confirmed_it(app_ctx):  # noqa: F811
    """★★ 零点击直写的条目，来源行**不许说「你确认过」** —— 用户一次都没点。

    这是 `legacy_notes_migration` 那次的同族：写侧登记了新来源、读侧没跟上，
    于是内部标识 `feedback_confirmed` 原样漏进 API，文案落到兜底的
    「最初来源不可用」。数据对、界面错。

    ★ 两条断言各管一半：
    - `type` 是 `auto_captured` 而**不是** `feedback_confirmed`（内部标识不出 API），
      也不是 `candidate_approved`（那条用户点过，撤销引导指的地方不一样）；
    - `source_label` 不含「确认」二字 —— 说「你确认过」就是回执在说谎。

    ★ 突变验证：删掉 `_created_from_label` 的分支 ⇒ 红（拿回兜底文案）；
    删掉 `_created_from_type` 的分支 ⇒ 红（内部标识 `feedback_confirmed` 漏进 API）。
    ⚠ **等价突变**：把 label 分支挪到 `candidate` 那条之后 —— 实测**不红**。
    自动记的条目 `source`/`type` 都是 `feedback_confirmed`，既不含 dreaming
    也不是 candidate，两条分支互不相交，顺序在这里不影响结果。
    （我初版把「挪后面也会红」写进了注释，是错的，已按实测改。）
    """
    app = app_ctx
    uid = "u_autocap"
    eid = await _seed_product_memory(app, user_id=uid, key="ranking_chart_type", value="排名图用横向条形图")
    await _set_cols(app, eid, created_source_type="feedback_confirmed")

    row = next(i for i in await _items(app, uid) if i["id"] == eid)
    created_from = row["provenance"]["created_from"]
    assert created_from["type"] == "auto_captured"
    assert "确认" not in created_from["source_label"], created_from["source_label"]
    assert created_from["source_label"] == "根据你在对话中的要求自动记下"


#: (created_source_type, source) combinations **actually observed** in 116 production (2026-09-10).
#: ★ Corpus uses real data, not presumed names; this guard exists because
#: reads previously compared `user_explicit`, never produced in production (`user_directive` 8 rows /
#: `user_explicit` 0), leaving 3 of 10 visible items showing '最初来源不可用'.
_PRODUCTION_SOURCES = [
    ("legacy_notes_migration", "user_directive"),
    ("user_directive", "user_directive"),
    ("candidate", "dreaming_light_approved"),
    ("feedback_confirmed", "feedback_confirmed"),
    ("tool_runtime", "tool_runtime"),
    ("bi_runtime", "bi_runtime"),
    ("cron_aggregator", "cron_aggregator"),
    # ⚠ Corpus must include **actual combinations of both columns**. Most common operational shape on 116 is
    # `created_source_type=NULL` (8 rows), but my first corpus copied one partial query by hand,
    # containing only `('tool_runtime','tool_runtime')`⇒guard green, production still falls back.
    # Corpus must come from the complete `SELECT DISTINCT created_source_type, source`, not hand entry.
    (None, "tool_runtime"),
    (None, "bi_runtime"),
    (None, "cron_aggregator"),
    (None, "ws-autosync"),
    # Dated sync variants: both coexist on 116.
    (None, "ws-sync-datarc-20260607"),
]


def test_no_production_source_falls_back_to_source_unavailable() -> None:
    """★★★ 逐条修了四次之后的**结构性守卫**：生产上真实出现过的来源，一个都不许落兜底。

    此前是打地鼠 —— `legacy_notes_migration` / `feedback_confirmed` / `import`
    各修一次，而**最主要的 `user_directive`（remember 工具写的）一直漏着**。

    ★ 突变验证：把 `user_directive` 从 `_created_from_label` 那个集合里去掉
    ⇒ 本用例红。
    """
    from bowhead_gateway.routes.memory import _created_from_label

    fell_back = [(cst, src) for cst, src in _PRODUCTION_SOURCES if _created_from_label(cst, src) == "最初来源不可用"]
    assert not fell_back, f"这些生产来源没有文案：{fell_back}"


def test_internal_source_identifiers_never_leak_into_the_public_type() -> None:
    """★★ `created_from.type` 是**对外**字段，不许把内部标识原样漏出去。

    原来最后一行是 `if created_source_type: return created_source_type`
    ⇒ `user_directive`、`tool_runtime` 这些内部名直接进了 API。

    ★ 突变验证：把 `_PUBLIC_SOURCE_TYPE` 的回落改回原样返回 ⇒ 本用例红。
    """
    from bowhead_gateway.routes.memory import _created_from_type

    public = {
        "candidate_approved",
        "explicit_instruction",
        "auto_captured",
        "legacy_notes_migration",
        "import",
        "operational",
        "legacy_unknown",
    }
    for cst, _src in _PRODUCTION_SOURCES:
        assert _created_from_type(cst) in public, f"{cst} 漏成了对外类型"
    # Negative control: unregistered values fall back to legacy_unknown, never pass through unchanged.
    assert _created_from_type("some_future_writer") == "legacy_unknown"


def test_every_production_source_has_a_localizable_label_key() -> None:
    """Clients render the source line from `source_label_key` in the interface language.

    The key must agree with the Chinese `source_label` kept for search and older clients, and every
    production source maps to a key from the fixed public set (never an internal identifier).
    """
    from bowhead_gateway.routes.memory import _SOURCE_LABEL_ZH, _created_from_label, _created_from_label_key

    for cst, src in _PRODUCTION_SOURCES:
        key = _created_from_label_key(cst, src)
        assert key in _SOURCE_LABEL_ZH, (cst, src, key)
        assert _SOURCE_LABEL_ZH[key] == _created_from_label(cst, src)
    assert _created_from_label_key("user_directive", "user_directive") == "told_in_chat"
    assert _created_from_label_key(None, "whatever") == "unavailable"


@pytest.mark.anyio
async def test_listed_items_carry_the_source_label_key(app_ctx):  # noqa: F811
    app = app_ctx
    uid = "u_label_key"
    eid = await _seed_product_memory(app, user_id=uid, key="profit_metric", value="只看净利率")
    await _set_cols(app, eid, created_source_type="legacy_notes_migration")

    row = next(i for i in await _items(app, uid) if i["id"] == eid)
    assert row["provenance"]["created_from"]["source_label_key"] == "legacy_migration"
