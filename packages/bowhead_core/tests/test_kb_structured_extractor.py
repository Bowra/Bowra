"""kb-knowledge-tables sprint Day 2: StructuredExtractor tests.

覆盖:
- list/dict/nested LLM output normalize (per Spike 4)
- citation grounding fuzzy match (PASS / FAIL)
- type coercion (currency 万元 / number / date / string)
- chunk splitting + per-field highest-confidence merge
- N=2 self-consistency vote (agree / disagree)
- 整 e2e mock LLM
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.doc_type_store import ExtractField
from bowhead_core.kb.structured_extractor import (
    StructuredExtractor,
    _chunk_text,
    _coerce_value,
    _ground_citation,
    _list_to_dict_by_schema,
    _merge_chunks,
    _normalize_for_match,
    _normalize_llm_output,
    _self_consistency_merge,
    _vote_value,
)

# ─── normalize_llm_output (Spike 4) ─────────


def test_normalize_list_shape() -> None:
    """LLM 返 list (Spike 2 实测形式)."""
    raw = [
        {"value": 5_000_000, "confidence": 0.9},
        {"value": "2024-01-15", "confidence": 0.95},
    ]
    out = _normalize_llm_output(raw, ["合同金额", "起始日"])
    assert out["合同金额"]["value"] == 5_000_000
    assert out["起始日"]["value"] == "2024-01-15"


def test_normalize_nested_dict_shape() -> None:
    """LLM 返 {fields: [...]}."""
    raw = {
        "fields": [
            {"value": 100, "confidence": 0.8},
            {"value": "X", "confidence": 0.9},
        ]
    }
    out = _normalize_llm_output(raw, ["a", "b"])
    assert out["a"]["value"] == 100
    assert out["b"]["value"] == "X"


def test_normalize_dict_by_name_shape() -> None:
    """LLM 返 dict by name (理想)."""
    raw = {"合同金额": {"value": 1, "confidence": 0.9}}
    out = _normalize_llm_output(raw, ["合同金额"])
    assert out["合同金额"]["value"] == 1


def test_normalize_skips_non_dict_values() -> None:
    raw = {"x": "scalar", "y": {"value": 1}}
    out = _normalize_llm_output(raw, ["x", "y"])
    assert "x" not in out  # Skip scalar.
    assert out["y"]["value"] == 1


def test_normalize_empty() -> None:
    assert _normalize_llm_output(None, ["x"]) == {}
    assert _normalize_llm_output("string", ["x"]) == {}


def test_list_to_dict_by_schema_truncates_extra() -> None:
    """LLM 给的 list 多于 schema → 多余项忽略."""
    raw = [{"value": 1}, {"value": 2}, {"value": 3}]
    out = _list_to_dict_by_schema(raw, ["a", "b"])
    assert set(out.keys()) == {"a", "b"}


def test_list_to_dict_by_schema_short() -> None:
    """LLM 给的 list 少于 schema → 缺的字段不在 result."""
    raw = [{"value": 1}]
    out = _list_to_dict_by_schema(raw, ["a", "b"])
    assert "a" in out
    assert "b" not in out


# ─── citation grounding ─────────


def test_ground_citation_match() -> None:
    """snippet 是 source 子串 → citation_verified=True."""
    src_norm = _normalize_for_match(
        "本协议合同总金额为人民币伍佰万元 (RMB 5,000,000), 含税.",
    )
    fdata = {
        "value": 5_000_000,
        "source_snippet": "合同总金额为人民币伍佰万元",
        "confidence": 0.9,
    }
    out = _ground_citation(fdata, src_norm)
    assert out["citation_verified"] is True
    assert out["confidence"] == 0.9  # not lowered


def test_ground_citation_whitespace_insensitive() -> None:
    """source 含空格 / 换行, snippet 紧凑 → 仍 match."""
    src_norm = _normalize_for_match("合同 总  金额 为 五百万")
    fdata = {"value": 5_000_000, "source_snippet": "合同总金额为五百万", "confidence": 0.9}
    out = _ground_citation(fdata, src_norm)
    assert out["citation_verified"] is True


def test_ground_citation_no_match() -> None:
    """LLM 编造 snippet (不在 source) → citation_verified=False, confidence-0.2."""
    src_norm = _normalize_for_match("合同金额为五百万元.")
    fdata = {
        "value": 5_000_000,
        "source_snippet": "fake editorial note: 不在文档里",
        "confidence": 0.9,
    }
    out = _ground_citation(fdata, src_norm)
    assert out["citation_verified"] is False
    assert out["source_snippet"] == ""
    assert out["source_snippet_unverified"] == "fake editorial note: 不在文档里"
    assert abs(out["confidence"] - 0.7) < 1e-6


def test_ground_citation_empty_snippet() -> None:
    """No snippet → no-op."""
    src_norm = "abc"
    fdata = {"value": 1, "source_snippet": "", "confidence": 0.9}
    out = _ground_citation(fdata, src_norm)
    assert "citation_verified" not in out


# ─── type coercion ─────────


def test_coerce_currency_simple() -> None:
    assert _coerce_value("5000000", "currency") == 5_000_000
    assert _coerce_value("RMB 5,000,000", "currency") == 5_000_000
    assert _coerce_value("¥1,234.56", "currency") == 1234.56


def test_coerce_currency_万元() -> None:
    """万元 → × 10000."""
    assert _coerce_value("500万元", "currency") == 5_000_000
    assert _coerce_value("500万", "currency") == 5_000_000
    assert _coerce_value("100万", "currency") == 1_000_000


def test_coerce_currency_already_number() -> None:
    assert _coerce_value(5_000_000, "currency") == 5_000_000
    assert _coerce_value(123.45, "currency") == 123.45


def test_coerce_number() -> None:
    assert _coerce_value("123", "number") == 123
    assert _coerce_value("1,234", "number") == 1234


def test_coerce_date_passthrough() -> None:
    assert _coerce_value("2024-01-15", "date") == "2024-01-15"
    assert _coerce_value(None, "date") is None


def test_coerce_string() -> None:
    assert _coerce_value(123, "string") == "123"
    assert _coerce_value(None, "string") is None


def test_coerce_currency_unparseable() -> None:
    """无法提数字 → 留原值 (不 crash)."""
    assert _coerce_value("abc", "currency") == "abc"


# ─── chunk_text ─────────


def test_chunk_text_short_no_split() -> None:
    text = "短文档"
    chunks = _chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_text_long_splits() -> None:
    """长文本按段落切."""
    paras = ["段落 " + str(i) + " " + "x" * 1000 for i in range(50)]
    text = "\n\n".join(paras)
    chunks = _chunk_text(text, max_chars=10000)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 10000 + 1000  # Paragraph-boundary tolerance.


def test_chunk_text_super_long_paragraph_hard_split() -> None:
    """单段超 max_chars → 硬切."""
    text = "x" * 60_000
    chunks = _chunk_text(text, max_chars=20_000)
    assert len(chunks) == 3
    assert all(len(c) <= 20_000 for c in chunks)


# ─── merge_chunks ─────────


def test_merge_chunks_picks_highest_confidence() -> None:
    """长 PDF chunk 1 找不到, chunk 2 找到 → 取 chunk 2 那条."""
    chunk_results = [
        {"合同金额": {"value": None, "confidence": 0}},
        {"合同金额": {"value": 5_000_000, "confidence": 0.9}},
    ]
    merged = _merge_chunks(chunk_results)
    assert merged["合同金额"]["value"] == 5_000_000


def test_merge_chunks_keeps_first_when_tie() -> None:
    chunk_results = [
        {"合同金额": {"value": 1, "confidence": 0.5}},
        {"合同金额": {"value": 2, "confidence": 0.4}},
    ]
    merged = _merge_chunks(chunk_results)
    assert merged["合同金额"]["value"] == 1


def test_merge_chunks_disjoint_fields() -> None:
    chunk_results = [
        {"a": {"value": 1, "confidence": 0.9}},
        {"b": {"value": 2, "confidence": 0.9}},
    ]
    merged = _merge_chunks(chunk_results)
    assert merged["a"]["value"] == 1
    assert merged["b"]["value"] == 2


# ─── self-consistency vote ─────────


def test_vote_value_clear_majority() -> None:
    val, agree = _vote_value([5_000_000, 5_000_000, 4_500_000])
    assert val == 5_000_000
    assert abs(agree - 2 / 3) < 1e-6


def test_vote_value_all_agree() -> None:
    val, agree = _vote_value([100, 100])
    assert val == 100
    assert agree == 1.0


def test_vote_value_all_null() -> None:
    val, agree = _vote_value([None, None])
    assert val is None
    assert agree == 0.0


def test_vote_value_skips_null() -> None:
    """None 不参投, 但 agree rate 以 total samples 为分母 (null 视为弃权 = 不 agree)."""
    val, agree = _vote_value([None, 5, 5])
    assert val == 5
    # 2/3 (2 vote 5, 1 abstains; abstention never agrees, conservative signal).
    assert abs(agree - 2 / 3) < 1e-6


def test_self_consistency_merge_agree() -> None:
    samples = [
        {"金额": {"value": 5_000_000, "confidence": 0.9}, "对方": {"value": "客户A", "confidence": 0.9}},
        {"金额": {"value": 5_000_000, "confidence": 0.85}, "对方": {"value": "客户B", "confidence": 0.6}},
    ]
    out = _self_consistency_merge(samples, {"金额"})
    assert out["金额"]["value"] == 5_000_000
    assert out["金额"]["self_consistency_agree"] == 1.0
    assert "needs_review" not in out["金额"]
    # Non-cross_verify fields retain sample[0].
    assert out["对方"]["value"] == "客户A"


def test_self_consistency_merge_disagree_marks_review() -> None:
    samples = [
        {"金额": {"value": 5_000_000, "confidence": 0.9}},
        {"金额": {"value": 4_500_000, "confidence": 0.9}},
    ]
    out = _self_consistency_merge(samples, {"金额"})
    # Tie → first sample (vote_value: equal counts →first non_null).
    assert out["金额"]["self_consistency_agree"] == 0.5
    assert out["金额"]["needs_review"] is True
    assert out["金额"]["confidence"] < 0.9


def test_self_consistency_no_cross_verify_fields() -> None:
    """没 cross_verify 字段 → 直接 sample[0] 不 vote."""
    samples = [{"a": {"value": 1}}]
    out = _self_consistency_merge(samples, set())
    assert out == samples[0]


# ─── full extract pipeline (mock router) ─────────


SAMPLE_CONTRACT = """
软件销售合同

