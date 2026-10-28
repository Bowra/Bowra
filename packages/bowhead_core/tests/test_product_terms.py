"""ProductTerms (L1 glossary) loader / agent filter / prompt section 渲染单测."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.product_terms import (
    AGENT_ALL,
    AGENT_ANALYST,
    AGENT_COMPUTE,
    AGENT_LEAD,
    ProductTerm,
    ProductTermsCatalog,
    default_catalog_path,
    load_catalog,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "product_terms.yaml"
    p.write_text(content, encoding="utf-8")
    return p


class TestLoadCatalog:
    def test_load_basic(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
terms:
  canvas:
    en: Canvas
    zh: 画布
    agents: [lead, analyst]
  agent:
    en: Agent
    zh: 智能体
    agents: [all]
""",
        )
        cat = load_catalog(path)
        assert cat.version == 1
        assert "canvas" in cat.terms
        canvas = cat.terms["canvas"]
        assert canvas.en == "Canvas"
        assert canvas.zh == "画布"
        assert canvas.agents == frozenset({"lead", "analyst"})

    def test_load_with_note(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
terms:
  measure:
    en: Measure
    zh: 度量
    agents: [analyst]
    note: 与 metric 区分
""",
        )
        cat = load_catalog(path)
        assert cat.terms["measure"].note == "与 metric 区分"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_catalog(tmp_path / "nonexistent.yaml")

    def test_missing_terms_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "version: 1\n")
        with pytest.raises(ValueError, match="missing `terms`"):
            load_catalog(path)

    def test_empty_terms_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "version: 1\nterms: {}\n")
        with pytest.raises(ValueError, match="zero terms"):
            load_catalog(path)

    def test_term_missing_en_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
terms:
  canvas:
    zh: 画布
    agents: [lead]
""",
        )
        with pytest.raises(ValueError, match="missing en or zh"):
            load_catalog(path)

    def test_invalid_agent_tag_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
terms:
  canvas:
    en: Canvas
    zh: 画布
    agents: [unknown_agent]
""",
        )
        with pytest.raises(ValueError, match="invalid agent tag"):
            load_catalog(path)

    def test_agents_must_be_list(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
version: 1
terms:
  canvas:
    en: Canvas
    zh: 画布
    agents: lead
""",
        )
        with pytest.raises(ValueError, match="must be a list"):
            load_catalog(path)


class TestAgentFilter:
    @pytest.fixture
    def catalog(self) -> ProductTermsCatalog:
        return ProductTermsCatalog(
            version=1,
            terms={
                "workspace": ProductTerm("workspace", "Workspace", "工作区", frozenset({AGENT_ALL})),
                "canvas": ProductTerm("canvas", "Canvas", "画布", frozenset({AGENT_LEAD, AGENT_ANALYST})),
                "compute_op": ProductTerm("compute_op", "Compute", "计算", frozenset({AGENT_COMPUTE})),
                "ui_only": ProductTerm("ui_only", "Send", "发送", frozenset()),  # Empty = frontend only.
            },
        )

    def test_all_tag_includes_for_lead(self, catalog: ProductTermsCatalog) -> None:
        terms = catalog.filter_for_agent(AGENT_LEAD)
        keys = {t.key for t in terms}
        assert "workspace" in keys  # Matched by 'all'.
        assert "canvas" in keys  # Matched by explicit lead.
        assert "compute_op" not in keys  # Compute only.
        assert "ui_only" not in keys  # Frontend only.

    def test_filter_for_compute(self, catalog: ProductTermsCatalog) -> None:
        terms = catalog.filter_for_agent(AGENT_COMPUTE)
        keys = {t.key for t in terms}
        assert keys == {"workspace", "compute_op"}

    def test_empty_agents_excluded(self, catalog: ProductTermsCatalog) -> None:
        for agent in [AGENT_LEAD, AGENT_ANALYST, AGENT_COMPUTE]:
            terms = catalog.filter_for_agent(agent)
            assert all(t.key != "ui_only" for t in terms)

    def test_alphabetical_order(self, catalog: ProductTermsCatalog) -> None:
        terms = catalog.filter_for_agent(AGENT_LEAD)
        keys = [t.key for t in terms]
        assert keys == sorted(keys)


class TestRenderPromptSection:
    @pytest.fixture
    def catalog(self) -> ProductTermsCatalog:
        return ProductTermsCatalog(
            version=1,
            terms={
                "agent": ProductTerm("agent", "Agent", "智能体", frozenset({AGENT_ALL})),
                "canvas": ProductTerm("canvas", "Canvas", "画布", frozenset({AGENT_LEAD})),
                "metric": ProductTerm("metric", "Metric", "指标", frozenset({AGENT_ANALYST})),
            },
        )

    def test_render_zh_for_lead(self, catalog: ProductTermsCatalog) -> None:
        section = catalog.render_prompt_section(AGENT_LEAD, output_language="zh-CN")
        assert "- agent → 智能体" in section
        assert "- canvas → 画布" in section
        assert "metric" not in section  # Not for lead.

    def test_render_en_for_lead(self, catalog: ProductTermsCatalog) -> None:
        section = catalog.render_prompt_section(AGENT_LEAD, output_language="en-US")
        assert "- agent → Agent" in section
        assert "- canvas → Canvas" in section

    def test_render_all_only_when_no_specific_match(self, catalog: ProductTermsCatalog) -> None:
        section = catalog.render_prompt_section("unknown", output_language="zh-CN")
        assert section == "- agent → 智能体"


class TestProductTermLabel:
    def test_label_zh(self) -> None:
        t = ProductTerm("canvas", "Canvas", "画布", frozenset())
        assert t.label("zh-CN") == "画布"

    def test_label_en(self) -> None:
        t = ProductTerm("canvas", "Canvas", "画布", frozenset())
        assert t.label("en-US") == "Canvas"


class TestRealCatalog:
    """加载真实的 config/glossary/product_terms.yaml 验证不出错."""

    def test_real_yaml_loads(self) -> None:
        path = default_catalog_path()
        if not path.exists():
            pytest.skip(f"product_terms.yaml not at expected path: {path}")
        cat = load_catalog(path)
        assert cat.version == 1
        # Spot-check a few key terms.
        assert "canvas" in cat.terms
        assert cat.terms["canvas"].zh == "画布"
        assert "agent" in cat.terms
        assert cat.terms["agent"].zh == "智能体"
        assert "dreaming" in cat.terms
        assert cat.terms["dreaming"].zh == "梦境模式"
        # Verify the initial scale of 51 terms.
        assert len(cat.terms) >= 50

    def test_real_yaml_lead_subset(self) -> None:
        path = default_catalog_path()
        if not path.exists():
            pytest.skip("product_terms.yaml not at expected path")
        cat = load_catalog(path)
        lead_terms = cat.filter_for_agent(AGENT_LEAD)
        keys = {t.key for t in lead_terms}
        # canvas / agent / dreaming should all be visible to lead.
        assert "canvas" in keys
        assert "agent" in keys
        assert "dreaming" in keys

    def test_real_yaml_render_zh(self) -> None:
        path = default_catalog_path()
        if not path.exists():
            pytest.skip("product_terms.yaml not at expected path")
        cat = load_catalog(path)
        section = cat.render_prompt_section(AGENT_LEAD, output_language="zh-CN")
        assert "canvas → 画布" in section
        assert "dreaming → 梦境模式" in section
