"""V2.3 单测: canvas-pptx render_from_canvas.py.

策略: 不真起 LLM, 用合成 ReportSection[] fixture 跑 render → assert
output.pptx 存在 + slide count + 关键 placeholder 文本被替换 + 截短规则.

CI dependency note (V2.3 D):
- 仅依赖 python-pptx (Python lib), **不依赖** LibreOffice / soffice.
- render_from_canvas.py 用 python-pptx 直接 load + edit .pptx, 不调用 office binary.
- LibreOffice 仅在本地 dev 跑 PDF 缩略图 review 视觉时用 (soffice --convert-to pdf),
  那是 dev workflow 不是 CI.
- CI workflow .github/workflows/ci.yml 不装 LibreOffice.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "packages/bowhead_skill/catalog/canvas-pptx/scripts/render_from_canvas.py"
TEMPLATE_ROOT = ROOT / "packages/bowhead_skill/catalog/canvas-pptx/templates"
TEMPLATE = TEMPLATE_ROOT / json.loads((TEMPLATE_ROOT / "default.json").read_text())["template"]


# ─── fixtures ───────────────────────────────────────────────


def _ea_canvas() -> dict:
    """合成简化 EA 类 canvas (含 cover/exec/kpi/insight/recs)."""
    return {
        "title": "Test Report",
        "sections": [
            {"id": "sec_eb", "kind": "eyebrow", "text": "合规监控 · 测试"},
            {"id": "sec_h1", "kind": "h1", "text": "EA 单据合规监控看板"},
            {"id": "sec_dk", "kind": "deck", "markdown": "测试 deck 段落"},
            {
                "id": "sec_kpi",
                "kind": "kpi_row",
                "cells": [
                    {"lbl": "总单据数", "val": "9,497", "sub": "11 月累计"},
                    {"lbl": "整体合规率", "val": "8.38%", "sub": "▼ 2.1pp", "subTone": "neg"},
                    {"lbl": "部门支出", "val": "8.30%", "sub": "≈ 持平"},
                    {"lbl": "TOP 体系", "val": "51.6%", "sub": "▲ 12pp", "subTone": "pos"},
                ],
            },
            {
                "id": "sec_narr1",
                "kind": "narrative",
                "markdown": "受监控范围 9,497 单, 整体合规率 8.38%, 信息技术体系 51.6% 领跑.",
            },
            {
                "id": "sec_chart1",
                "kind": "chart",
                "title": "各体系合规率",
                "spec": {
                    "kind": "bar",
                    "title": "Compliance by System",
                    "rows": [
                        {"体系": "信息技术", "率": 51.6},
                        {"体系": "投资公行", "率": 14.1},
                        {"体系": "营销", "率": 8.5},
                        {"体系": "研发技术", "率": 0.3},
                    ],
                    "columns": [
                        {"key": "体系", "header": "体系", "type": "categorical"},
                        {"key": "率", "header": "合规率 (%)", "type": "numeric"},
                    ],
                },
            },
            {
                "id": "sec_ins1",
                "kind": "insight_card",
                "title": "总体",
                "thesis": "全量结算单据合规率仅 8.38%, 超 9 成单据未关联前置流程",
                "narrative": "全量数据 71,370 行, 受监控范围 9,497 单, 整体合规 8.38%.",
                "evidence": [{"kind": "chart", "chart_section_id": "sec_chart1", "caption": "各体系"}],
            },
            {
                "id": "sec_recs",
                "kind": "recommendations",
                "items": [
                    {
                        "priority": "P0",
                        "action": "针对无请购 3,379 单专项治理 + 历史合同清理",
                        "rationale": "占违规单据 50%",
                        "owner": "财经体系",
                        "timeline": "2 周内",
                    },
                    {
                        "priority": "P1",
                        "action": "全公司单据双签制度上线",
                        "rationale": "颗粒度从部门级降至单据级",
                        "owner": "审计部",
                        "timeline": "1 月",
                    },
                ],
            },
        ],
    }


def _trend_canvas() -> dict:
    """含 trend chart 的 canvas, 验 slide 5 line chart 路径."""
    return {
        "sections": [
            {"id": "h1", "kind": "h1", "text": "趋势报告"},
            {"id": "kp", "kind": "kpi_row", "cells": [{"lbl": "GMV", "val": "16.5B"}]},
            {
                "id": "ch1",
                "kind": "chart",
                "spec": {
                    "kind": "line",
                    "title": "Monthly GMV",
                    "rows": [{"month": f"{m}月", "v": 10 + m * 0.5} for m in range(1, 13)],
                    "columns": [
                        {"key": "month", "header": "月", "type": "categorical"},
                        {"key": "v", "header": "GMV (亿)", "type": "numeric"},
                    ],
                },
            },
        ],
    }


# ─── helpers ────────────────────────────────────────────────


def _run_render(input_data: dict, *, template_path: str | None = None) -> tuple[Path, dict]:
    """跑 render → 返 (output_pptx, result_json)."""
    workdir = Path(tempfile.mkdtemp(prefix="pptx-test-"))
    try:
        if template_path:
            input_data["template_path"] = template_path
        (workdir / "input.json").write_text(
            json.dumps(input_data, ensure_ascii=False),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", str(workdir / "input.json"), "--workdir", str(workdir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"render failed: {proc.stderr}")
        out = workdir / "output.pptx"
        result_json = workdir / "result.json"
        result = json.loads(result_json.read_text()) if result_json.exists() else {}
        return out, result
    finally:
        # Keep workdir for debugging failures; pytest automatically cleans tmp.
        pass


def _slide_texts(pptx_path: Path) -> list[list[str]]:
    """返 [[slide1_texts], [slide2_texts], ...]."""
    from pptx import Presentation

    prs = Presentation(str(pptx_path))
    out = []
    for slide in prs.slides:
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
        out.append(texts)
    return out


def _chart_fingerprints(pptx_path: Path) -> list[tuple[tuple[str, ...], tuple[float, ...]]]:
    """Return native-chart categories/values for residue assertions."""
    from pptx import Presentation

    prs = Presentation(str(pptx_path))
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_chart:
                continue
            plot = shape.chart.plots[0]
            categories = tuple("" if str(category) == "None" else str(category) for category in plot.categories)
            values = tuple(float(value or 0) for value in plot.series[0].values)
            out.append((categories, values))
    return out


def _package_members(pptx_path: Path) -> set[str]:
    with zipfile.ZipFile(pptx_path) as archive:
        return set(archive.namelist())


def _package_text_nodes(pptx_path: Path) -> str:
    with zipfile.ZipFile(pptx_path) as archive:
        texts = []
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            texts.extend(re.findall(rb"<a:t>(.*?)</a:t>", archive.read(name), flags=re.DOTALL))
        return b"".join(texts).decode("utf-8", errors="ignore")


def _embedded_workbook_xml(pptx_path: Path) -> list[str]:
    """Read nested XLSX XML so hidden template data is tested, not only chart caches."""
    payloads = []
    with zipfile.ZipFile(pptx_path) as archive:
        workbook_names = [name for name in archive.namelist() if name.startswith("ppt/embeddings/")]
        for workbook_name in workbook_names:
            with zipfile.ZipFile(io.BytesIO(archive.read(workbook_name))) as workbook:
                payloads.append(
                    "".join(workbook.read(name).decode("utf-8", errors="ignore") for name in workbook.namelist() if name.endswith(".xml"))
                )
    return payloads


# ─── tests ──────────────────────────────────────────────────


@pytest.mark.unit
def test_render_basic_output_exists():
    out, result = _run_render(_ea_canvas())
    assert out.is_file()
    assert out.stat().st_size > 30_000  # Real .pptx is at least 30KB
    assert result["slides"] == 6  # no line/trend input → no demo trend slide
    assert result["demo_residue_count"] == 0
    assert result["sections"] == len(_ea_canvas()["sections"])


@pytest.mark.unit
def test_render_cover_h1_replaced():
    out, _ = _run_render(_ea_canvas())
    texts = _slide_texts(out)
    cover = " ".join(texts[0])
    assert "EA 单据合规监控看板" in cover, f"cover h1 not replaced: {cover}"


@pytest.mark.unit
def test_render_kpi_values_replaced():
    out, _ = _run_render(_ea_canvas())
    texts = _slide_texts(out)
    kpi_text = " ".join(next(slide for slide in texts if "9,497" in " ".join(slide)))
    assert "9,497" in kpi_text
    assert "8.38%" in kpi_text
    assert "51.6%" in kpi_text


@pytest.mark.unit
def test_render_insight_thesis_is_complete():
    """Long insight text wraps without character-count truncation."""
    canvas = _ea_canvas()
    long_thesis = "X" * 200
    ins = next(s for s in canvas["sections"] if s["kind"] == "insight_card")
    ins["thesis"] = long_thesis
    out, _ = _run_render(canvas)
    texts = _slide_texts(out)
    matches = re.findall(r"X+", " ".join(text for slide in texts for text in slide))
    assert sum(map(len, matches)) == 200


@pytest.mark.unit
def test_render_recommendations_action_is_complete():
    canvas = _ea_canvas()
    recs = next(s for s in canvas["sections"] if s["kind"] == "recommendations")
    recs["items"][0]["action"] = "Y" * 100
    out, _ = _run_render(canvas)
    texts = _slide_texts(out)
    matches = re.findall(r"Y+", " ".join(text for slide in texts for text in slide))
    assert sum(map(len, matches)) == 100


@pytest.mark.unit
def test_render_trend_chart_path():
    """Line charts retain their source title and native chart type."""
    out, _ = _run_render(_trend_canvas())
    texts = _slide_texts(out)
    assert any("Monthly GMV" in " ".join(slide) for slide in texts)
    from pptx import Presentation
    from pptx.enum.chart import XL_CHART_TYPE

    charts = [shape.chart for slide in Presentation(str(out)).slides for shape in slide.shapes if shape.has_chart]
    assert [chart.chart_type for chart in charts] == [XL_CHART_TYPE.LINE]


@pytest.mark.unit
def test_render_with_invalid_template_falls_back():
    """非法 template_path → fallback default baseline (CTO review hotfix V2.1)."""
    out, result = _run_render(_ea_canvas(), template_path="/tmp/nonexistent.pptx")
    assert out.is_file()
    assert result["slides"] == 6  # Fallback baseline must also remove empty trend demo


@pytest.mark.unit
def test_render_rejects_template_path_outside_temp_or_workdir() -> None:
    """Reject caller template paths outside explicitly isolated temp/work roots.

    Pin the subprocess temp root so this security check also works when the
    complete Community checkout itself resides under the host temp directory.
    """
    workdir = Path(tempfile.mkdtemp(prefix="pptx-sec-"))
    (workdir / "input.json").write_text(
        json.dumps({**_ea_canvas(), "template_path": str(TEMPLATE)}, ensure_ascii=False),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(workdir / "input.json"), "--workdir", str(workdir)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TMPDIR": str(workdir)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "outside allowed roots" in proc.stderr, f"guard did not fire: {proc.stderr!r}"
    assert (workdir / "output.pptx").is_file()


@pytest.mark.unit
def test_render_honors_template_path_inside_temp() -> None:
    """canvas_export 把上传模板 tempfile.mkdtemp 到 OS temp 根下——守卫必须放行 temp 路径
    (不误拒), 正常 render。"""
    import shutil

    staged = Path(tempfile.mkdtemp(prefix="bow-pptx-tpl-")) / "user.pptx"
    shutil.copy(str(TEMPLATE), str(staged))
    out, result = _run_render(_ea_canvas(), template_path=str(staged))
    assert out.is_file()
    assert result["slides"] == 6


@pytest.mark.unit
def test_render_handles_minimal_canvas():
    """只有 h1 时不得把默认模板的业务事实或 embedded workbook 当成报告内容。"""
    minimal = {"sections": [{"id": "h1", "kind": "h1", "text": "Minimal"}]}
    out, result = _run_render(minimal)
    assert out.is_file()
    assert out.stat().st_size > 20_000
    assert result["slides"] == 1
    assert result["demo_residue_count"] == 0
    all_text = " ".join(text for slide in _slide_texts(out) for text in slide)
    for marker in ("9,497", "8.38%", "51.6%", "研发技术", "EA 结算单据"):
        assert marker not in all_text
    members = _package_members(out)
    assert not any(name.startswith("ppt/charts/chart") for name in members)
    assert not any(name.startswith("ppt/embeddings/") for name in members)
    package_xml = _package_text_nodes(out)
    for marker in ("CMP-2604-001", "1003", "1004", "1005"):
        assert marker not in package_xml


@pytest.mark.unit
def test_render_bar_only_removes_demo_trend_and_workbook() -> None:
    """EA fixture only has a bar chart; template's 12-month trend must disappear entirely."""
    out, result = _run_render(_ea_canvas())
    assert result["slides"] == 6
    fingerprints = _chart_fingerprints(out)
    assert len(fingerprints) == 1
    assert fingerprints[0][0] == ("信息技术", "投资公行", "营销", "研发技术")
    all_text = " ".join(text for slide in _slide_texts(out) for text in slide)
    assert "12 月持续阴跌" not in all_text
    assert "Overall Compliance Rate" not in all_text
    members = _package_members(out)
    assert len([name for name in members if name.startswith("ppt/embeddings/")]) == 1
    workbook_xml = "".join(_embedded_workbook_xml(out))
    assert "信息技术" in workbook_xml
    assert "研发技术" in workbook_xml


