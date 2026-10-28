"""canvas-docx 渲染契约 —— 真跑脚本、真读回 .docx。

为什么必须真跑：本 sprint 里 canvas-docx 连着栽了两次，两次都是**跑起来才看见**的：
1. 内联 markdown 把 `pure_chrome_kind` 渲成 `purechromekind`（下划线强调没按 CommonMark
   只认词间，`province_str` 这类 snake_case 会被吃掉）；
2. ★ `insight_card` 没实现，而去重逻辑又会把它引用的 chart/image 从顶层折叠 ——
   **净效果是那张图从文档里彻底消失**（116 实测导出开头连着 4 行占位注记暴露了这条）。
纯静态审阅这两条都看不出来。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "packages/bowhead_skill/catalog/canvas-docx/scripts/render_from_canvas.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="canvas-docx script not present")


def _render(sections: list, title: str = "T") -> tuple[str, list]:
    """跑脚本 → 读回 (全文纯文本, 表格列表)。"""
    docx = pytest.importorskip("docx")
    workdir = Path(tempfile.mkdtemp(prefix="docx-test-"))
    (workdir / "input.json").write_text(json.dumps({"title": title, "sections": sections}, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(workdir / "input.json"), "--workdir", str(workdir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"render failed: {proc.stderr[-800:]}"
    out = workdir / "output.docx"
    assert out.is_file() and out.stat().st_size > 0
    d = docx.Document(str(out))
    text = "\n".join(p.text for p in d.paragraphs)
    text += "\n" + "\n".join(c.text for t in d.tables for r in t.rows for c in r.cells)
    return text, d.tables


def test_insight_card_evidence_chart_is_not_lost() -> None:
    """★ 核心回归：被 insight_card 引用的 chart 顶层折叠后，必须在卡内渲出来。

    修复前：顶层跳过 + 卡片没实现 ⇒ 图彻底消失，且卡片本身渲成 `(insight_card — …)`。
    """
    sections = [
        {"id": "c1", "kind": "chart", "title": "各品牌营业额", "rows": [{"品牌": "金辉福", "营业额": 41438885.5}]},
        {
            "kind": "insight_card",
            "title": "头部品牌集中",
            "thesis": "前两名贡献过半",
            "evidence": [{"chart_section_id": "c1", "caption": "图1 各品牌营业额"}],
        },
    ]
    text, tables = _render(sections)
    assert "(insight_card" not in text, "卡片渲成了占位注记"
    assert "头部品牌集中" in text and "前两名贡献过半" in text
    assert "图1 各品牌营业额" in text, "证据 caption 丢了"
    assert any("品牌" in c.text for t in tables for c in t.rows[0].cells), "被引用的图表数据整个消失了"
    assert len(tables) == 1, "顶层折叠失效 → 同一张图渲了两次"


def test_recommendations_and_sources_render() -> None:
    sections = [
        {"kind": "recommendations", "items": [{"priority": "P1", "action": "复核合约", "owner": "商品部"}]},
        {"kind": "sources", "label": "数据来源", "entries": [{"label": "bi_query", "query": "select 1"}]},
    ]
    text, _ = _render(sections)
    assert "复核合约" in text and "商品部" in text
    assert "数据来源" in text and "bi_query" in text


def test_interactive_kinds_degrade_visibly_not_vanish() -> None:
    """导出契约③：进不了 Word 的交互内容要**降级可见**，不能蒸发、也不许打 unsupported。"""
    text, _ = _render([{"kind": "html_artifact", "title": "交互看板", "description": "含筛选器"}])
    assert "交互看板" in text and "含筛选器" in text
    assert "unsupported" not in text.lower()


def test_snake_case_survives_inline_markdown() -> None:
    """下划线强调只认词间（CommonMark）——否则 `province_str` 会被吃成 `provincestr`。"""
    text, _ = _render([{"kind": "narrative", "markdown": "字段 province_str 与 pure_chrome_kind 必须原样保留"}])
    assert "province_str" in text
    assert "pure_chrome_kind" in text


def test_missing_image_degrades_to_alt_not_silence() -> None:
    text, _ = _render([{"kind": "image", "alt": "趋势图", "path": "nope.png"}])
    assert "趋势图" in text


def test_empty_sections_yields_placeholder_not_crash() -> None:
    text, _ = _render([])
    assert "empty canvas" in text


def test_chart_title_comes_from_spec_not_section_top_level() -> None:
    """真实 chart section 的标题在 `spec.title`（payload 形状就是 `{"spec": {...}}`）。

    读错层级不会报错、也不会丢数据——只是每张图变成一张**无名表**。116 1.2.71 dogfood
    实测：六张图渲成六张匿名表，正文里「图N：」的引用对不上是哪张。所以这里按**真实形状**
    构造 fixture（不是顶层 title 的简化形状），否则测试会把这个 bug 一起钉住。
    """
    text, tables = _render(
        [
            {
                "kind": "chart",
                "id": "sec_c1",
                "spec": {
                    "title": "各品类营业额同比变化",
                    "subtitle": "2026年1-8月 vs 2025年同期",
                    "rows": [{"品类": "外套", "同比": "-6.2%"}, {"品类": "上装", "同比": "-8.9%"}],
                },
            }
        ]
    )
    assert "各品类营业额同比变化" in text, "图表标题必须渲染，否则 Word 里是一张无名表"
    assert "2026年1-8月 vs 2025年同期" in text
    assert len(tables) == 1 and "外套" in text  # No regression in the data table itself
