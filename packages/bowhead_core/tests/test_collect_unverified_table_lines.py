"""表格行必须参与数字核验——排版跳过清单不得当安全清单用。

老实现把琥珀标记 `[num:V:U]` 内联注入正文，往表格单元格注标记会破排版，
所以 `_collect_unverified` 跳过 `|` 行。M1-3 改成结构化字段 `unverified_numbers`
后不再注入任何东西，排版理由消失，跳过却留着——于是 writer 铁律 6 明令
「对比用 markdown 表格」写出来的数字，一个都不过核验。

116 全库实测：narrative 的 markdown 表格里 4,658 个数字，被核验的是 0 个。

成对断言（[[feedback_assert_absence_needs_positive_control]]）：
既钉「编造的表格数字会被抓」，也钉「合法的表格数字不会被误报」——
只写前者的话，把判据写成恒真也能过。
"""

from bowhead_core.report_writer_loop import _collect_unverified


def test_fabricated_number_in_table_is_flagged() -> None:
    """表格里凭空出现的数字必须进 unverified_numbers。"""
    authorized = {528312046.38, 286417930.62}
    content = "| 品牌 | 营业额 |\n|---|---|\n| 品牌甲 | 52831 万元 |\n| 幽灵品牌 | 88888 万元 |\n"

    out = _collect_unverified(content, authorized)

    # 88888 万元 = 8.8888e8, neither authorized nor derived from any authorized value
    assert any("88888" in token for token in out)


def test_faithful_rounding_and_unit_scale_in_table_is_not_flagged() -> None:
    """合法的单位换算 + 忠实四舍五入不得误报，否则等于把表格全判成不可信。

    528,312,046.38 元 ÷ 1e4 = 52,831.20 万元，模型写「52831 万元」是忠实舍入。
    """
    authorized = {528312046.38}
    content = "| 品牌 | 营业额 |\n|---|---|\n| 品牌甲 | 52831 万元 |\n"

    out = _collect_unverified(content, authorized)

    assert out == []


def test_table_lines_are_no_longer_skipped_wholesale() -> None:
    """反向保险：同一个编造数字，写在表格行里和写在正文里必须同样被抓。

    跳过一旦回归，这条会红——而只测正文的用例不会。
    """
    authorized = {100.0}
    in_table = _collect_unverified("| 指标 | 值 |\n|---|---|\n| 营收 | 77777 万元 |\n", authorized)
    in_prose = _collect_unverified("本期营收 77777 万元。\n", authorized)

    assert in_prose, "正文里的编造数字本来就该被抓（若这条红了说明用例本身失效）"
    assert in_table, "表格行被整体跳过 —— 排版清单又被当成安全清单了"


def test_headings_blockquotes_and_figure_anchors_stay_skipped() -> None:
    """这三类不承载待核验业务数字，仍应跳过——别把取消跳过做成一刀切。"""
    authorized = {100.0}
    content = "## 2026 年营收 99999 万元\n> 引用：99999 万元\n[figure:sec_abc] 99999 万元\n"

    assert _collect_unverified(content, authorized) == []


def test_code_fence_still_skipped() -> None:
    """代码块里的数字不是报告结论，不参与核验。"""
    authorized = {100.0}
    content = "```python\nrevenue = 55555  # 万元\n```\n"

    assert _collect_unverified(content, authorized) == []


def test_bare_cell_gets_unit_from_table_header() -> None:
    """BI 表格常见形态是单位写在表头、单元格里是裸数字。

    116 全库：表格里 3,057 个数字属于这种形态（占三分之二）。`_MARK_NUM_RE` 要求数字
    紧跟单位，不做表头推断就既取不到 token 也没法做量级换算 —— 等于对这批数字漏检。
    """
    authorized = {528312046.38}
    md = "| 品牌 | 营业额（万元） |\n|------|---------------|\n| 品牌甲 | 52831 |\n| 幽灵品牌 | 88888 |\n"

    out = _collect_unverified(md, authorized)

    # Detect fabricated values after applying the column header's unit.
    assert any("88888" in token for token in out)
    # Do not flag valid values (528,312,046.38 ÷ 1e4 = 52,831.20, faithfully rounded).
    assert not any("52831" in token for token in out)