@pytest.mark.unit
def test_render_deletes_extra_title_only_template_slide() -> None:
    """A third TITLE_ONLY slide is neither exec nor recommendations and must not survive."""
    from pptx import Presentation
    from pptx.util import Inches

    staged = Path(tempfile.mkdtemp(prefix="bow-pptx-tpl-extra-")) / "extra.pptx"
    prs = Presentation(str(TEMPLATE))
    title_only_layout = next(layout for layout in prs.slide_layouts if (layout.name or "").upper().replace(" ", "_") == "TITLE_ONLY")
    extra = prs.slides.add_slide(title_only_layout)
    marker = extra.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
    marker.text = "SHOULD NOT SURVIVE"
    # Add one copied business-demo shape too, proving whole-slide deletion rather than text allowlisting.
    extra.shapes._spTree.append(deepcopy(prs.slides[1].shapes[2].element))
    prs.save(str(staged))

    out, result = _run_render(_ea_canvas(), template_path=str(staged))
    assert result["slides"] == 6
    all_text = " ".join(text for slide in _slide_texts(out) for text in slide)
    assert "SHOULD NOT SURVIVE" not in all_text


@pytest.mark.unit
def test_render_chart_data_replaced():
    """V2.3.1: chart slide 的 native chart 真换成了 canvas 数据 (不只是文本对)."""
    from pptx import Presentation

    out, _ = _run_render(_ea_canvas())
    prs = Presentation(str(out))
    chart_shapes = [sh for slide in prs.slides for sh in slide.shapes if sh.has_chart]
    assert chart_shapes, "The Canvas chart must appear as an editable chart"
    chart = chart_shapes[0].chart
    # Categories and values must match the fixture.
    plot = chart.plots[0]
    cats = [str(c) for c in plot.categories]
    assert "信息技术" in cats and "研发技术" in cats, f"chart categories 没换: {cats}"
    series_values = list(plot.series[0].values)
    # fixture: 信息技术 51.6 / 投资公行 14.1 / 营销 8.5 / 研发技术 0.3
    assert any(abs(v - 51.6) < 0.01 for v in series_values), f"chart values 没换: {series_values}"


