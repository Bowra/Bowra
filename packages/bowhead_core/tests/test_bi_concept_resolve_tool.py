"""`bi_concept_resolve` tool tests.

**2026-08-28 concept-resolve-redo 批1 重写。** 旧文件的大半用例绑在
`_resolve_one_token` / `_shape_*` 这套「按 kind_hint 选一条路 → 只吐那条路的
一个 candidate」的架构上，那套已被删除（生产 57% 的调用因此完全跳过词表，
`dict_hit` 结构不可达）。词表召回本身的用例见 `test_concept_recall.py`；
这里只覆盖 **tool 层**：schema / 装配 / handler 合成 / 降级。

保留的不变量：
- I3: shape `{"results": [...]}`，len == len(tokens)
- I4: next_action_hint 枚举
- I5: clarification_payload.questions[0] 三字段（peer 要原样 forward，**逐字节不动**）
- I7: register 可导入可调用
- P1-CTO-2: 单 token 失败只让那个 token 降级，不炸整批
- 工具内部不调模型：词表答不了的概念由调用方 Agent 自己解释（2026-10-17 用户拍板）
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.bi_concept_resolve import (
    EMBED_UNAVAILABLE_PREFIX,
    _bi_concept_resolve_handler,
    _bi_concept_resolve_schema,
    _compose_result,
    register_bi_concept_resolve_tool,
)
from bowhead_core.concept_recall import Match, TokenRecall

_HINTS = {"use_top_candidate", "call_clarification_ask", "skip_token"}


def _ctx(profile: str = "compute") -> Any:
    return SimpleNamespace(_running_profile=profile)


def _rec(token: str, verdict: str, matches: list[Match] | None = None, **kw: Any) -> TokenRecall:
    return TokenRecall(token=token, verdict=verdict, matches=matches or [], **kw)


def _m(dim: str, value: str, *, mtype: str = "dim_value", ev: str = "literal", name: str = "") -> Match:
    return Match(type=mtype, dim=dim, dim_name=name, value=value, evidence=ev, dist=0.0)  # type: ignore[arg-type]


# ── Schema / wiring ─────────────────────────────────────────────────────────


def test_schema_shape() -> None:
    s = _bi_concept_resolve_schema()
    assert s.name == "bi_concept_resolve"
    props = s.input_schema["properties"]
    assert set(props) == {"tokens"}, "query_context only fed the removed in-tool LLM guess"
    assert s.input_schema["required"] == ["tokens"]


def test_description_documents_the_new_contract() -> None:
    """出参形状是**纯 prompt 契约**（全仓无后端消费方）⇒ 改了形状必须同时改说明。"""
    d = _bi_concept_resolve_schema().description
    for kw in ("verdict", "matches", "unusable_dimension", "no_permission", "token_in_doc", "suffix"):
        assert kw in d, f"tool description 少了 {kw!r}——模型会按旧契约读新结构"
    for gone in ("guessed", "value_set", "formula", "match.source"):
        assert gone not in d, f"{gone!r} described a removed output; the model would wait for it"
    # `dim` is an internal ID; explicitly tell the model not to pass it to bi_query.
    assert "不吃 id" in d


def test_register_function_imports_and_callable() -> None:
    assert callable(register_bi_concept_resolve_tool)


def test_wire_in_compute_peer_tool_whitelist() -> None:
    from bowhead_core.subagent import get_compute_profile

    assert "bi_concept_resolve" in get_compute_profile().tool_whitelist


def test_wire_in_lead_manifest() -> None:
    from pathlib import Path

    import yaml

    manifest = Path(__file__).parent.parent / "src" / "bowhead_core" / "agents" / "manifests" / "system" / "lead.yaml"
    assert "bi_concept_resolve" in yaml.safe_load(manifest.read_text())["tools"]["allow"]


def test_wire_in_role_bindings_codegen() -> None:
    from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP

    roles = _PARADIGM_V2_ROLE_MAP["bi_concept_resolve"]
    assert "analyst" in roles and "lead" in roles


def test_tool_names_constant_exported() -> None:
    from bowhead_core.agents._generated.tool_names import BI_CONCEPT_RESOLVE

    assert BI_CONCEPT_RESOLVE == "bi_concept_resolve"


# ── Handler composition ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_handler_returns_empty_for_empty_tokens() -> None:
    assert await _bi_concept_resolve_handler({"tokens": []}, _ctx()) == {"results": []}


@pytest.mark.anyio
async def test_handler_shape_and_length_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """I3：一个 token 一条结果，顺序对齐。"""

    async def fake_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        return [_rec(t, "unique", [_m("d199", t)]) for t in tokens]

    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fake_recall)
    out = await _bi_concept_resolve_handler({"tokens": ["爱必妥", "格华止"]}, _ctx())
    assert [r["token"] for r in out["results"]] == ["爱必妥", "格华止"]
    assert all(r["next_action_hint"] in _HINTS for r in out["results"])


@pytest.mark.anyio
async def test_recall_timeout_reports_unavailable_and_skips_the_suffix_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """datarc 召回接口服务端串行、请求之间排队（实测 4 路并发 p50 13s）：召回段也受 timebox 管。

    超时的词必须报「不可用」而不是 not_found（那等于说字典里没有），且不进后缀核对——
    核对会再打同一个排队中的接口。
    """
    checked: list[str] = []

    async def slow_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        await asyncio.sleep(5)
        return []

    async def record_check(token: str, ctx: Any) -> None:
        checked.append(token)

    monkeypatch.setenv("BOWHEAD_CONCEPT_RESOLVE_TIMEOUT_S", "0.05")
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", slow_recall)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve._suffix_split", record_check)
    out = await _bi_concept_resolve_handler({"tokens": ["华东", "爱必妥"]}, _ctx())

    assert [r["token"] for r in out["results"]] == ["华东", "爱必妥"]
    for r in out["results"]:
        assert str(r["error"]).startswith(f"{EMBED_UNAVAILABLE_PREFIX}:recall_timeout")
        assert r["matches"] == []
        assert r["next_action_hint"] == "skip_token"
    assert checked == [], "召回超时后不该再进后缀核对"


@pytest.mark.anyio
async def test_recall_within_timebox_is_not_reported_as_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：同样的短 timebox 下召回按时返回 ⇒ 不许误报超时。"""

    async def fast_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        return [_rec(t, "unique", [_m("d199", t)]) for t in tokens]

    monkeypatch.setenv("BOWHEAD_CONCEPT_RESOLVE_TIMEOUT_S", "1")
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fast_recall)
    out = await _bi_concept_resolve_handler({"tokens": ["华东", "爱必妥"]}, _ctx())
    assert [r["error"] for r in out["results"]] == [None, None]
    assert [r["next_action_hint"] for r in out["results"]] == ["use_top_candidate", "use_top_candidate"]


