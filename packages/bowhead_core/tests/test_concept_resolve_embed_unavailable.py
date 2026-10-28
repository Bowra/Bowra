"""「词表后端不可用」必须与「字典里真没有这个概念」可区分.

2026-08-17 两个私有化客户的现象都是「concept resolve 返回 0 个答案」。真因是 collection
名的 tenant 段写死成 "demo"（真实实例是 dev），datarc 回「Collection does not exist」。
之所以拖了很久没人定位到：两种情况返回的 ConceptResult **逐字节相同**——都是
candidates=[] + skip_token + error=None，telemetry 也都记 outcome=no_match。

维度值召回改走 datarc `/keywords/recall/` 后，同一个问题换了形状：接口在旧版 datarc 上
是 404、客户没配向量服务是 errcode=400。这些都必须报成「不可用」且原因可区分，
而不是「没命中」。

所以本文件的每条断言都成对写（后端坏 / 后端好但确实没命中），单看一边说明不了任何事：
「返回 0」永远成立，能不能**分辨**才是要守的东西。
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import patch

import pytest
from bowhead_core.agents.bi_concept_resolve import (
    EMBED_UNAVAILABLE_PREFIX,
    _bi_concept_resolve_handler,
)
from bowhead_core.embed_search import query as embed_query

from bowhead_bi_bridge import (
    BINotFoundError,
    BIPermissionError,
    BIValidationError,
    KeywordCandidate,
    KeywordRecall,
)


class _CapturingVC:
    """记录被查过的 collection；永远返回「查到了但零命中」。"""

    def __init__(self) -> None:
        self.collections: list[str] = []

    async def query(self, *, collection: str, **_kw: Any) -> dict[str, Any]:
        self.collections.append(collection)
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}


class _FakeDatarc:
    """datarc 召回接口替身：按词返回预设候选，或抛预设异常。"""

    def __init__(self, candidates: dict[str, list[KeywordCandidate]] | None = None, *, raises: Exception | None = None) -> None:
        self.candidates = candidates or {}
        self.raises = raises
        self.calls: list[tuple[str, list[str], int | None]] = []

    async def recall_keywords(self, ws: str, words: list[str], *, n_results: int | None = None) -> list[KeywordRecall]:
        self.calls.append((ws, list(words), n_results))
        if self.raises is not None:
            raise self.raises
        return [KeywordRecall(source=w, candidates=list(self.candidates.get(w, []))) for w in words]

    async def list_dimensions(self, ws: str) -> list[Any]:
        return []


def _ctx(vc: Any, *, datarc: Any = None, tenant: str = "", project: str = "1") -> Any:
    return types.SimpleNamespace(
        vector_client=vc,
        datarc=datarc,
        tenant=tenant,
        workspace_id="ws_abc",
        resolve_datarc_project=lambda: project,
        ar2_thresholds=None,
        router=object(),
        _running_profile="compute",
    )


# ─── Schema collection: explicit unavailability reason required ──────────────


async def test_healthy_backend_with_zero_hits_is_not_unavailable() -> None:
    """正对照：后端好、collection 对、就是没命中 → unavailable_reason 必须为空。

    没有这条，下面几条「不可用」断言可能只是恒真。
    """
    vc = _CapturingVC()
    vc.datarc_tenant = "dev"  # type: ignore[attr-defined]
    res = await embed_query(_ctx(vc), query_texts=["门店"], collection="schema", n_results=3)
    assert vc.collections == ["v1_dev_p1"]
    assert res[0].hits == []
    assert res[0].miss is True
    assert res[0].unavailable_reason == ""
    assert res[0].unavailable is False


async def test_missing_vector_client_reports_unavailable() -> None:
    res = await embed_query(_ctx(None), query_texts=["门店"], collection="schema", n_results=3)
    assert res[0].unavailable_reason == "no_vector_client"


async def test_unresolved_tenant_never_guesses_a_collection_name() -> None:
    """★ 判不出 tenant 时**一次查询都不能发**——拼错名字轻则全 miss，重则读到别家客户。"""
    vc = _CapturingVC()  # No datarc_tenant attribute; ctx.tenant is also empty
    res = await embed_query(_ctx(vc), query_texts=["门店"], collection="schema", n_results=3)
    assert vc.collections == []
    assert res[0].unavailable_reason == "tenant_unresolved"


async def test_connection_discovered_tenant_beats_deployment_pin() -> None:
    """连接实测判定 > 部署级 pin：同进程多连接指向不同 datarc 实例时才不会串。"""
    vc = _CapturingVC()
    vc.datarc_tenant = "dev"  # type: ignore[attr-defined]
    await embed_query(_ctx(vc, tenant="demo"), query_texts=["店"], collection="schema", n_results=3)
    assert vc.collections == ["v1_dev_p1"]


async def test_deployment_pin_used_when_discovery_unavailable() -> None:
    """自动判定不了时，admin 显式 pin 仍然生效（私有化单实例的兜底）。"""
    vc = _CapturingVC()
    await embed_query(_ctx(vc, tenant="acme"), query_texts=["店"], collection="schema", n_results=3)
    assert vc.collections == ["v1_acme_p1"]


async def test_backend_error_reports_unavailable_not_miss() -> None:
    class _BoomVC:
        async def query(self, **_kw: Any) -> dict[str, Any]:
            raise RuntimeError("Collection [v1_demo_p1] does not exist")

    vc = _BoomVC()
    vc.datarc_tenant = "demo"  # type: ignore[attr-defined]
    res = await embed_query(_ctx(vc), query_texts=["华东"], collection="schema", n_results=3)
    assert res[0].unavailable_reason == "query_failed"


# ─── Dimension value recall (datarc /keywords/recall/) ────────────────────────


async def test_dim_value_recall_goes_through_datarc_not_the_vector_store() -> None:
    """★ 维度值召回不再依赖 embed 代理 / tenant：没判出 tenant 也照样能答。"""
    vc = _CapturingVC()  # No tenant: old implementation inevitably yielded tenant_unresolved here
    datarc = _FakeDatarc({"上海": [KeywordCandidate(word="上海市", distance=0.08, field="d50", prop="d50=上海市")]})
    res = await embed_query(_ctx(vc, datarc=datarc), query_texts=["上海"], collection="keywords", n_results=3)
    assert vc.collections == []
    assert datarc.calls == [("1", ["上海"], 3)]
    assert res[0].unavailable_reason == ""
    (hit,) = res[0].hits
    assert (hit.dim_id, hit.value, hit.doc, hit.dist) == ("d50", "上海市", "上海市", 0.08)


async def test_healthy_recall_with_zero_hits_is_not_unavailable() -> None:
    """正对照：接口好、就是没召回 → results 为空列表，不能报成不可用。"""
    datarc = _FakeDatarc()
    res = await embed_query(_ctx(None, datarc=datarc), query_texts=["xyzzy"], collection="keywords", n_results=3)
    assert res[0].hits == []
    assert res[0].unavailable_reason == ""


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (
            BIValidationError(
                "当前工作区未配置向量服务，请先配置向量服务后重试。",
                errcode=400,
                errmsg="当前工作区未配置向量服务，请先配置向量服务后重试。",
            ),
            "vector_service_unconfigured",
        ),
        # HTTP 404: this datarc version lacks the endpoint (observed on demo.datarc.cn, 2026-09-15).
        (BINotFoundError("datarc 404: openapi/v1/workspaces/1/keywords/recall/"), "keyword_recall_unsupported"),
        # Business-level nonexistence (with errcode) is not a missing endpoint; do not incorrectly suggest upgrading datarc.
        (BINotFoundError("工作区不存在", errcode=400, errmsg="工作区不存在"), "query_failed"),
        (BIPermissionError("datarc 403: access rejected"), "keyword_recall_forbidden"),
        (RuntimeError("boom"), "query_failed"),
    ],
)
async def test_recall_failures_are_unavailable_with_a_distinguishable_reason(exc: Exception, reason: str) -> None:
    datarc = _FakeDatarc(raises=exc)
    res = await embed_query(_ctx(None, datarc=datarc), query_texts=["华东", "门店"], collection="keywords", n_results=3)
    assert [r.unavailable_reason for r in res] == [reason, reason]


async def test_missing_datarc_client_reports_unavailable() -> None:
    res = await embed_query(_ctx(_CapturingVC(), datarc=None), query_texts=["华东"], collection="keywords", n_results=3)
    assert res[0].unavailable_reason == "no_datarc_client"


async def test_blank_words_are_not_sent_but_keep_their_positions() -> None:
    """datarc 拒收空串；调用方按下标对应结果，所以空白词占位、不发送。"""
    datarc = _FakeDatarc({"华东": [KeywordCandidate(word="华东大区", distance=0.1, field="d162")]})
    res = await embed_query(_ctx(None, datarc=datarc), query_texts=["", "华东", "  "], collection="keywords", n_results=3)
    assert datarc.calls[0][1] == ["华东"]
    assert [r.query for r in res] == ["", "华东", "  "]
    assert [len(r.hits) for r in res] == [0, 1, 0]
    assert all(r.unavailable_reason == "" for r in res)


async def test_duplicate_words_are_sent_once_but_answer_every_position() -> None:
    """服务端逐词串行计算且不去重（实测）；同一个词只发一次，结果按原位置还原。"""
    datarc = _FakeDatarc({"北京": [KeywordCandidate(word="北京", distance=0.0, field="d21")]})
    res = await embed_query(_ctx(None, datarc=datarc), query_texts=["北京", "上海", "北京", ""], collection="keywords", n_results=3)
    assert datarc.calls[0][1] == ["北京", "上海"]
    assert [r.query for r in res] == ["北京", "上海", "北京", ""]
    assert [[h.dim_id for h in r.hits] for r in res] == [["d21"], [], ["d21"], []]


@pytest.mark.parametrize(("asked", "sent"), [(500, 100), (0, 1), (30, 30)])
async def test_n_results_is_clamped_to_the_contract_range(asked: int, sent: int) -> None:
    datarc = _FakeDatarc()
    await embed_query(_ctx(None, datarc=datarc), query_texts=["华东"], collection="keywords", n_results=asked)
    assert datarc.calls[0][2] == sent


async def test_candidates_are_ordered_by_distance() -> None:
    """下游把 hits[0] 当 top-1；接口没承诺排序，乱序返回也不能把远的排在前面。"""
    datarc = _FakeDatarc(
        {
            "爱必妥": [
                KeywordCandidate(word="爱必妥 5", distance=0.3, field="d202"),
                KeywordCandidate(word="爱必妥", distance=0.0, field="d199"),
            ]
        }
    )
    (res,) = await embed_query(_ctx(None, datarc=datarc), query_texts=["爱必妥"], collection="keywords", n_results=3)
    assert [h.doc for h in res.hits] == ["爱必妥", "爱必妥 5"]
    # Flags use sorted top-1: unsorted top-1=0.3 would be classified as miss and non-exact.
    assert res.miss is False
    assert res.exact is True


# ─── Tool layer: unavailable vs genuinely absent must stay distinguishable ───


async def test_embed_down_surfaces_error() -> None:
    """★ 词表后端不可用 → error 带前缀，不伪装成「字典里没有」。"""
    res = await _bi_concept_resolve_handler({"tokens": ["华东"]}, _ctx(None))
    out = res["results"][0]
    assert out["matches"] == []
    assert out["next_action_hint"] == "skip_token"
    assert str(out["error"]).startswith(EMBED_UNAVAILABLE_PREFIX)


async def test_recall_endpoint_missing_is_reported_not_swallowed() -> None:
    """★ 旧版 datarc 没有 `/keywords/recall/`（404）时，工具必须如实报原因。

    schema 侧好好的也不能把它盖掉——否则「datarc 该升级了」会表现成「这个词不存在」。
    """
    vc = _CapturingVC()
    vc.datarc_tenant = "dev"  # type: ignore[attr-defined]
    datarc = _FakeDatarc(raises=BINotFoundError("datarc 404: openapi/v1/workspaces/1/keywords/recall/"))
    res = await _bi_concept_resolve_handler({"tokens": ["华东"]}, _ctx(vc, datarc=datarc))
    out = res["results"][0]
    assert out["error"] == f"{EMBED_UNAVAILABLE_PREFIX}:keyword_recall_unsupported"
    assert out["next_action_hint"] == "skip_token"


async def test_genuine_no_match_leaves_error_empty() -> None:
    """正对照：后端健康但确实没命中 → 同样 0 个候选，但 error 必须为 None。"""
    vc = _CapturingVC()
    vc.datarc_tenant = "dev"  # type: ignore[attr-defined]
    res = await _bi_concept_resolve_handler({"tokens": ["华东"]}, _ctx(vc, datarc=_FakeDatarc()))
    out = res["results"][0]
    assert out["matches"] == []
    assert out["next_action_hint"] == "skip_token"
    assert out.get("error") is None


async def test_handler_telemetry_separates_unavailable_from_no_match() -> None:
    """两种 0 在 metrics 里必须是两个 outcome，否则运维故障永远淹没在正常 no_match 里。"""
    seen: list[dict[str, str]] = []

    def _inc(_name: str, *, labels: dict[str, str], **_kw: Any) -> None:
        seen.append(labels)

    with patch("bowhead_core.metrics.inc", new=_inc):
        out = await _bi_concept_resolve_handler({"tokens": ["华东"]}, _ctx(None))
    assert len(out["results"]) == 1
    assert [x["outcome"] for x in seen] == ["embed_unavailable"]