@pytest.mark.unit
def test_render_kpi_label_remains_readable():
    """Long labels wrap at readable size instead of shrinking to 7 points."""
    canvas = _ea_canvas()
    kpi = next(s for s in canvas["sections"] if s["kind"] == "kpi_row")
    kpi["cells"][0] = {"lbl": "受监控总单据数全公司全口径", "val": "9,497"}
    out, _ = _run_render(canvas)
    from pptx import Presentation

    prs = Presentation(str(out))
    target = next(sh for slide in prs.slides for sh in slide.shapes if sh.has_text_frame and "受监控" in sh.text_frame.text)
    sizes = [r.font.size.pt for p in target.text_frame.paragraphs for r in p.runs if r.font.size]
    assert sizes
    assert min(sizes) >= 16


@pytest.mark.unit
def test_render_includes_sql_block_and_data_table():
    canvas = _ea_canvas()
    canvas["sections"].append({"id": "sql1", "kind": "sql_block", "sql": "SELECT 1"})
    canvas["sections"].append({"id": "dt1", "kind": "data_table", "rows": [{"a": 1}]})
    out, result = _run_render(canvas)
    assert result["slides"] == 8
    assert any("SELECT 1" in text for slide in _slide_texts(out) for text in slide)
    from pptx import Presentation

    assert any(shape.has_table for slide in Presentation(str(out)).slides for shape in slide.shapes)