def test_compose_unique_uses_top_candidate() -> None:
    r = _compose_result("格华止", _rec("格华止", "unique", [_m("d199", "格华止", name="中文品牌")]), None, 40.0)
    assert r["verdict"] == "unique"
    assert r["next_action_hint"] == "use_top_candidate"
    # Named field, no Datarc id: no tool accepts `d199`.
    assert r["matches"][0]["dim_name"] == "中文品牌" and "dim" not in r["matches"][0]


def test_compose_ambiguous_emits_clarification_payload() -> None:
    """I5：payload shape 是 load-bearing —— peer 原样 forward 进 subagent_return。"""
    rec = _rec(
        "爱必妥",
        "ambiguous",
        [_m("d199", "爱必妥", name="中文品牌"), _m("d186", "西妥昔单抗", name="中文通用名")],
    )
    r = _compose_result("爱必妥", rec, None, 40.0)
    assert r["next_action_hint"] == "call_clarification_ask"
    q = r["clarification_payload"]["questions"][0]  # type: ignore[index]
    assert set(q) == {"question", "options", "type"}
    assert q["type"] == "single_select"
    assert 2 <= len(q["options"]) <= 4, "B5 schema 限 2-4 个选项"
    # Options must include dimension names; users cannot choose from values alone.
    assert any("中文品牌" in o for o in q["options"])
    assert any("中文通用名" in o for o in q["options"])


def test_compose_unusable_dimension_does_not_look_like_not_found() -> None:
    """「有字段但没数据」≠「没找到」——上游要据此给 domain_mismatch。"""
    rec = _rec("创建时间", "unusable_dimension", [], unusable=[{"dim": "d66", "dim_name": "创建时间", "reason": "declared_but_no_data"}])
    r = _compose_result("创建时间", rec, None, 40.0)
    assert r["verdict"] == "unusable_dimension"
    assert r["next_action_hint"] == "skip_token"
    assert r["unusable"][0] == {"dim_name": "创建时间", "reason": "declared_but_no_data"}


