"""交付物文件名推导（dev 1.0.77 dogfood P2-a / P2-b）。"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_skill.export_naming import (
    FALLBACK_BASENAME,
    export_filename,
    report_title_from_sections,
    sanitize_basename,
)


def test_h1_wins_over_canvas_title() -> None:
    """真实缺陷形态：canvas.title 恒为 NULL（只有用户手动重命名才写），
    报告标题一直躺在 h1 里没人读。"""
    rows = [
        SimpleNamespace(kind="eyebrow", payload={"text": "销售达成"}),
        SimpleNamespace(kind="h1", payload={"text": "2026 年 8 月经营汇报"}),
    ]
    assert report_title_from_sections(rows, None) == "2026 年 8 月经营汇报"
    # h1 takes precedence even when canvas.title is set; h1 is the deliverable's own title.
    assert report_title_from_sections(rows, "未命名画布") == "2026 年 8 月经营汇报"


def test_flat_section_shape_supported() -> None:
    """`to_report_section()` 把 payload 打平了，两种形态都得吃下。"""
    flat = [{"kind": "h1", "text": "August Business Review", "id": "sec_1", "rev": 2}]
    assert report_title_from_sections(flat, None) == "August Business Review"


def test_falls_back_to_canvas_title_then_none() -> None:
    assert report_title_from_sections([], "季度复盘") == "季度复盘"
    assert report_title_from_sections([], None) is None
    # A canvas containing only chart / code_block (before compose runs) also provides no title.
    assert report_title_from_sections([SimpleNamespace(kind="bar", payload={"title": "x"})], None) is None


def test_filename_falls_back_to_legacy_name() -> None:
    """兜底必须仍是历史那个名字——换掉会让老用户以为文件丢了。"""
    assert export_filename(None, ".docx") == f"{FALLBACK_BASENAME}.docx"
    assert export_filename("", "xlsx") == f"{FALLBACK_BASENAME}.xlsx"


def test_filename_uses_title() -> None:
    assert export_filename("2026 年 8 月经营汇报", ".docx") == "2026 年 8 月经营汇报.docx"


def test_unsafe_characters_stripped() -> None:
    """标题里带 `/` 会在登记时变成路径分隔符；Windows 还禁 `:*?"<>|`。"""
    got = export_filename("营收/利润 分析: Q3 <草稿>", ".pdf")
    assert "/" not in got and ":" not in got and "<" not in got
    assert got.endswith(".pdf")


def test_control_chars_and_whitespace_collapsed() -> None:
    assert sanitize_basename("  报告\n\t标题  ") == "报告 标题"
    assert sanitize_basename("\x00\x01") is None


def test_trailing_dot_and_space_removed() -> None:
    """Windows 会静默剥掉首尾的 `.` 和空格 —— 留着会让登记名与落盘名对不上。"""
    assert sanitize_basename("报告. ") == "报告"
    assert sanitize_basename("...") is None


def test_reserved_device_names_rejected() -> None:
    """`CON.docx` 在 Windows 上打不开。"""
    assert sanitize_basename("CON") is None
    assert sanitize_basename("com1") is None
    assert export_filename("nul", ".docx") == f"{FALLBACK_BASENAME}.docx"


def test_long_title_truncated() -> None:
    got = sanitize_basename("长" * 200)
    assert got is not None
    assert len(got) == 60