# ─── feedback-0713 F5: image sections on chart slides (B4 dual visual-reference bucket) ───


def _tiny_png(path: Path, color=(46, 93, 79)) -> None:
    from PIL import Image

    Image.new("RGB", (640, 360), color).save(str(path))


def _picture_count(pptx_path: Path) -> int:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(str(pptx_path))
    return sum(1 for slide in prs.slides for sh in slide.shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE)


@pytest.mark.unit
def test_render_insight_referenced_image_embeds_picture(tmp_path):
    """insight evidence 引用 image (chart_section_id 双桶) → chart slide 嵌图.

    此前 canvas-pptx 完全不渲 image section: image-backed insight 两个池都进
    不去, chart slide 留 template demo 数据。
    """
    png = tmp_path / "fig.png"
    _tiny_png(png)
    canvas = _ea_canvas()
    canvas["sections"] = [s for s in canvas["sections"] if s["kind"] != "chart"]
    canvas["sections"].append({"id": "img1", "kind": "image", "artifact_id": "art_x", "caption": "蒙特卡洛模拟结果", "_local_path": str(png)})
    ins = next(s for s in canvas["sections"] if s["kind"] == "insight_card")
    ins["evidence"] = [{"kind": "image", "chart_section_id": "img1"}]
    out, _ = _run_render(canvas)
    assert _picture_count(out) >= 1, "insight 引用的 image 应嵌入 picture shape"
    # Insight text still goes on the slide (uses the chart-slide template).
    texts = [t for slide in _slide_texts(out) for t in slide]
    assert any("合规率仅 8.38%" in t for t in texts)