@pytest.mark.anyio
async def test_partial_failure_degrades_only_the_failed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-CTO-2：一个 token 的后缀核对炸了，不许连累同批其它 token。"""

    async def fake_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        return [
            _rec("好词", "unique", [_m("d199", "好词")]),
            _rec("坏词", "not_found"),
        ]

    async def boom(token: str, ctx: Any) -> Any:
        raise RuntimeError("suffix check exploded")

    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fake_recall)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve._suffix_split", boom)
    out = await _bi_concept_resolve_handler({"tokens": ["好词", "坏词"]}, _ctx())
    good, bad = out["results"]
    assert good["next_action_hint"] == "use_top_candidate" and good["error"] is None
    assert bad["next_action_hint"] == "skip_token" and "suffix check exploded" in (bad["error"] or "")


@pytest.mark.anyio
async def test_dictionary_hit_skips_the_suffix_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """词表能答的词不再做后缀核对（多打一次排队中的召回接口）。"""
    called: list[str] = []

    async def fake_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        return [_rec(t, "unique", [_m("d199", t)]) for t in tokens]

    async def spy(token: str, ctx: Any) -> Any:
        called.append(token)
        return None

    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fake_recall)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve._suffix_split", spy)
    await _bi_concept_resolve_handler({"tokens": ["爱必妥", "格华止"]}, _ctx())
    assert called == [], f"词表已定案却仍做了后缀核对: {called}"


@pytest.mark.anyio
async def test_unresolved_concept_is_returned_to_the_agent_without_a_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 词表答不了的概念（「华东」「客单价」）原样交还调用方 Agent，工具内部不调模型。

    2026-10-17 用户拍板：词表查不到时由对应的 Agent 来猜，不是工具来猜。
    """
    invoked: list[Any] = []

    class _Router:
        async def invoke(self, *args: Any, **kwargs: Any) -> Any:
            invoked.append(kwargs)
            raise AssertionError("bi_concept_resolve must not call the model")

    async def fake_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        return [_rec("华东", "not_found"), _rec("客单价", "weak", [_m("m56", "客单", mtype="metric", ev="near")])]

    async def no_suffix(token: str, ctx: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fake_recall)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.detect_dim_suffix", no_suffix)
    ctx = SimpleNamespace(_running_profile="compute", router=_Router())
    out = await _bi_concept_resolve_handler({"tokens": ["华东", "客单价"], "query_context": "华东客单价"}, ctx)

    assert invoked == []
    east, aov = out["results"]
    assert (east["verdict"], east["matches"]) == ("not_found", [])
    assert aov["verdict"] == "weak" and [m["value"] for m in aov["matches"]] == ["客单"]
    assert all(r["next_action_hint"] == "skip_token" and r["error"] is None for r in out["results"])


def test_weak_near_only_evidence_must_not_say_use_it() -> None:
    """★ 回归守卫：只有语义近邻撑着时，**不许**给 `use_top_candidate`。

    embedding 最近邻会把「广州」判到「广渠门」。说"直接用"就是把「返回空」
    升级成「自信地返回错维度」，比返回空更糟——老实现在 EXACT 分支专门留过
    这道守卫，重写第一版把它丢了，自 review 抓回来的。
    """
    m = Match(type="dim_value", dim="d1", dim_name="区域", value="广渠门", evidence="near", dist=0.2)  # type: ignore[arg-type]
    r = _compose_result("广州", _rec("广州", "weak", [m]), None, 40.0)
    assert r["next_action_hint"] != "use_top_candidate", "near-only 证据不许说「直接用」"
    # Still return candidates; insufficiently strong evidence is no reason to discard retrieved evidence.
    assert len(r["matches"]) == 1


@pytest.mark.anyio
async def test_blank_and_duplicate_tokens_are_not_checked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """空白 token 与重复 token 不该各做一次后缀核对。"""
    called: list[str] = []

    async def fake_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        return [_rec("爱必妥", "unique", [_m("d199", "爱必妥")])]

    async def spy(token: str, ctx: Any) -> Any:
        called.append(token)
        return None

    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fake_recall)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve._suffix_split", spy)
    await _bi_concept_resolve_handler({"tokens": ["   ", "爱必妥", "爱必妥"]}, _ctx())
    assert called == [], f"空白/重复 token 白做了后缀核对: {called}"


# ── Verified suffix split (`万达店` → 门店包含万达) ─────────────────────────────


@pytest.mark.parametrize(
    ("dim_name", "value", "why"),
    [
        ("", "万达", "没有维度名 → 裸值串，模型不知道按哪个字段筛"),
        ("客户", "张三、李四公司", "值自身含分隔符 → cut 会把一个值切成两个"),
    ],
)
def test_rewrite_is_withheld_rather_than_wrong(dim_name: str, value: str, why: str) -> None:
    """给一个「粘贴即用但其实错」的串，比不给更糟。"""
    from bowhead_core.agents.bi_concept_resolve import _contains_rewrite

    assert _contains_rewrite("门店", "万达") == "门店包含万达"
    assert _contains_rewrite(dim_name, value) == "", why


