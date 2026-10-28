"""feedback-0713 F5 (B4 债): canvas-pdf image section 内嵌/折叠.

B4 把 chart_section_id 变成视觉引用双桶 (chart/image 同字段), FE 把被引用的
image 折进 insight card——PDF 此前不镜像: insight evidence 引用 image 时卡内
不渲, 顶层也不折叠。修后: 卡内内嵌 + 顶层被引用 image 折叠 (仅显式引用的折,
与 FE 同规则)。断言法: pypdf 数页内 image XObject——引用图折叠后全文恰好
2 张 (卡内 1 + 独立 1); 不折叠会是 3 张。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "packages/bowhead_skill/catalog/canvas-pdf/scripts/render_from_canvas.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="canvas-pdf script not present")


def _tiny_png(path: Path, color) -> None:
    from PIL import Image

    Image.new("RGB", (320, 180), color).save(str(path))


def _run_render(input_data: dict) -> Path:
    workdir = Path(tempfile.mkdtemp(prefix="pdf-test-"))
    (workdir / "input.json").write_text(json.dumps(input_data, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(workdir / "input.json"), "--workdir", str(workdir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"render failed rc={proc.returncode}: {proc.stderr[-800:]}")
    out = workdir / "output.pdf"
    assert out.is_file(), f"output.pdf missing; stderr={proc.stderr[-400:]}"
    return out


def _pdf_image_count(pdf_path: Path) -> int:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return sum(len(page.images) for page in reader.pages)


def _pdf_text(pdf_path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)


@pytest.mark.unit
def test_referenced_image_inlined_in_card_and_folded_at_top_level(tmp_path):
    png_a = tmp_path / "a.png"
    png_b = tmp_path / "b.png"
    _tiny_png(png_a, (46, 93, 79))
    _tiny_png(png_b, (181, 68, 59))
    data = {
        "title": "img 报告",
        "sections": [
            {"id": "h", "kind": "h1", "text": "图像折叠验证"},
            {
                "id": "ins",
                "kind": "insight_card",
                "title": "洞察甲",
                "thesis": "模拟收敛良好",
                "narrative": "见图。",
                # B4 dual bucket: image evidence uses the same chart_section_id field.
                "evidence": [{"kind": "image", "chart_section_id": "img_ref", "caption": "模拟图"}],
            },
            # Referenced → embedded in card + folded at top level (appears once).
            {"id": "img_ref", "kind": "image", "artifact_id": "art_a", "_local_path": str(png_a)},
            # Unreferenced → render at top level as usual.
            {"id": "img_free", "kind": "image", "artifact_id": "art_b", "caption": "独立图", "_local_path": str(png_b)},
        ],
    }
    out = _run_render(data)
    n = _pdf_image_count(out)
    assert n == 2, f"引用图应折叠 (卡内 1 + 独立 1 = 2), 实际 {n} (3 = 没折叠, 1 = 卡内没内嵌)"


@pytest.mark.unit
def test_image_without_local_path_renders_placeholder_not_crash(tmp_path):
    data = {
        "title": "无图字节",
        "sections": [
            {"id": "h", "kind": "h1", "text": "占位"},
            {"id": "img_x", "kind": "image", "artifact_id": "art_x", "caption": "取不到"},
        ],
    }
    out = _run_render(data)
    assert _pdf_image_count(out) == 0


@pytest.mark.unit
def test_legacy_pdf_consumes_all_four_embedded_data_copy_states_without_verified_claims() -> None:
    messages = [
        "Generation data attached · Source citations [1] [2]",
        "Generation data attached · No source citations provided",
        "Static content was not checked against tool output · Source citations [3]",
        "Static content was not checked against tool output",
    ]
    out = _run_render(
        {
            "title": "Embedded provenance",
            "sections": [
                {
                    "id": f"html-{index}",
                    "kind": "html_artifact",
                    "title": f"Block {index}",
                    "embedded_data_copy": message,
                }
                for index, message in enumerate(messages, start=1)
            ],
        }
    )
    text = _pdf_text(out)
    normalized = " ".join(text.split()).casefold()
    for message in messages:
        assert " ".join(message.split()).casefold() in normalized
    assert "verified data" not in normalized
    assert "verified sources" not in normalized