@pytest.mark.unit
def test_render_standalone_image_embeds_picture(tmp_path):
    """独立 image (无 insight 包裹) 同样上 chart slide."""
    png = tmp_path / "fig2.png"
    _tiny_png(png, color=(181, 68, 59))
    canvas = _ea_canvas()
    canvas["sections"] = [s for s in canvas["sections"] if s["kind"] not in ("chart", "insight_card")]
    canvas["sections"].append({"id": "img2", "kind": "image", "artifact_id": "art_y", "caption": "波形图", "_local_path": str(png)})
    out, _ = _run_render(canvas)
    assert _picture_count(out) >= 1, "独立 image 应嵌入 picture shape"


@pytest.mark.unit
def test_render_image_without_local_path_no_crash(tmp_path):
    """预取失败 (无 _local_path) 的独立 image 不排队不崩溃 (脚本退出 0)."""
    canvas = _ea_canvas()
    canvas["sections"].append({"id": "img3", "kind": "image", "artifact_id": "art_z", "caption": "取不到的图"})
    out, result = _run_render(canvas)
    assert out.is_file()
    assert "image_unavailable" in result["warnings"]


@pytest.mark.unit
def test_render_image_clears_hidden_demo_chart_workbook(tmp_path: Path) -> None:
    """An image may cover the native chart visually, but its embedded workbook must be scrubbed too."""
    png = tmp_path / "fig3.png"
    _tiny_png(png)
    canvas = _ea_canvas()
    canvas["sections"] = [s for s in canvas["sections"] if s["kind"] not in ("chart", "insight_card")]
    canvas["sections"].append(
        {
            "id": "img4",
            "kind": "image",
            "artifact_id": "art_image",
            "caption": "真实图片",
            "_local_path": str(png),
        }
    )
    out, _ = _run_render(canvas)
    assert _picture_count(out) == 1
    assert _chart_fingerprints(out) == []
    workbook_payloads = _embedded_workbook_xml(out)
    assert workbook_payloads == []


