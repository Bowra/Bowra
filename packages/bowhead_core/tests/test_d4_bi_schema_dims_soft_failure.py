"""D4 — bi_schema retries + warns on asymmetric-empty dimensions.

datarc /dimensions/ can return HTTP-200 with empty `data` on a transient hiccup; the
client coalesces None/[] → [] (datarc_client.py:362). measures-OK + dims-empty is a
partial-failure fingerprint (a workspace with measures-on-data but zero declared
dimensions is implausible). `_schema` retries the dims fetch before accepting empty, and
if still empty surfaces a `_schema_warnings` soft-warning so the LLM does NOT assert
"本 workspace 不支持按 X 拆分" off a flake. Detection keys on the RAW dims (pre used-filter)
so the legitimate all-used=false → full-fallback path (test_bi_schema_used_filter) is
untouched.
"""

from __future__ import annotations

import pytest
from bowhead_bi_bridge.types import DimensionMeta, MeasureMeta
from bowhead_core.tool_registry import build_default_registry


def _measures() -> list[MeasureMeta]:
    return [MeasureMeta(id=1, names=["销售额", "Sales"])]


def _real_dims() -> list[DimensionMeta]:
    return [DimensionMeta(id=199, names=["品牌"], is_keyword=True, used=True)]


class _SeqDatarc:
    """Fake datarc whose list_dimensions returns dims_seq[i] on the i-th call (clamped)."""

    def __init__(self, dims_seq: list[list[DimensionMeta]], measures: list[MeasureMeta]) -> None:
        self._dims_seq = dims_seq
        self._measures = measures
        self.dims_calls = 0

    async def list_dimensions(self, _ws: str) -> list[DimensionMeta]:
        i = min(self.dims_calls, len(self._dims_seq) - 1)
        self.dims_calls += 1
        return self._dims_seq[i]

    async def list_measures(self, _ws: str) -> list[MeasureMeta]:
        return self._measures


class _Ctx:
    def __init__(self, datarc: _SeqDatarc) -> None:
        self.datarc = datarc
        self.member_uid = "m_test"

    def resolve_datarc_project(self) -> str:
        return "336"


@pytest.fixture
def schema_handler():
    return build_default_registry().get("bi_schema").handler


async def test_dims_empty_then_recovered_on_retry(schema_handler) -> None:
    # measures OK, first dims empty, retry returns real dims → recovered, no warning.
    dc = _SeqDatarc([[], _real_dims()], _measures())
    out = await schema_handler({"kind": "both"}, _Ctx(dc))
    assert dc.dims_calls >= 2, "retry should have fired"
    assert [d["names"][0] for d in out["dimensions"]] == ["品牌"]
    assert "_schema_warnings" not in out


async def test_dims_persistently_empty_warns_not_silent(schema_handler) -> None:
    # measures OK, dims empty on every call → degraded BUT flagged (not a silent []).
    dc = _SeqDatarc([[]], _measures())
    out = await schema_handler({"kind": "both"}, _Ctx(dc))
    assert dc.dims_calls >= 2, "should have retried before giving up"
    assert out["dimensions"] == []
    assert out.get("_schema_warnings"), "asymmetric empty must surface a soft-warning"


async def test_both_empty_is_ambiguous_no_retry_no_warning(schema_handler) -> None:
    # measures ALSO empty → genuinely-empty/new ws is plausible → no retry, no warning.
    dc = _SeqDatarc([[]], [])
    out = await schema_handler({"kind": "both"}, _Ctx(dc))
    assert dc.dims_calls == 1, "no retry when both empty (not an asymmetric signal)"
    assert "_schema_warnings" not in out


async def test_dims_ok_first_try_no_retry_no_warning(schema_handler) -> None:
    dc = _SeqDatarc([_real_dims()], _measures())
    out = await schema_handler({"kind": "both"}, _Ctx(dc))
    assert dc.dims_calls == 1
    assert out["dimensions"] and "_schema_warnings" not in out


async def test_legacy_kind_dimensions_now_follows_the_both_contract(schema_handler) -> None:
    """Legacy kind arguments retain the current both-lists contract.

    Two genuinely empty lists remain ambiguous and follow the existing no-retry
    policy, rather than being interpreted as an intentionally omitted list.
    """
    dc = _SeqDatarc([[]], [])
    out = await schema_handler({"kind": "dimensions"}, _Ctx(dc))
    assert dc.dims_calls == 1, "两边都空 ⇒ 不重试（both 契约）"
    assert "_schema_warnings" not in out, "两边都空时说「但指标正常返回」是撒谎"
