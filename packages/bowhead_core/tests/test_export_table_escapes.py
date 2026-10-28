"""导出器是**渲染终点** —— markdown 的反斜杠转义不能原样印给用户。

上游 `markdown-pm.ts::_serializeTableCell` 把每个单元格当独立文档序列化：
行首的 `-` / `N.` / `>` 被 prosemirror-markdown 的 `esc(startOfLine)` 加反斜杠
（`-37.5%` → `\\-37.5%`），竖线则**必须**保持 `\\|` 才不会把一格劈成两格。
写路径已经不再产出行首那批，但**存量 markdown 里还有**，而 `\\|` 是有意长期保留的 ——
所以三个导出器各自都得认。此前它们**一个都不认**，且都用裸 `split("|")` 切列。

顺带钉住 GFM 分隔行：`|-|-|` 一个连字符就合法，三个导出器原本都要求 2~3 个，
合法表会整张判不出来（Word 变裸竖线文本、PDF 摊成一行流水账）。

真跑脚本、真读回文件 —— 这两个渲染器栽过的坑都是「跑起来才看见」的。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCX_SCRIPT = ROOT / "packages/bowhead_skill/catalog/canvas-docx/scripts/render_from_canvas.py"
PDF_SCRIPT = ROOT / "packages/bowhead_skill/catalog/canvas-pdf/scripts/render_from_canvas.py"

# Cells combine leading block-level escapes, literal pipes, and single-hyphen delimiter rows.
NARRATIVE_MD = "\n".join(
    [
        "结论如下。",
        "",
        r"| 指标 | 同比\|环比 | 备注 |",
        "|-|-|-|",
        r"| 毛利 | \-37.5% | a\|b |",
        r"| 步骤 | 1\. 第一 | \> 注意 |",
    ]
)


def _sections() -> list[dict]:
    return [{"id": "n1", "kind": "narrative", "markdown": NARRATIVE_MD}]


def _run(script: Path, suffix: str) -> Path:
    workdir = Path(tempfile.mkdtemp(prefix=f"export-esc-{suffix}-"))
    (workdir / "input.json").write_text(
        json.dumps({"title": "T", "sections": _sections()}, ensure_ascii=False),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--input", str(workdir / "input.json"), "--workdir", str(workdir)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"render failed rc={proc.returncode}: {proc.stderr[-800:]}"
    out = workdir / f"output.{suffix}"
    assert out.is_file() and out.stat().st_size > 0
    return out


@pytest.mark.skipif(not DOCX_SCRIPT.is_file(), reason="canvas-docx script not present")
def test_docx_table_cells_are_unescaped_and_split_on_real_pipes() -> None:
    docx = pytest.importorskip("docx")
    d = docx.Document(str(_run(DOCX_SCRIPT, "docx")))

    assert len(d.tables) == 1, "`|-|-|` 分隔行没被认出来 ⇒ 整张表掉成裸竖线段落"
    tbl = d.tables[0]
    assert len(tbl.columns) == 3, "`a\\|b` 里的转义竖线被当成分隔符，凭空多切一列"
    cells = [c.text for r in tbl.rows for c in r.cells]

    assert "同比|环比" in cells, f"表头单元格没反转义：{cells}"
    assert "-37.5%" in cells, f"行首转义没还原：{cells}"
    assert "a|b" in cells, f"转义竖线没还原：{cells}"
    assert "1. 第一" in cells
    assert "> 注意" in cells
    assert not any("\\" in c for c in cells), f"还有字面反斜杠漏给用户：{cells}"


@pytest.mark.skipif(not PDF_SCRIPT.is_file(), reason="canvas-pdf script not present")
def test_pdf_table_cells_are_unescaped_and_split_on_real_pipes() -> None:
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(str(_run(PDF_SCRIPT, "pdf")))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert "同比|环比" in text, f"表头单元格没反转义：{text!r}"
    assert "-37.5%" in text, f"行首转义没还原：{text!r}"
    assert "a|b" in text, f"转义竖线没还原：{text!r}"
    assert "1. 第一" in text
    assert "\\-" not in text and "\\|" not in text, f"还有字面反斜杠漏给用户：{text!r}"
    # Recognizing the delimiter selects a Table flowable instead of flattening the entire table into one pipe-separated line.
    assert "|-|-|" not in text


@pytest.mark.skipif(not PDF_SCRIPT.is_file(), reason="canvas-pdf script not present")
def test_pdf_unescape_keeps_reportlab_xml_safe() -> None:
    """反向保险：`\\<` 还原成裸 `<` 会被 reportlab 的 Paragraph 当标签吃掉。

    Paragraph 那条路必须换成实体；纯文本的 Table 单元格那条路则要还原成真字符。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_pdf_render_esc", PDF_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:  # reportlab is missing.
        pytest.skip("reportlab not installed")

    assert mod._unescape_md(r"\<b\>") == "<b>"
    assert mod._unescape_md(r"\<b\>", xml=True) == "&lt;b&gt;"
    # Parse emphasis markers before unescaping; reversing the order treats `\*` as emphasis.
    assert mod._md_to_reportlab(r"**重点** \-37.5%") == "<b>重点</b> -37.5%"