def test_cell_with_its_own_unit_is_not_double_suffixed() -> None:
    """单元格自带单位时不能再补表头单位，否则 `1.2 万元` 会变成 `1.2万元万元`。"""
    authorized = {12000.0}
    md = "| 项目 | 金额（万元） |\n|---|---|\n| 甲 | 1.2 万元 |\n"

    # 1.2 万元 = 12000 is authorized; duplicating the unit would cause a parse failure and false alarm.
    assert _collect_unverified(md, authorized) == []


def test_header_unit_prefers_longer_unit() -> None:
    """「万元」必须整体命中，不能被「万」先吃掉 —— 数量级会差 1e4。"""
    from bowhead_core.report_writer_loop import _header_units

    assert _header_units("| 品牌 | 营业额（万元） |") == [None, "万元"]
    assert _header_units("| 品牌 | 营业额（亿元） |") == [None, "亿元"]
    assert _header_units("| 品牌 | 占比 % |") == [None, "%"]


def test_header_units_reset_between_tables() -> None:
    """一张表的表头单位不得泄漏到后面的段落或另一张表。"""
    # Use nonmatching values in a nonempty authorized set (empty sets skip verification), above the small-integer allowlist.
    authorized = {123456.0}
    md = "| 项目 | 金额（万元） |\n|---|---|\n| 甲 | 7777 |\n\n| 名称 | 数量（个） |\n|---|---|\n| 乙 | 8888 |\n"

    out = _collect_unverified(md, authorized)

    # Apply each table's own header units; the second table must not inherit the first table's "万元".
    assert "7777万元" in out
    assert "8888个" in out
    assert not any(token == "8888万元" for token in out)


def test_all_dash_row_is_not_mistaken_for_a_separator() -> None:
    """`| - | - |` 是**全空值数据行**，不是分隔行。

    误判的伤害不在这一行本身，而在表头判据「下一行是分隔行」：它上面那行会被当成表头，
    整行不核验，且 col_units 被覆写成全 None，**这张表后面所有行随之失去表头单位**。

    ⚠ 原先靠「每格至少三个连字符」收紧 —— 那**不是 GFM spec**（见
    `test_gfm_separator_variants_are_all_recognised`），而且防不住 `| --- | --- |`
    这种占位行。真正的判据是**位置**：分隔行只可能是表格的第二行。
    """
    authorized = {123456.0}
    md = "| 品牌 | 营业额（万元） |\n|---|---|\n| 品牌甲 | 11111 |\n| - | - |\n| 品牌乙 | 22222 |\n"

    out = _collect_unverified(md, authorized)

    # Verify data rows before and after null rows as usual.
    assert "11111万元" in out
    assert "22222万元" in out


def test_placeholder_dash_row_mid_table_does_not_poison_the_rest() -> None:
    """表格中间出现一行 `| --- | --- |`（模型写的占位行）不得毒化整张表。

    逐行 re-match 分隔行正则做不到这件事：占位行与真分隔行**语法上完全一样**，
    GFM spec 本身也不区分。老实现把它当分隔行 ⇒ 它上面那行被当成新表头（该行不核验）
    ⇒ col_units 覆写成全 None ⇒ 后面所有行失去表头单位。实测老实现整张表返回 `[]`。
    """
    authorized = {123456.0}
    md = "| 品牌 | 营业额（万元） |\n| --- | --- |\n| 甲 | 11111 |\n| --- | --- |\n| 乙 | 22222 |\n"

    out = _collect_unverified(md, authorized)

    assert "11111万元" in out
    assert "22222万元" in out