@pytest.mark.unit
def test_render_image_with_bytes_wins_over_bytesless_insight_image(tmp_path):
    """F5 review P2: 无字节 image insight 不再抢 chart slide 渲成「真文案配 template
    假图」——有字节的独立 image 应优先被渲。"""
    png = tmp_path / "real.png"
    _tiny_png(png)
    canvas = _ea_canvas()
    canvas["sections"] = [s for s in canvas["sections"] if s["kind"] != "chart"]
    # Insight-referenced image without bytes (previously claimed cursor0, rendering real text with a template demo chart).
    canvas["sections"].append({"id": "img_nobyte", "kind": "image", "artifact_id": "art_x"})
    ins = next(s for s in canvas["sections"] if s["kind"] == "insight_card")
    ins["evidence"] = [{"kind": "image", "chart_section_id": "img_nobyte"}]
    # Standalone image with bytes.
    canvas["sections"].append({"id": "img_real", "kind": "image", "artifact_id": "art_y", "_local_path": str(png)})
    out, _ = _run_render(canvas)
    assert _picture_count(out) >= 1, "有字节 image 应被渲染, 不被无字节 insight image 抢占"


# ─── feedback-triage-2026-08-24 A · export template residue ───


def _chart_number_formats(pptx_path: Path) -> list[str]:
    """Return every numFmt formatCode present in the deck's chart parts.

    ⚠ 必须 unescape：OOXML 里 `0.0"%"` 落盘是 `0.0&quot;%&quot;`。第一版忘了这一步，
    于是 `'0.0"%"' not in codes` 这条**恒为真** —— 一个逮不到任何东西的死判据。
    下面 `test_render_percent_column_keeps_percent_format` 是它的 positive control：
    那条断言 `0.0"%"` **在**列表里，helper 一旦再瞎掉它就先红。
    """
    with zipfile.ZipFile(pptx_path) as archive:
        codes: list[str] = []
        for name in archive.namelist():
            if "/charts/" not in name or not name.endswith(".xml"):
                continue
            xml = archive.read(name).decode("utf-8", errors="ignore")
            codes.extend(html.unescape(code) for code in re.findall(r'<c:numFmt[^>]*formatCode="([^"]*)"', xml))
        return codes