第一条 合同金额
本协议合同总金额为人民币伍佰万元 (RMB 5,000,000), 含税.

第二条 合同期限
本合同自2024年1月15日起生效, 至2026年12月31日止.
"""


def _mock_router_with(content: str) -> object:
    resp = MagicMock(content=content)
    r = MagicMock()
    r.invoke = AsyncMock(return_value=resp)
    return r


@pytest.mark.asyncio
async def test_extract_dict_shape_full() -> None:
    """End-to-end mock: dict-by-name shape, citation match, type coerce."""
    llm_out = json.dumps(
        {
            "合同金额": {
                "value": "5000000",
                "source_snippet": "合同总金额为人民币伍佰万元",
                "source_page": 1,
                "confidence": 0.95,
            },
            "起始日": {
                "value": "2024-01-15",
                "source_snippet": "本合同自2024年1月15日起生效",
                "source_page": 1,
                "confidence": 0.95,
            },
        },
        ensure_ascii=False,
    )
    router = _mock_router_with(llm_out)
    fields = [
        ExtractField(name="合同金额", type="currency"),
        ExtractField(name="起始日", type="date"),
    ]
    extractor = StructuredExtractor(router=router)
    result = await extractor.extract(
        text=SAMPLE_CONTRACT,
        fields=fields,
        cross_verify_enabled=False,  # disable for this test
    )
    assert result.chunks_processed == 1
    assert result.self_consistency_runs == 0
    # currency coerced to number
    assert result.fields["合同金额"]["value"] == 5_000_000
    assert result.fields["合同金额"]["citation_verified"] is True
    assert result.fields["起始日"]["value"] == "2024-01-15"
    assert result.fields["起始日"]["citation_verified"] is True
    assert result.has_low_confidence_fields is False


@pytest.mark.asyncio
async def test_extract_list_shape_normalized() -> None:
    """LLM 返 list (Spike 2 实测) 走 _list_to_dict_by_schema."""
    llm_out = json.dumps(
        [
            {
                "value": "500万元",
                "source_snippet": "合同总金额为人民币伍佰万元",
                "confidence": 0.9,
            },
        ],
        ensure_ascii=False,
    )
    router = _mock_router_with(llm_out)
    fields = [ExtractField(name="合同金额", type="currency")]
    extractor = StructuredExtractor(router=router)
    result = await extractor.extract(
        text=SAMPLE_CONTRACT,
        fields=fields,
        cross_verify_enabled=False,
    )
    # 万元 →×10000 =5_000_000.
    assert result.fields["合同金额"]["value"] == 5_000_000


@pytest.mark.asyncio
async def test_extract_self_consistency_runs_n_2() -> None:
    """cross_verify_enabled=True, ≥1 字段 cross_verify=True → N=2 sampling."""
    llm_out = json.dumps(
        {
            "金额": {
                "value": 5_000_000,
                "source_snippet": "合同总金额为人民币伍佰万元",
                "confidence": 0.9,
            },
        },
        ensure_ascii=False,
    )
    router = _mock_router_with(llm_out)
    fields = [ExtractField(name="金额", type="currency", cross_verify=True)]
    extractor = StructuredExtractor(router=router)
    result = await extractor.extract(
        text=SAMPLE_CONTRACT,
        fields=fields,
        cross_verify_enabled=True,
    )
    # router.invoke called 2x (N=2)
    assert router.invoke.await_count == 2
    assert result.self_consistency_runs == 1
    assert result.fields["金额"]["self_consistency_agree"] == 1.0


@pytest.mark.asyncio
async def test_extract_no_cross_verify_single_call() -> None:
    """所有字段 cross_verify=False → N=1, router 调 1 次."""
    llm_out = json.dumps(
        {
            "金额": {"value": 1, "confidence": 0.9, "source_snippet": ""},
        },
        ensure_ascii=False,
    )
    router = _mock_router_with(llm_out)
    fields = [ExtractField(name="金额", type="currency", cross_verify=False)]
    extractor = StructuredExtractor(router=router)
    await extractor.extract(
        text="x",
        fields=fields,
        cross_verify_enabled=True,
    )
    assert router.invoke.await_count == 1


@pytest.mark.asyncio
async def test_extract_empty_fields_short_circuit() -> None:
    """fields=[] → 不调 LLM, 返空."""
    router = _mock_router_with("{}")
    extractor = StructuredExtractor(router=router)
    result = await extractor.extract(text="x", fields=[])
    assert result.fields == {}
    assert result.chunks_processed == 0
    assert router.invoke.await_count == 0


@pytest.mark.asyncio
async def test_extract_handles_invalid_json() -> None:
    """LLM 返非 JSON → 不 crash, fields 空."""
    router = _mock_router_with("this is not JSON at all")
    extractor = StructuredExtractor(router=router)
    fields = [ExtractField(name="X", type="string")]
    result = await extractor.extract(
        text="x",
        fields=fields,
        cross_verify_enabled=False,
    )
    assert result.fields == {}


@pytest.mark.asyncio
async def test_extract_low_confidence_flagged() -> None:
    """≥1 field confidence < 0.5 → has_low_confidence_fields=True."""
    llm_out = json.dumps(
        {
            "X": {"value": "x", "confidence": 0.3, "source_snippet": ""},
        },
        ensure_ascii=False,
    )
    router = _mock_router_with(llm_out)
    extractor = StructuredExtractor(router=router)
    fields = [ExtractField(name="X", type="string")]
    result = await extractor.extract(
        text="x",
        fields=fields,
        cross_verify_enabled=False,
    )
    assert result.has_low_confidence_fields is True


@pytest.mark.asyncio
async def test_extract_llm_error_returns_empty() -> None:
    """Router.invoke raises → fields 空, 不 crash."""
    router = MagicMock()
    router.invoke = AsyncMock(side_effect=RuntimeError("boom"))
    extractor = StructuredExtractor(router=router)
    fields = [ExtractField(name="X", type="string")]
    result = await extractor.extract(
        text="x",
        fields=fields,
        cross_verify_enabled=False,
    )
    assert result.fields == {}


@pytest.mark.asyncio
async def test_extract_long_text_chunks() -> None:
    """文本 > MAX_CHUNK_CHARS → 多 chunk, 每 chunk 调 LLM."""
    long_text = "段落\n\n" + ("x" * 30_000)
    llm_out = json.dumps(
        {
            "X": {"value": "x", "confidence": 0.9, "source_snippet": ""},
        },
        ensure_ascii=False,
    )
    router = _mock_router_with(llm_out)
    extractor = StructuredExtractor(router=router)
    fields = [ExtractField(name="X", type="string", cross_verify=False)]
    result = await extractor.extract(
        text=long_text,
        fields=fields,
        cross_verify_enabled=False,
    )
    assert result.chunks_processed >= 2
    assert router.invoke.await_count == result.chunks_processed
