"""`_validate_legacy_output` —— 导出产物的格式自检（legacy job 路的最后一道闸）。

为什么单独补这组：这个函数**此前一条测试都没有**，于是
`required_prefix = "ppt/" if fmt == "pptx" else "xl/"` 这个二元式一直没人守 ——
docx 落进 else 被要求含 `xl/`，**每一次 Word 导出都判失败**
（真机 dev `exp_4e02c4d6896c`: `unexpected: ValueError: invalid Office output`）。

用**真的 OOXML 包**测（真按每种格式的主部件目录建 zip），不是造个假 zip 糊过去 ——
假替身正是这类 bug 能活下来的原因。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from bowhead_core.canvas_export import _validate_legacy_output

# fmt → main-part directory in that format's OOXML package
_MAIN_PART = {"pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml", "docx": "word/document.xml"}


def _ooxml(path: Path, main_part: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(main_part, "<root/>")
    return path


@pytest.mark.parametrize("fmt", sorted(_MAIN_PART))
def test_valid_ooxml_accepted_for_each_format(fmt: str, tmp_path: Path) -> None:
    """★ docx 这一条在修复前是红的 —— 它就是那次真机导出失败的最小复现。"""
    p = _ooxml(tmp_path / f"out.{fmt}", _MAIN_PART[fmt])
    _validate_legacy_output(p, fmt)  # No exception means success


@pytest.mark.parametrize("fmt", sorted(_MAIN_PART))
def test_wrong_main_part_rejected(fmt: str, tmp_path: Path) -> None:
    """闸不能因为放宽 docx 就变成谁都放行：主部件目录不对必须继续拒。"""
    wrong = next(v for k, v in _MAIN_PART.items() if k != fmt)
    p = _ooxml(tmp_path / f"bad.{fmt}", wrong)
    with pytest.raises(ValueError, match="invalid Office output"):
        _validate_legacy_output(p, fmt)


def test_pdf_magic_checked(tmp_path: Path) -> None:
    ok = tmp_path / "ok.pdf"
    ok.write_bytes(b"%PDF-1.4 ...")
    _validate_legacy_output(ok, "pdf")

    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    with pytest.raises(ValueError, match="invalid PDF output"):
        _validate_legacy_output(bad, "pdf")


def test_empty_output_rejected(tmp_path: Path) -> None:
    p = tmp_path / "empty.docx"
    p.write_bytes(b"")
    with pytest.raises(ValueError, match="export output is empty"):
        _validate_legacy_output(p, "docx")


def test_non_zip_rejected(tmp_path: Path) -> None:
    p = tmp_path / "notzip.docx"
    p.write_bytes(b"plain text, definitely not a zip")
    with pytest.raises(ValueError, match="invalid Office output"):
        _validate_legacy_output(p, "docx")


def test_unknown_fmt_is_rejected_not_silently_passed(tmp_path: Path) -> None:
    """未知格式必须显式拒 —— 别再让它悄悄落进某个分支当成另一种格式来校验。"""
    p = _ooxml(tmp_path / "x.rtf", "word/document.xml")
    with pytest.raises(ValueError, match="unsupported fmt"):
        _validate_legacy_output(p, "rtf")
