"""language_directive 单测：末尾段生成 + glossary 注入 + tool 约束."""

from __future__ import annotations

import pytest
from bowhead_core.language_context import LanguageContext
from bowhead_core.language_directive import build_language_directive
from bowhead_core.product_terms import (
    AGENT_ANALYST,
    AGENT_LEAD,
    ProductTerm,
    ProductTermsCatalog,
)


@pytest.fixture
def catalog() -> ProductTermsCatalog:
    return ProductTermsCatalog(
        version=1,
        terms={
            "agent": ProductTerm("agent", "Agent", "智能体", frozenset({"all"})),
            "canvas": ProductTerm("canvas", "Canvas", "画布", frozenset({AGENT_LEAD})),
            "metric": ProductTerm("metric", "Metric", "指标", frozenset({AGENT_ANALYST})),
        },
    )


@pytest.fixture
def lang_ctx_zh() -> LanguageContext:
    return LanguageContext(
        ui_language="zh-CN",
        output_language="zh-CN",
        data_language="zh-CN",
        week_start_day="monday",
    )


@pytest.fixture
def lang_ctx_en_with_zh_data() -> LanguageContext:
    """Use English for interface and answers with Chinese source data."""
    return LanguageContext(
        ui_language="en-US",
        output_language="en-US",
        data_language="zh-CN",
        week_start_day="monday",
    )


class TestBasicShape:
    def test_none_context_returns_empty(self) -> None:
        assert build_language_directive(None, AGENT_LEAD) == ""

    def test_zh_context_basic_structure(self, lang_ctx_zh: LanguageContext) -> None:
        out = build_language_directive(lang_ctx_zh, AGENT_LEAD)
        assert out.startswith("\n\n## 输出语言与术语规范")
        assert "output_language=zh-CN" in out
        assert "data_language=zh-CN" in out
        assert "monday" in out


class TestRealisticScenarios:
    def test_english_answers_with_chinese_data(
        self,
        lang_ctx_en_with_zh_data: LanguageContext,
    ) -> None:
        out = build_language_directive(lang_ctx_en_with_zh_data, AGENT_LEAD)
        assert "output_language=en-US" in out
        assert "data_language=zh-CN" in out
        # Tool constraints must emphasize Chinese entities.
        assert "data_language=zh-CN" in out
        assert "保留原文不翻译" in out


class TestGlossaryInjection:
    def test_glossary_injected_for_lead(
        self,
        lang_ctx_zh: LanguageContext,
        catalog: ProductTermsCatalog,
    ) -> None:
        out = build_language_directive(lang_ctx_zh, AGENT_LEAD, catalog=catalog)
        # lead must see agent + canvas (all + lead tags).
        assert "agent → 智能体" in out
        assert "canvas → 画布" in out
        # No metric glossary entries (only analyst is tagged for metric).
        # Note: directive's "metric tool" is explanatory prose, not a glossary entry; distinguish by →.
        assert "metric →" not in out

    def test_glossary_injected_for_analyst_uses_en(
        self,
        catalog: ProductTermsCatalog,
    ) -> None:
        ctx = LanguageContext(
            ui_language="en-US",
            output_language="en-US",
            data_language="en-US",
            week_start_day="monday",
        )
        out = build_language_directive(ctx, AGENT_ANALYST, catalog=catalog)
        # analyst sees both metric + agent in English.
        assert "metric → Metric" in out
        assert "agent → Agent" in out

    def test_no_catalog_skips_glossary(self, lang_ctx_zh: LanguageContext) -> None:
        out = build_language_directive(lang_ctx_zh, AGENT_LEAD)  # No catalog
        assert "产品术语" not in out


class TestToolConstraint:
    def test_tool_constraint_included_by_default(
        self,
        lang_ctx_en_with_zh_data: LanguageContext,
    ) -> None:
        out = build_language_directive(lang_ctx_en_with_zh_data, AGENT_LEAD)
        assert "Tool 调用约束" in out
        assert "verify it with an available Tool" in out
        assert "state the uncertainty instead of guessing" in out
        assert "Inspect column labels with an available schema Tool" in out

    def test_tool_constraint_excluded_for_pure_text_agents(
        self,
        lang_ctx_zh: LanguageContext,
    ) -> None:
        out = build_language_directive(
            lang_ctx_zh,
            AGENT_LEAD,
            include_tool_constraint=False,
        )
        assert "Tool 调用约束" not in out

    def test_tool_constraint_uses_data_language(self) -> None:
        ctx = LanguageContext(
            ui_language="en-US",
            output_language="en-US",
            data_language="zh-CN",
            week_start_day="monday",
        )
        out = build_language_directive(ctx, AGENT_LEAD)
        # Mentions data_language=zh-CN.
        assert "data_language=zh-CN" in out
        # Example must include "北京同仁医院".
        assert "北京同仁医院" in out


class TestWeekStartDay:
    def test_monday_default(self, lang_ctx_zh: LanguageContext) -> None:
        out = build_language_directive(lang_ctx_zh, AGENT_LEAD)
        assert "monday" in out

    def test_sunday_workspace(self) -> None:
        ctx = LanguageContext(
            ui_language="zh-CN",
            output_language="zh-CN",
            data_language="zh-CN",
            week_start_day="sunday",
        )
        out = build_language_directive(ctx, AGENT_LEAD)
        assert "sunday" in out


class TestRealCatalogIntegration:
    def test_with_real_catalog(self, lang_ctx_zh: LanguageContext) -> None:
        from bowhead_core.product_terms import default_catalog_path, load_catalog

        path = default_catalog_path()
        if not path.exists():
            pytest.skip("real product_terms.yaml not present")
        catalog = load_catalog(path)
        out = build_language_directive(lang_ctx_zh, AGENT_LEAD, catalog=catalog)
        # Real catalog includes dreaming → 梦境模式.
        assert "dreaming → 梦境模式" in out
        assert "canvas → 画布" in out