@pytest.mark.unit
def test_kpi_value_size_thresholds_match_web() -> None:
    """KPI 分档阈值必须与 Web 侧对拍（feedback-triage-2026-08-24）。

    同一份报告在网页和 deck 上是两套渲染器：Web 是 `kpi-row.tsx` 的
    `kpiValueSizeTier`，deck 是本 skill 的 `_kpi_value_font_size`。deck 跑在沙箱里，
    跨运行时没法共用代码，只能各写一份 —— 那就至少把**阈值**钉死对拍，
    否则「改一边记得改另一边」永远只是注释里的一句愿望。
    字号本身不对拍（两边基准不同：Web 32px / deck 44pt）。
    """
    web = (ROOT / "packages/bowhead_frontend/components/canvas/sections/kpi-row.tsx").read_text(encoding="utf-8")
    web_thresholds = [int(n) for n in re.findall(r"if \(width <= (\d+)\) return", web)]

    script = SCRIPT.read_text(encoding="utf-8")
    body = script.split("def _kpi_value_font_size")[1].split("\ndef ")[0]
    deck_thresholds = [int(n) for n in re.findall(r"if width <= (\d+):", body)]

    assert web_thresholds, "没解析到 Web 侧阈值 —— kpiValueSizeTier 形态变了就更新本测试"
    assert deck_thresholds, "没解析到 deck 侧阈值 —— _kpi_value_font_size 形态变了就更新本测试"
    assert web_thresholds == deck_thresholds, f"两侧分档阈值分叉：web={web_thresholds} deck={deck_thresholds}"
    # Both sides must use **display width**, never revert to character count (deck previously used len(text)).
    assert "ord(ch) > 127" in body, "deck 侧退回按字符数判了"
    assert "charCodeAt(0) > 127" in web, "web 侧退回按字符数判了"


@pytest.mark.unit
def test_render_carries_no_vendor_branding() -> None:
    """Exported decks must not retain vendor branding in slide, layout or master XML."""
    out, _ = _run_render(_ea_canvas())
    package_text = _package_text_nodes(out)
    normalized = "".join(package_text.split()).casefold()
    for token in ("bowhead", "北极九章", "bowhead.ai"):
        assert token not in normalized, f"导出件仍带厂商品牌串 {token!r}"


@pytest.mark.unit
def test_render_chart_number_format_does_not_inherit_template_percent() -> None:
    """An absolute-value chart must not inherit the template percentage format."""
    canvas = _ea_canvas()
    chart = next(s for s in canvas["sections"] if s["kind"] == "chart")
    chart["spec"]["rows"] = [
        {"体系": "Example Product A", "率": 125000},
        {"体系": "Example Product B", "率": 42000},
        {"体系": "Example Product C", "率": 36000},
    ]
    out, _ = _run_render(canvas)
    codes = _chart_number_formats(out)
    assert codes, "chart part 应带 numFmt"
    assert '0.0"%"' not in codes, f"数据标签仍挂着模板的百分比档: {codes}"
    assert '0"%"' not in codes, f"值轴仍挂着模板的百分比档: {codes}"
    assert any("#,##0" in code for code in codes), f"未写入千分位档: {codes}"


def _percent_canvas(*, column_patch: dict, rows: list[dict]) -> dict:
    canvas = _ea_canvas()
    chart = next(s for s in canvas["sections"] if s["kind"] == "chart")
    chart["spec"]["columns"][1].update(column_patch)
    chart["spec"]["rows"] = rows
    return canvas


@pytest.mark.unit
def test_render_percent_column_keeps_percent_format() -> None:
    """A-2 反向对照：真的百分比列仍要渲成百分比，别把修复做成一刀切。

    两种落库形状**光看值推不出来**（`bowhead_core.units` 的契约：达成率 1.02 是
    fraction=102%、市场份额 2.45 是 points=2.45%，值域重叠、含义相反），所以判据
    只认元数据：
    - `format="percent"` 在渲染面恒等于 fraction → Excel 的 `0.0%`（Excel 自己乘 100）
    - `percent_scale="points"` / `unit="%"` → 值已放大过 → 字面后缀 `0.0"%"`
    选错就是 100 倍误读。这条同时是 `_chart_number_formats` 的 positive control ——
    helper 若再忘了 unescape，它会先红。
    """
    fraction = _percent_canvas(
        column_patch={"format": "percent"},
        rows=[{"体系": "A", "率": 0.275}, {"体系": "B", "率": 0.166}],
    )
    assert "0.0%" in _chart_number_formats(_run_render(fraction)[0])

    # ★ Same value 0.9: points requires a literal suffix; never guess fraction from the value range
    #   (wrong guess renders 0.9% as 90%).
    points = _percent_canvas(
        column_patch={"percent_scale": "points"},
        rows=[{"体系": "A", "率": 0.9}, {"体系": "B", "率": 2.45}],
    )
    assert '0.0"%"' in _chart_number_formats(_run_render(points)[0])

    unit_only = _percent_canvas(
        column_patch={"unit": "%"},
        rows=[{"体系": "A", "率": 27.5}, {"体系": "B", "率": 16.6}],
    )
    assert '0.0"%"' in _chart_number_formats(_run_render(unit_only)[0])


