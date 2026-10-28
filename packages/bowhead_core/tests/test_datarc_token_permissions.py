"""Datarc terms are filtered by the asking member's permissions before tools hand them on.

Recall, the schema vector collection and the measure/dimension listings carry no
member context. Each surface checks its terms with one batched Datarc permission
call: denied fields and values disappear, a denied field the user named is reported
as ``no_permission``, and an unavailable check keeps the terms.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_bi_bridge.types import DimensionMeta, KeywordCandidate, KeywordRecall, MeasureMeta
from bowhead_core.concept_recall import recall_tokens
from bowhead_core.embed_search import query as embed_query
from bowhead_core.tool_registry import build_default_registry

pytestmark = pytest.mark.anyio

MODELS = "model-a,model-b"


class _Datarc:
    """Datarc stand-in: ``denied`` holds props this member may not use."""

    _base_url = "https://datarc.test/"

    def __init__(self, denied: set[str] = frozenset(), *, fail: bool = False) -> None:
        self.denied = set(denied)
        self.fail = fail
        self.permission_calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.measures = [
            MeasureMeta(id=1, names=["营业额"], description="", sort=1),
            MeasureMeta(id=2, names=["利润"], description="", sort=2),
        ]
        self.dims = [
            DimensionMeta(id=50, names=["城市"], is_keyword=True, used=True),
            DimensionMeta(id=54, names=["品牌"], is_keyword=True, used=True),
        ]

    async def check_token_permissions(self, ws: str, member: str, tokens: list[dict[str, Any]]) -> list[str]:
        self.permission_calls.append((member, tokens))
        if self.fail:
            raise RuntimeError("datarc down")
        return ["No permission" if t["prop"] in self.denied else "Normal" for t in tokens]

    async def recall_keywords(self, ws: str, words: list[str], n_results: int | None = None) -> list[KeywordRecall]:
        values = {
            "北京": [KeywordCandidate("北京", 0.0, "d50", "d50=5YyX5Lqs", MODELS), KeywordCandidate("北京东", 0.1, "d50", "d50=east", MODELS)],
            "耐克": [KeywordCandidate("耐克", 0.0, "d54", "d54=6ICQ5YWL", MODELS)],
        }
        return [KeywordRecall(source=w, candidates=values.get(w, [])) for w in words]

    async def list_measures(self, ws: str) -> list[MeasureMeta]:
        return self.measures

    async def list_dimensions(self, ws: str) -> list[DimensionMeta]:
        return self.dims

    async def get_workspace(self, ws: str) -> Any:
        return SimpleNamespace(name="Retail", desc="")


class _Vectors:
    datarc_tenant = "demo"

    async def query(self, *, collection: str, query_texts: list[str], n_results: int, include: list[str]) -> dict[str, Any]:
        rows = {
            "利润": [("利润", "m2", 0.0)],
            "北京": [("城市", "d50", 0.0)],
            "耐克": [("品牌", "d54", 0.0)],
            "门店面积": [("门店面积", "d49", 0.0), ("门店", "d57", 0.2), ("面积", "m25", 0.3)],
        }
        hits = [rows.get(q, []) for q in query_texts]
        return {
            "documents": [[h[0] for h in row] for row in hits],
            "metadatas": [[{"prop": h[1]} for h in row] for row in hits],
            "distances": [[h[2] for h in row] for row in hits],
        }


def _ctx(datarc: _Datarc, member: str = "member-1") -> Any:
    return SimpleNamespace(
        datarc=datarc,
        member_uid=member,
        resolve_datarc_project=lambda: "1",
        vector_client=_Vectors(),
        tenant="demo",
        ar2_thresholds=None,
    )


async def test_denied_value_is_withheld_with_one_batched_check() -> None:
    datarc = _Datarc(denied={"d50=5YyX5Lqs"})
    (beijing, nike) = await embed_query(_ctx(datarc), ["北京", "耐克"], "keywords", 10)

    assert [h.value for h in beijing.hits] == ["北京东"]
    assert [h.value for h in beijing.denied] == ["北京"]
    assert [h.value for h in nike.hits] == ["耐克"]
    (member, tokens), *rest = datarc.permission_calls
    assert rest == []
    assert member == "member-1"
    # Recall joins model uids with commas; the permission check needs them as a list.
    assert {"word": "北京", "prop": "d50=5YyX5Lqs", "tuids": ["model-a", "model-b"]} in tokens


async def test_value_of_a_column_denied_dimension_is_withheld() -> None:
    """Datarc checks a value only against row permission; its dimension's column is checked too."""
    datarc = _Datarc(denied={"d54"})
    (nike,) = await embed_query(_ctx(datarc), ["耐克"], "keywords", 10)

    assert nike.hits == []
    assert [h.value for h in nike.denied] == ["耐克"]


async def test_named_field_without_permission_is_reported_not_missing() -> None:
    datarc = _Datarc(denied={"m2"})
    (profit,) = await recall_tokens(_ctx(datarc), ["利润"], cap=5)

    assert profit.verdict == "no_permission"
    assert profit.matches == []
    assert profit.unusable == [{"dim": "m2", "dim_name": "利润", "reason": "no_permission"}]


async def test_named_field_without_permission_is_not_replaced_by_its_parts() -> None:
    """The user named 门店面积 exactly; 门店 and 面积 must not stand in for the denied field."""
    datarc = _Datarc(denied={"d49"})
    (area,) = await recall_tokens(_ctx(datarc), ["门店面积"], cap=5)

    assert area.verdict == "no_permission"
    assert area.matches == []
    assert area.unusable == [{"dim": "d49", "dim_name": "门店面积", "reason": "no_permission"}]


async def test_denied_value_is_not_disclosed_by_name() -> None:
    datarc = _Datarc(denied={"d54=6ICQ5YWL"})
    (nike,) = await recall_tokens(_ctx(datarc), ["耐克"], cap=5)

    assert all(m.value != "耐克" or m.type != "dim_value" for m in nike.matches)
    assert all(u["reason"] != "no_permission" or u["dim"] != "d54" for u in nike.unusable)


async def test_unavailable_check_keeps_terms() -> None:
    datarc = _Datarc(denied={"d50=5YyX5Lqs"}, fail=True)
    (beijing,) = await embed_query(_ctx(datarc), ["北京"], "keywords", 10)

    assert [h.value for h in beijing.hits] == ["北京", "北京东"]


async def test_revoked_permission_applies_on_the_next_call() -> None:
    datarc = _Datarc()
    (before,) = await embed_query(_ctx(datarc), ["利润"], "schema", 5)
    datarc.denied.add("m2")
    (after,) = await embed_query(_ctx(datarc), ["利润"], "schema", 5)

    assert "利润" in [h.doc for h in before.hits]
    assert "利润" not in [h.doc for h in after.hits]


async def test_bi_schema_hides_denied_fields_and_says_why() -> None:
    datarc = _Datarc(denied={"m2", "d54"})
    out = await build_default_registry().get("bi_schema").handler({}, _ctx(datarc))

    assert [m["names"][0] for m in out["measures"]] == ["营业额"]
    assert [d["names"][0] for d in out["dimensions"]] == ["城市"]
    assert (out["measures_no_permission_count"], out["dimensions_no_permission_count"]) == (1, 1)
    assert (out["measures_unused_count"], out["dimensions_unused_count"]) == (0, 0)
    assert "permission" in out["_permission_note"]


async def test_bi_schema_without_denials_has_no_permission_keys() -> None:
    out = await build_default_registry().get("bi_schema").handler({}, _ctx(_Datarc()))

    assert "_permission_note" not in out
    assert len(out["measures"]) == 2