def test_adjacent_tables_without_a_blank_line_are_one_table() -> None:
    """两张表之间没有空行时，GFM 认为那是**一张**表 —— 单位跟着第一张的表头。

    这是位置判据的**必然取舍**，写下来是为了让下一个人知道它是刻意的：
    「表中间的 `| --- | --- |`」与「紧接着的第二张表的分隔行」在语法上完全一样，
    不可能同时把前者判成数据行、把后者判成新表头。选跟随 GFM ——
    渲染器就是这么显示的，核验口径跟渲染保持一致比另发明一套更安全。
    留空行（正常写法）时各按各自表头，见 `test_header_units_reset_between_tables`。
    """
    authorized = {123456.0}
    md = "| 品牌 | 营业额（万元） |\n| --- | --- |\n| 甲 | 11111 |\n| 名称 | 数量（个） |\n| --- | --- |\n| 乙 | 22222 |\n"

    out = _collect_unverified(md, authorized)

    assert "11111万元" in out
    assert "22222万元" in out, "第二段被当成新表头了 —— 与 GFM 渲染不一致"


def test_gfm_separator_variants_are_all_recognised() -> None:
    """`|-|-|`、`|:-:|:-:|`、省略尾竖线 —— GFM spec 里**一个连字符就合法**。

    收紧成「至少三个连字符」的代价比它想防的漏检更大：整张表判不出表头 ⇒ col_units
    取不到 ⇒ 表里的裸数字**全部**失核验。实测收紧版对 `|-|-|` 的表返回 `[]`。
    """
    authorized = {123456.0}
    for sep in ("|-|-|", "|:-:|:-:|", "| --- | ---", "| :--- | ---: |"):
        md = f"| 品牌 | 营业额（万元） |\n{sep}\n| 甲 | 11111 |\n"
        assert "11111万元" in _collect_unverified(md, authorized), f"分隔行形态 {sep!r} 没认出来"


def test_bare_horizontal_rule_is_not_a_separator() -> None:
    """反向保险：裸 `---` 是水平分隔线，不能把它上面那行 `|` 认成表头。"""
    from bowhead_core.report_writer_loop import _TABLE_SEP_RE

    assert not _TABLE_SEP_RE.match("---")
    assert not _TABLE_SEP_RE.match("- - -")


def test_second_number_in_the_same_cell_is_not_skipped() -> None:
    """`值（同比 X）` 是 BI 表格最常见的复合格式之一，第二个数字不能静默逃逸。"""
    authorized = {528312046.38}
    md = "| 品牌 | 营业额（万元） |\n|---|---|\n| 品牌甲 | 52,831（去年 99999） |\n"

    out = _collect_unverified(md, authorized)

    # 52,831 is valid (faithfully rounded from 528,312,046.38 ÷ 1e4); 99999 is fabricated.
    assert any("99999" in token for token in out)
    assert not any(token.startswith("52,831") for token in out)


def test_composite_cell_keeps_header_unit() -> None:
    """判据必须是「该数字后面紧跟的是不是单位」，不是「整格出现过单位字」。

    否则 `88888（+12%）` 里的 `%` 会让整格失去表头单位，编造值直接漏检。
    """
    authorized = {123456.0}
    md = "| 品牌 | 营业额（万元） |\n|---|---|\n| 品牌甲 | 88888（+12%） |\n"

    assert any("88888" in t for t in _collect_unverified(md, authorized))


def test_header_unit_requires_a_delimiter_not_substring_match() -> None:
    """整格子串匹配会把中文表头读出假单位，其中「万」直接差 1e4。"""
    from bowhead_core.report_writer_loop import _header_units

    assert _header_units("| 品牌 | 万科营收 |") == [None, None]
    assert _header_units("| 国家 | 门店 |") == [None, None]
    assert _header_units("| 单元 | 数值 |") == [None, None]
    # Still extract supported forms.
    assert _header_units("| 品牌 | 营业额（万元） |") == [None, "万元"]
    assert _header_units("| 品牌 | 占比 % |") == [None, "%"]
    assert _header_units("| 品牌 | 营业额 / 元 |") == [None, "元"]