@pytest.mark.anyio
async def test_verified_suffix_split_is_a_unique_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """`万达店` 被确定性地剥成「门店包含万达」（零 LLM，前缀已在该维度下核对过）。

    这个能力仓里一直有（`detect_dim_suffix`），但唯一 import 方是已断线的
    `bi_query_preflight` ⇒ 实际零使用，客户在自己的 skill 里重写了一份。
    """

    async def fake_suffix(token: str, ctx: Any, **kw: Any) -> Any:
        return SimpleNamespace(dim_id="d54", dim_word="门店", value_part="万达")

    async def fake_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        return [_rec("万达店", "not_found")]

    async def verified(suffix: Any, ctx: Any) -> bool:
        return True

    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fake_recall)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.detect_dim_suffix", fake_suffix)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve._suffix_prefix_exists", verified)

    out = await _bi_concept_resolve_handler({"tokens": ["万达店"]}, _ctx())
    r0 = out["results"][0]
    m = r0["matches"][0]
    assert m["evidence"] == "suffix" and m["op"] == "contains"
    assert m["rewrite"] == "门店包含万达"
    # A verified suffix is a deterministic conclusion; do not label it as weak evidence to abandon.
    assert r0["verdict"] == "unique"
    assert r0["next_action_hint"] == "use_top_candidate"


@pytest.mark.anyio
async def test_unverified_suffix_split_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 116 真机抓到的：`万达店` 拆出「门店包含万达」，而全部 40 家门店没一家含万达。

    `detect_dim_suffix` 只验后缀对得上维度名，不验前缀在该维度里有值。
    验不过就当没检测到，别给一个粘贴即用的错改写。
    """

    async def fake_suffix(token: str, ctx: Any, **kw: Any) -> Any:
        return SimpleNamespace(dim_id="d57", dim_word="门店", value_part="万达")

    async def not_verified(suffix: Any, ctx: Any) -> bool:
        return False

    async def fake_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        return [_rec("万达店", "not_found")]

    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fake_recall)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.detect_dim_suffix", fake_suffix)
    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve._suffix_prefix_exists", not_verified)

    out = await _bi_concept_resolve_handler({"tokens": ["万达店"]}, _ctx())
    r0 = out["results"][0]
    assert r0["matches"] == [], "未验证的拆分不该出现"
    assert r0["verdict"] == "not_found"


@pytest.mark.anyio
async def test_dictionary_is_always_consulted_even_with_stale_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 本 sprint 的核心回归：**任何入参都不能让词表被跳过**。

    `kind_hint` 已从 schema 删除，但本仓工具入参链路没有 schema 校验，
    残留/幻觉出来的参数仍可能被传进来 —— 它绝不能再影响查不查词表。

    生产实测：该分支下 `dict_hit` 0/28（结构不可达），而 `auto` 分支 11/26。
    """
    seen: list[list[str]] = []

    async def fake_recall(ctx: Any, tokens: list[str], *, cap: int) -> list[TokenRecall]:
        seen.append(list(tokens))
        return [_rec(t, "unique", [_m("d199", t)]) for t in tokens]

    monkeypatch.setattr("bowhead_core.agents.bi_concept_resolve.recall_tokens", fake_recall)
    out = await _bi_concept_resolve_handler({"tokens": ["爱必妥"], "kind_hint": "abstract", "query_context": "旧提示词仍会传"}, _ctx())
    assert seen == [["爱必妥"]], "有残留入参时词表被跳过了"
    assert out["results"][0]["matches"][0]["value"] == "爱必妥"


def test_verified_suffix_split_leads_weak_neighbours() -> None:
    """A verified split turns the verdict into `unique`; `use_top_candidate` must then point at it,
    not at a weak semantic neighbour recall happened to return ("广州" → "广渠门")."""
    from types import SimpleNamespace as _NS

    rec = _rec("万达店", "weak", [_m("d1", "万达里", ev="near", name="小区")])
    r = _compose_result("万达店", rec, _NS(dim_id="d57", dim_word="门店", value_part="万达"), 40.0)
    assert r["verdict"] == "unique" and r["next_action_hint"] == "use_top_candidate"
    assert r["matches"][0]["evidence"] == "suffix"
    assert r["matches"][0]["rewrite"] == "门店包含万达"