def _doc_props_xml(pptx_path: Path) -> str:
    """docProps 两个 part 的原始 XML。

    ⚠ 刻意**不复用** `_package_text_nodes` —— 那个 helper 虽然遍历整包 .xml，
    却只抽 `<a:t>` 绘图文本节点，而文档属性长在 `<dc:creator>` / `<Company>` 里，
    一个 `<a:t>` 都没有。「遍历了整包」和「看得见整包」是两回事，A-1 第一轮
    就是栽在这个差别上：slide 全清干净了，属性里整份留着。
    """
    with zipfile.ZipFile(pptx_path) as archive:
        names = [n for n in archive.namelist() if n.startswith("docProps/") and n.endswith(".xml")]
        assert names, "包里没有 docProps —— 判据失效，请更新本测试"
        return "".join(archive.read(n).decode("utf-8", errors="ignore") for n in names)


@pytest.mark.unit
def test_render_document_properties_carry_no_vendor_branding() -> None:
    """Document properties must not retain vendor branding."""
    out, _ = _run_render(_ea_canvas())
    props = _doc_props_xml(out)
    normalized = "".join(props.split()).casefold()
    for token in ("bowhead", "北极九章", "bowhead.ai"):
        assert token not in normalized, f"文档属性仍带厂商品牌串 {token!r}"


@pytest.mark.unit
def test_render_document_title_is_the_report_not_the_template_demo() -> None:
    """Document properties must use the report title rather than template metadata."""
    canvas = _ea_canvas()
    h1 = next(s for s in canvas["sections"] if s["kind"] == "h1")
    h1["text"] = "2026 年商超经营专项分析"
    out, _ = _run_render(canvas)
    props = _doc_props_xml(out)

    title = re.search(r"<dc:title>(.*?)</dc:title>", props, re.DOTALL)
    assert title is not None, "core.xml 没有 dc:title —— 判据失效"
    assert title.group(1) == "2026 年商超经营专项分析", f"dc:title 不是本报告标题：{title.group(1)!r}"
    assert "PptxGenJS" not in props, "dc:subject 仍是模板的 'PptxGenJS Presentation'"


@pytest.mark.unit
def test_default_template_residue_scan_covers_document_properties() -> None:
    """Detect intentionally tainted document properties and verify that cleanup removes them."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_rfc", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_rfc"] = module
    spec.loader.exec_module(module)

    from pptx import Presentation  # noqa: PLC0415

    # Add a synthetic vendor attribution so the check does not depend on a captured template.
    dirty = Presentation(str(TEMPLATE))
    dirty.core_properties.author = "Bowhead Analytics"
    dirty.core_properties.title = "Synthetic template example"
    found = module._document_property_residue(dirty, deck_title="2026 年商超经营专项分析")
    assert found, "扫描器看不见 docProps —— 安全网是死的"
    assert any(f.startswith("docprop_brand:") for f in found), f"没认出厂商品牌串：{found}"

    # Cleaning must make this pass; otherwise the assertion is always true and cannot prove the scanner works.
    from datetime import datetime as _dt  # noqa: PLC0415

    module._scrub_document_properties(
        dirty,
        deck_title="2026 年商超经营专项分析",
        stamped=_dt(2026, 8, 25, 12, 0, 0),
    )
    assert module._document_property_residue(dirty, deck_title="2026 年商超经营专项分析") == []