def test_prose_tokens_are_not_squeezed_out_by_a_long_table() -> None:
    """表格通常排在结论段之前；按文档序截断会把正文里的前瞻性编造挤出 20 条窗口。"""
    authorized = {123456.0}
    rows = "".join(f"| 品牌{i} | {1000 + i} |\n" for i in range(40))
    md = "| 品牌 | 营业额（万元） |\n|---|---|\n" + rows + "\n综上，预计全年利润将达 6666 亿元。\n"

    out = _collect_unverified(md, authorized)

    assert "6666亿元" in out, "正文结论被表格明细挤出了窗口"
    assert out[0] == "6666亿元", "正文 token 必须排在表格 token 之前"


def test_negative_numbers_are_reportable() -> None:
    """表格的「同比 / 环比 / 差额」列天然大量是负数。

    `verify_numbers_in_text` 本来就正确识别 `-37.5` 并判定未授权，只是取 token 的
    `_MARK_NUM_RE` 不含负号 ⇒ 匹配不上、静默丢弃。把表格纳入核验后，这条从
    「散文里偶尔」变成「表格里成片」。
    """
    authorized = {999999999.0}
    md = "| 品牌 | 同比（%） |\n|---|---|\n| 甲 | -37.5 |\n"

    assert _collect_unverified(md, authorized) == ["-37.5%"]


def test_minus_must_hug_the_digit_so_list_markers_are_safe() -> None:
    """`- 37.5%` 是 markdown 列表项，不是负数——读成 -37.5 会把符号弄反。"""
    authorized = {999999999.0}

    assert _collect_unverified("- 37.5% 的增长\n", authorized) == ["37.5%"]


def test_dates_and_en_dash_ranges_are_not_read_as_negatives() -> None:
    """`2026-08-11` 的连字符前是数字（被 lookbehind 排除）；区间用的是 en-dash。"""
    authorized = {999999999.0}

    assert _collect_unverified("截至 2026-08-11 营收 4321 万元\n", authorized) == ["4321万元"]
    assert _collect_unverified("区间 1.22–1.60亿\n", authorized) == ["1.60亿"]


def test_hyphen_after_cjk_unit_is_a_range_not_a_minus() -> None:
    """`1000万-1600万元` 是**区间**。lookbehind 只排了数字/字母，没排 CJK ⇒
    连字符被读成负号 ⇒ 报给用户一个凭空的 `-1600万元`（实测产出
    `['1000万', '-1600万元']`）。区间两端都必须各自正常取到。
    """
    authorized = {999999999.0}

    assert _collect_unverified("预计规模 1000万-1600万元。\n", authorized) == ["1000万", "1600万元"]


def test_number_verifier_and_token_regex_agree_on_the_hyphen() -> None:
    """两处判据必须切出同一批数字，否则数字会**静默消失**。

    取 token 的 `_MARK_NUM_RE` 与判授权的 `number_verifier._NUM_RE` 是同一件事的两处实现。
    只修其中一处时实测：token 取到 `1600`、核验器报的是 `-1600`，两边对不上 ⇒
    `1600万元` 整个不见了 —— 把「值报错」换成了「值消失」，更难发现。
    """
    from bowhead_core.number_verifier import verify_numbers_in_text

    raws = [raw for raw, _v in verify_numbers_in_text("预计规模 1000万-1600万元。", {999999999.0})]

    assert raws == ["1000", "1600"]


def test_escaped_pipe_does_not_shift_columns() -> None:
    """`\\|` 是单元格内的字面竖线，裸 split 会凭空多切一列，表头单位落到错误的列。

    本模块的 serializer 写单元格时正是把 `|` 转义成 `\\|`，往返越多越容易撞上。
    """
    from bowhead_core.report_writer_loop import _header_units, _split_row

    assert len(_split_row(r"| 名称 \| 别名 | 营业额 |")) == 2
    # The unit must stay in column 2 (revenue); an extra split must not shift it.
    assert _header_units(r"| 名称 \| 别名 | 营业额（万元） |") == [None, "万元"]
