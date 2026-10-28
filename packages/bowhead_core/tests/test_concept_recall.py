"""Verify deterministic recall evidence, dimension/value pairing, and unusable fields.

Synthetic cases cover suffixed values, typed grouping, short tokens, and fair caps.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.concept_recall import (
    Match,
    _rank_and_cap,
    _verdict,
    grade_evidence,
    recall_tokens,
)
from bowhead_core.embed_search import EmbedHit, EmbedQueryResult

NEAR = 0.25


def _hit(doc: str, dim: str, dist: float, value: str | None = None) -> EmbedHit:
    return EmbedHit(doc=doc, dim_id=dim, value=value, dist=dist)


def _ctx(dims: list[Any] | None = None) -> Any:
    class _Datarc:
        async def list_dimensions(self, ws: str) -> list[Any]:
            return dims or []

    return SimpleNamespace(
        datarc=_Datarc(),
        resolve_datarc_project=lambda: "355",
        vector_client=object(),
        tenant="demo",
        ar2_thresholds=None,
    )


def _dim(i: int, name: str, used: bool = True) -> Any:
    return SimpleNamespace(id=i, names=[name], used=used)


# ── A1: never assume literal matching ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("token", "doc", "expect"),
    [
        ("产品甲", "产品甲", "literal"),
        # ★ Core A1 case: Chinese product dimension stores values with specification suffixes.
        ("产品甲", "产品甲 5", "token_in_doc"),
        ("产品甲", "产品甲5mg注射液", "token_in_doc"),
        ("Erbitux", "Erbitux 100mg vial", "token_in_doc"),
        ("北京", "北京市", "token_in_doc"),
        ("BURBERRY", "burberry", "literal"),  # Literal after normalization
    ],
)
def test_a1_containment_counts_as_dictionary_evidence(token: str, doc: str, expect: str) -> None:
    """带后缀/变体的值必须算「字典能答」，不能掉进 LLM 猜测路。"""
    assert grade_evidence(token, doc, 0.9, NEAR) == expect


@pytest.mark.parametrize(
    ("token", "doc"),
    [
        ("率", "达成率"),  # Single character: containment noise overwhelms signal
        ("销售", "销售数量增长率"),  # Insufficient two-character coverage
    ],
)
def test_d4_short_token_containment_does_not_explode(token: str, doc: str) -> None:
    """review D4：短 token 不许靠包含关系升到「字典能答」档。"""
    assert grade_evidence(token, doc, 0.9, NEAR) is None


@pytest.mark.anyio
async def test_a1_suffixed_value_does_not_fall_through_to_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """A suffixed synthetic value remains dictionary evidence without a literal hit.

    Exercise the final verdict as well as evidence grading so the dictionary-answer
    threshold cannot silently send a supported token to the guessing path.
    """

    async def fake_query(ctx: Any, *, query_texts: list[str], collection: str, n_results: int) -> Any:
        if collection == "keywords":
            hits = [_hit("产品甲 5", "d202", 0.31)]  # Deliberately distant: supported only by containment
            return [EmbedQueryResult(query=q, hits=hits) for q in query_texts]
        return [EmbedQueryResult(query=q, hits=[]) for q in query_texts]

    monkeypatch.setattr("bowhead_core.concept_recall.embed_query", fake_query)
    ctx = _ctx([_dim(202, "中文产品")])
    (rec,) = await recall_tokens(ctx, ["产品甲"], cap=5)

    assert rec.verdict == "unique", f"带后缀的值必须由词表定案，实得 {rec.verdict}"
    assert rec.matches[0].evidence == "token_in_doc"
    assert rec.matches[0].dim == "d202"
    assert rec.matches[0].value == "产品甲 5"


# ── A2 / D3: one match per hit; grouping key includes type ─────────────────────────


@pytest.mark.anyio
async def test_a2_dim_and_value_come_from_the_same_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A literal product match cannot inherit the majority dimension of other hits."""
    hits = [_hit("产品乙", "d199", 0.0)] + [_hit(f"代表{i}", "d173", 0.05 + i * 0.01) for i in range(9)]

    async def fake_query(ctx: Any, *, query_texts: list[str], collection: str, n_results: int) -> Any:
        if collection == "keywords":
            return [EmbedQueryResult(query=q, hits=hits) for q in query_texts]
        return [EmbedQueryResult(query=q, hits=[]) for q in query_texts]

    monkeypatch.setattr("bowhead_core.concept_recall.embed_query", fake_query)
    ctx = _ctx([_dim(199, "中文品牌"), _dim(173, "代表姓名")])
    (rec,) = await recall_tokens(ctx, ["产品乙"], cap=5)

    dv = [m for m in rec.matches if m.value == "产品乙"]
    assert dv, "逐字命中必须在返回里"
    assert dv[0].dim == "d199", f"值与维度必须同源，实得 {dv[0].dim}"
    # Majority dimension must never attach to this value.
    assert not any(m.value == "产品乙" and m.dim == "d173" for m in rec.matches)


