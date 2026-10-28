"""Tests for sprint δ-#2 — provenance.py BNF parser + γ migration."""

from __future__ import annotations

from bowhead_core.provenance import (
    ProvenanceTag,
    Source,
    decode_ref,
    encode_ref,
    extract_fact_keys,
    has_any_source,
    is_data_claim_valid,
    parse_tag,
    render_tag,
)

# ─── encode/decode ───────────────────────────────────────


def test_encode_basic_no_special_chars() -> None:
    assert encode_ref("art_001") == "art_001"


def test_encode_colon() -> None:
    assert encode_ref("references/foo:bar.md") == "references/foo%3Abar.md"


def test_encode_pipe_and_bracket() -> None:
    assert encode_ref("a|b]c") == "a%7Cb%5Dc"


def test_encode_percent_first() -> None:
    """% must encode first to avoid double-encoding."""
    assert encode_ref("%3A") == "%253A"


def test_decode_roundtrip() -> None:
    raw = "references/foo:bar.md|piece]"
    assert decode_ref(encode_ref(raw)) == raw


# ─── parse_tag ───────────────────────────────────────────


def test_parse_simple_bi_tag() -> None:
    tags = parse_tag("revenue [bi:art_001]")
    assert len(tags) == 1
    assert tags[0].sources == [Source(kind="bi", ref="art_001")]


def test_parse_multiple_tags() -> None:
    tags = parse_tag("[bi:art_001] foo [fact:cogs]")
    assert len(tags) == 2
    assert tags[0].sources[0].kind == "bi"
    assert tags[1].sources[0].kind == "fact"


def test_parse_rejects_raw_colon_in_ref() -> None:
    """Raw `:` inside ref is not allowed by BNF — must be percent-encoded.

    `[llm-rsn:bi:art_001|fact:cogs]` looks chain-like but the second `:`
    breaks the regex. Parser MUST reject (no partial parse).
    """
    tags = parse_tag("[llm-rsn:bi:art_001|fact:cogs]")
    assert tags == []


def test_parse_chain_proper_format() -> None:
    """Chain via | separator, each spec is kind:ref."""
    text = "[bi:art_001|fact:cogs]"
    tags = parse_tag(text)
    assert len(tags) == 1
    assert len(tags[0].sources) == 2
    assert tags[0].sources[0].kind == "bi"
    assert tags[0].sources[1].kind == "fact"


def test_parse_decodes_ref() -> None:
    text = "[kb:references/foo%3Abar.md]"
    tags = parse_tag(text)
    assert tags[0].sources[0].ref == "references/foo:bar.md"


def test_parse_skips_malformed() -> None:
    """Unknown kind silently skipped."""
    tags = parse_tag("[invalid:foo]")
    assert tags == []


def test_parse_empty_ref_for_llm_std() -> None:
    """[llm-std:] (encyclopedic standalone, no ref) is valid."""
    tags = parse_tag("EBITDA = 营收-COGS [llm-std:]")
    assert len(tags) == 1
    assert tags[0].sources[0].kind == "llm-std"
    assert tags[0].sources[0].ref == ""


# ─── render_tag ──────────────────────────────────────────


def test_render_simple() -> None:
    tag = ProvenanceTag(sources=[Source(kind="bi", ref="art_001")])
    assert render_tag(tag) == "[bi:art_001]"


def test_render_chain() -> None:
    tag = ProvenanceTag(
        sources=[
            Source(kind="bi", ref="art_001"),
            Source(kind="fact", ref="cogs"),
        ]
    )
    assert render_tag(tag) == "[bi:art_001|fact:cogs]"


def test_render_encodes_special() -> None:
    tag = ProvenanceTag(sources=[Source(kind="kb", ref="path/with:colons")])
    assert render_tag(tag) == "[kb:path/with%3Acolons]"


def test_parse_render_roundtrip() -> None:
    original = "[bi:art_001|fact:cogs_def]"
    parsed = parse_tag(original)
    re_rendered = render_tag(parsed[0])
    assert re_rendered == original


# ─── is_data_claim_valid ─────────────────────────────────


def test_data_claim_with_bi_valid() -> None:
    tag = ProvenanceTag(sources=[Source(kind="bi", ref="art_001")])
    assert is_data_claim_valid(tag) is True


def test_data_claim_with_fact_valid() -> None:
    tag = ProvenanceTag(sources=[Source(kind="fact", ref="cogs")])
    assert is_data_claim_valid(tag) is True


def test_data_claim_with_user_valid() -> None:
    tag = ProvenanceTag(sources=[Source(kind="user", ref="t_001")])
    assert is_data_claim_valid(tag) is True


def test_data_claim_llm_std_alone_invalid() -> None:
    """LLM standard knowledge alone is NOT valid for data claims (G2.5 anti-hallu)."""
    tag = ProvenanceTag(sources=[Source(kind="llm-std", ref="")])
    assert is_data_claim_valid(tag) is False


def test_data_claim_llm_rsn_chain_with_bi_valid() -> None:
    tag = ProvenanceTag(
        sources=[
            Source(kind="llm-rsn", ref=""),
            Source(kind="bi", ref="art_001"),
        ]
    )
    assert is_data_claim_valid(tag) is True


# citation-audit M3-5: four γ→δ legacy migration guards (`migrate_legacy_num_tag`) were removed
# with that function: no consumers, and no γ-era `[num:…:src:S]` remained after M1 stopped writes and M2 cleaned data.


# ─── helpers ─────────────────────────────────────────────


def test_has_any_source_bnf() -> None:
    assert has_any_source("foo [bi:art_001] bar") is True


def test_has_any_source_ignores_retired_syntax() -> None:
    """M3-5：`has_any_source` 只认 δ BNF——退役的 `[num:…:src:S]` 不再算「有出处」
    （原断言 True）。库内已无该语法；万一混进来也不该给它 provenance 待遇。"""
    assert has_any_source("foo [num:1:int:src:s1] bar") is False


def test_has_any_source_none() -> None:
    assert has_any_source("plain text no tags") is False


def test_extract_fact_keys() -> None:
    text = "EBITDA [fact:ebitda_def] vs ROE [fact:roe_def] vs raw [bi:art_001]"
    keys = extract_fact_keys(text)
    assert keys == ["ebitda_def", "roe_def"]
