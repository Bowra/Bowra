"""飞书卡片的表格解析 —— 转义竖线会**静默丢行**，这是三个导出器里最狠的一处。

`_parse_markdown_table` 用列数做硬约束：数据行列数对不上就 `break`。
裸 `split("|")` 把单元格内的字面竖线 `\\|` 当分隔符 ⇒ 该行列数 +1 ⇒
**该行及其之后的所有行被直接丢掉**（出现在表头行则整张表退化成裸文本）。
不是渲错，是数据没了。

另钉 GFM 分隔行：`|-|-|` 一个连字符就合法，原实现要求 `len >= 3`。
"""

from __future__ import annotations

from bowhead_gateway.feishu.renderer import (
    _is_table_separator,
    _parse_markdown_table,
    _strip_markdown,
)


def test_escaped_pipe_does_not_drop_the_row() -> None:
    lines = [
        "| 指标 | 同比 | 备注 |",
        "| --- | --- | --- |",
        r"| 毛利 | -37.5% | a\|b |",
        "| 净利 | 12.0% | 正常 |",
    ]

    parsed = _parse_markdown_table(lines, 0)

    assert parsed is not None
    _header, rows, _end = parsed
    # Before the fix: the row containing `\|` had 4 != 3 columns → break → both it and the following '净利' row disappeared.
    assert len(rows) == 2, f"含转义竖线的行把后续行一起丢了：{rows}"
    assert rows[0] == ["毛利", "-37.5%", r"a\|b"]
    assert rows[1][0] == "净利"


def test_single_hyphen_separator_is_valid_gfm() -> None:
    """`|-|-|` 是合法 GFM；判成非表格会让整张表退化成 `tag:"markdown"` 裸文本。"""
    for sep in ("|-|-|", "|:-:|:-:|", "| --- | --- |", "| :--- | ---: |"):
        parsed = _parse_markdown_table(["| a | b |", sep, "| 1 | 2 |"], 0)
        assert parsed is not None, f"分隔行形态 {sep!r} 没认出来"
        assert parsed[1] == [["1", "2"]]

    # Negative control: non-separators must not be recognized as separator rows.
    assert not _is_table_separator("")
    assert not _is_table_separator("abc")
    assert not _is_table_separator("-a-")


def test_cell_text_is_unescaped_at_the_render_end() -> None:
    """卡片的 table 组件不解析 markdown ⇒ 单元格是渲染终点，反斜杠必须还原。"""
    assert _strip_markdown(r"\-37.5%") == "-37.5%"
    assert _strip_markdown(r"1\. 第一") == "1. 第一"
    assert _strip_markdown(r"a\|b") == "a|b"
    # Strip inline markup as before, without affecting unescaping.
    assert _strip_markdown(r"**重点** \-5%") == "重点 -5%"