@pytest.mark.parametrize(
    ("token", "shorter", "shorter_type", "why"),
    [
        ("科室会", "科室", "dim_name", "维度名是 token 的子串（116 实测最大噪声源）"),
        ("管理科室", "科室", "dim_name", "同上"),
        ("不是新经销商", "经销商", "dim_value", "更短的**值**被包含"),
        ("广州嘉事百洲医疗器械有限公司-二级", "广州嘉事百洲医疗器械有限公司", "dim_value", "同一家公司的短名"),
    ],
)
def test_shorter_contained_match_does_not_open_an_ambiguity(token: str, shorter: str, shorter_type: str, why: str) -> None:
    """★ 116 dogfood 定的线：`doc_in_token` 能当答案，但**不开歧义组**。

    用户打的串比词表里的值更长时，他说的是更具体的那个东西；词表里恰好有个更短的
    串被包含，不是「两种读法」。修前这一类把 ambiguous 率抬到 55%，每个都会弹卡——
    而误弹正是 bi-hitl-cleanup 当初砍掉自动弹卡的理由（37 张卡 22 张无人作答）。
    """
    hit = Match(type="dim_value", dim="d1", dim_name="主维度", value=token, evidence="literal", dist=0.0)
    noise = Match(type=shorter_type, dim="d2", dim_name="另一维度", value=shorter, evidence="doc_in_token", dist=0.1)  # type: ignore[arg-type]
    assert _verdict([hit, noise], []) == "unique", why


def test_doc_in_token_alone_still_answers() -> None:
    """但它单独出现时仍要能当答案：用户打「北京市」，词表里是「北京」。"""
    m = Match(type="dim_value", dim="d1", dim_name="市", value="北京", evidence="doc_in_token", dist=0.05)
    assert _verdict([m], []) == "unique"


def test_d3_group_key_includes_type() -> None:
    """同一个 d162 的维度名与维度值是两种语义，不能算作同一组。"""
    name = Match(type="dim_name", dim="d162", dim_name="大区", value="大区", evidence="literal", dist=0.0)
    val = Match(type="dim_value", dim="d162", dim_name="大区", value="华东大区", evidence="literal", dist=0.0)
    assert name.group_key != val.group_key
    assert _verdict([name, val], []) == "ambiguous"


# ── A3: used=false checked on schema side ─────────────────────────────────────────


@pytest.mark.anyio
async def test_a3_unused_dimension_is_not_recommended(monkeypatch: pytest.MonkeyPatch) -> None:
    """「创建时间」在 schema coll 逐字命中，但 used=false ⇒ 不许进 matches。

    116 实测 23 个 used=false 维度里 20 个能被逐字召回；直接推荐会指向一个
    datarc 会静默兜底的字段（比返回 0 更危险）。
    """

    async def fake_query(ctx: Any, *, query_texts: list[str], collection: str, n_results: int) -> Any:
        hits = [_hit("创建时间", "d66", 0.0)] if collection == "schema" else []
        return [EmbedQueryResult(query=q, hits=hits) for q in query_texts]

    monkeypatch.setattr("bowhead_core.concept_recall.embed_query", fake_query)
    ctx = _ctx([_dim(66, "创建时间", used=False)])
    (rec,) = await recall_tokens(ctx, ["创建时间"], cap=5)

    assert rec.matches == [], "used=false 的维度不许被推荐"
    assert rec.verdict == "unusable_dimension"
    assert rec.unusable and rec.unusable[0]["dim"] == "d66"
    # Include name: not found differs from an existing field without data.
    assert rec.unusable[0]["dim_name"] == "创建时间"


@pytest.mark.anyio
async def test_a3_positive_control_used_true_still_recommended(monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：同样的形状，used=true 时必须照常返回。

    没有这条，上面那个用例即使在「所有维度都被丢掉」的坏实现下也会绿。
    """

    async def fake_query(ctx: Any, *, query_texts: list[str], collection: str, n_results: int) -> Any:
        hits = [_hit("创建时间", "d66", 0.0)] if collection == "schema" else []
        return [EmbedQueryResult(query=q, hits=hits) for q in query_texts]

    monkeypatch.setattr("bowhead_core.concept_recall.embed_query", fake_query)
    ctx = _ctx([_dim(66, "创建时间", used=True)])
    (rec,) = await recall_tokens(ctx, ["创建时间"], cap=5)

    assert rec.verdict == "unique"
    assert rec.matches and rec.matches[0].dim == "d66"
    assert rec.unusable == []


# ── Ambiguity must be visible ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ambiguous_across_dimensions_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep both interpretations when one synthetic token belongs to two dimensions."""

    async def fake_query(ctx: Any, *, query_texts: list[str], collection: str, n_results: int) -> Any:
        if collection == "keywords":
            hits = [_hit("产品甲", "d199", 0.0), _hit("产品甲", "d186", 0.02)]
            return [EmbedQueryResult(query=q, hits=hits) for q in query_texts]
        return [EmbedQueryResult(query=q, hits=[]) for q in query_texts]

    monkeypatch.setattr("bowhead_core.concept_recall.embed_query", fake_query)
    ctx = _ctx([_dim(199, "中文品牌"), _dim(186, "中文通用名")])
    (rec,) = await recall_tokens(ctx, ["产品甲"], cap=5)

    assert rec.verdict == "ambiguous"
    assert {m.dim for m in rec.matches} == {"d199", "d186"}


def test_grouped_ranking_keeps_one_representative_per_group() -> None:
    """§5.1：平排 top-N 会让强势维度挤掉另一维度的唯一代表，歧义就被抹平了。"""
    strong = [Match("dim_value", "d199", "品牌", f"值{i}", "literal", 0.0 + i * 0.001) for i in range(5)]
    lone = Match("dim_value", "d186", "通用名", "西妥昔单抗", "token_in_doc", 0.2)
    kept, truncated = _rank_and_cap(strong + [lone], cap=2)
    assert {m.dim for m in kept} == {"d199", "d186"}, "另一维度的代表必须活下来"
    assert truncated == 4


# ── Batching: N terms issue only 2 embedding requests ────────────────────────────────


# ── Four gaps found by batch 1 self-review (review-fix iteration) ─────────────────────


@pytest.mark.anyio
async def test_embed_raising_is_distinguishable_from_empty_dictionary(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 后端抛异常 ≠ 字典里真没有。

    初版 `except: continue` 让整批查询失败后 hits 为空 ⇒ verdict=not_found，
    运维故障伪装成「这个词不存在」——concept resolve 在两个私有化客户静默全 0
    就是这么藏住的，不能在重写里重犯。
    """

    async def boom(ctx: Any, *, query_texts: list[str], collection: str, n_results: int) -> Any:
        raise RuntimeError("embed 502")

    monkeypatch.setattr("bowhead_core.concept_recall.embed_query", boom)
    (rec,) = await recall_tokens(_ctx([]), ["产品甲"], cap=5)
    assert rec.embed_unavailable, "后端抛异常必须留痕，否则与「字典里没有」不可区分"
    assert rec.embed_unavailable.startswith("query_raised")


@pytest.mark.anyio
async def test_inventory_failure_is_visible_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """维度清单拿不到 ⇒ A3 过滤失效（fail-open），但必须能看见。

    fail-open 本身是有意的（宁可多答也不要整批哑火），**静默**才是问题：
    「used=false 的维度被推荐出去」线上会完全看不见。
    """

    class _Boom:
        async def list_dimensions(self, ws: str) -> list[Any]:
            raise RuntimeError("datarc down")

    async def fake_query(ctx: Any, *, query_texts: list[str], collection: str, n_results: int) -> Any:
        hits = [_hit("创建时间", "d66", 0.0)] if collection == "schema" else []
        return [EmbedQueryResult(query=q, hits=hits) for q in query_texts]

    monkeypatch.setattr("bowhead_core.concept_recall.embed_query", fake_query)
    ctx = _ctx([])
    ctx.datarc = _Boom()
    (rec,) = await recall_tokens(ctx, ["创建时间"], cap=5)
    assert rec.inventory_degraded is True, "清单拿不到必须标记，否则 A3 静默失效"


@pytest.mark.anyio
async def test_batched_two_calls_regardless_of_token_count(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_query(ctx: Any, *, query_texts: list[str], collection: str, n_results: int) -> Any:
        calls.append(collection)
        return [EmbedQueryResult(query=q, hits=[]) for q in query_texts]

    monkeypatch.setattr("bowhead_core.concept_recall.embed_query", fake_query)
    ctx = _ctx([])
    await recall_tokens(ctx, ["a", "b", "c", "d", "e", "f", "g", "h"], cap=5)
    assert calls == ["keywords", "schema"], f"8 个词应只发 2 次，实得 {calls}"


# Cap distinct values rather than rows. Repeated values in separate dimensions
# must not consume the capacity needed for another member of the same group.


def _mirrored(values: list[str]) -> list[Match]:
    """真形态：每个值都在两个维度下各出现一次。"""
    return [
        Match(type="dim_value", dim=d, dim_name=n, value=v, evidence="token_in_doc", dist=0.10 + i * 0.01)
        for i, v in enumerate(values)
        for d, n in (("d149", "上级机构名称"), ("d152", "客户名称"))
    ]


def test_mirrored_values_do_not_consume_the_cap() -> None:
    """★ 正主：五个院区各占两个维度，cap=5 时必须五个值全在。"""
    campuses = ["西院区", "东院区", "北院区", "南院区", "中院区"]
    out, truncated = _rank_and_cap(_mirrored(campuses), 5)
    assert {m.value for m in out} == set(campuses), "同值镜像吃掉了额度 —— 南院区又会丢"
    assert truncated == 0


def test_both_dims_are_still_returned_for_each_value() -> None:
    """搭便车的那条**必须照常回传**：agent 要靠它知道能按哪个字段筛。"""
    out, _ = _rank_and_cap(_mirrored(["西院区"]), 5)
    assert {m.dim for m in out} == {"d149", "d152"}


def test_distinct_values_still_hit_the_cap() -> None:
    """★ 正对照：真的是不同的值时 cap 照旧生效 —— 否则这个改动等于取消了上限。"""
    vals = [f"值{i}" for i in range(9)]
    singles = [Match(type="dim_value", dim="d1", dim_name="D", value=v, evidence="token_in_doc", dist=0.1 + i * 0.01) for i, v in enumerate(vals)]
    out, truncated = _rank_and_cap(singles, 4)
    assert len({m.value for m in out}) == 4
    assert truncated == 5, "truncated 要报「丢了几个**不同的值**」"


def test_group_representation_survives_the_dedup() -> None:
    """★ 不能为了去重破坏原有不变量（tech-design §5.1）：

    强势维度的多个值不许挤掉另一个维度的唯一代表。
    """
    strong = [Match(type="dim_value", dim="d1", dim_name="A", value=f"a{i}", evidence="literal", dist=0.01 * i) for i in range(6)]
    lone = [Match(type="dim_value", dim="d2", dim_name="B", value="b0", evidence="token_in_doc", dist=0.9)]
    out, _ = _rank_and_cap(strong + lone, 3)
    assert "d2" in {m.dim for m in out}, "另一个维度的唯一代表被挤掉了"
