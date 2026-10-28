"""`[cite:N]` 剥除规则的 FE↔BE 对拍（打法照 test_figure_caption_parity.py）。

为什么要有这条：`citation_marks.CITE_MARKER_RE` 与 FE `lib/citation-marks.ts` 的
`CITE_MARKER_RE` 是**跨语言双份常量**。同族的 `FIGURE_CAPTION_PREFIX_RE` 就在这上面
分叉过——`\\d` 在 Python 匹配全部 Unicode 十进制数字（含全角 ０-９），在 JS 只匹配
ASCII，986 条语料里 648 条行为不同。

这个 marker 的分叉后果同样是「画布与导出不一致」：canvas 侧藏住了、导出侧没藏，
用户就会在 PDF 里看到字面 `[cite:1]`（2026-08-15 116 实测就是这个症状，只不过当时
是导出侧**压根没有**这一层）。

⚠ 收口前 FE 有三份口径各异的拷贝（citation-plugin `\\d{1,3}` / format-inline-tokens
`\\d+` / 后端新加的 `[0-9]{1,3}`），现在 FE 两处都 import 同一个常量。
"""

from __future__ import annotations

import re
from pathlib import Path

from bowhead_core.citation_marks import (
    _ASCII_UNIT_MAP,
    _CURRENCY_SCALE_UNITS,
    CITE_MARKER_RE,
    strip_cite_markers,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TS_SOURCE = _REPO_ROOT / "packages/bowhead_frontend/lib/citation-marks.ts"

# Both sides must use ASCII classes only; divergence came from \d / \s / \w.
_FORBIDDEN_CLASSES = (r"\d", r"\s", r"\w")


def _extract_ts_regex() -> str:
    text = _TS_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"export const CITE_MARKER_RE = /(.+?)/g;", text)
    assert match, "没在 citation-marks.ts 里找到 CITE_MARKER_RE —— 抠取失效，这条闸会假绿"
    return match.group(1)


def test_regex_literal_matches_frontend() -> None:
    ts_source = _extract_ts_regex()
    assert ts_source == CITE_MARKER_RE.pattern, f"cite marker 正则分叉: FE={ts_source!r} server={CITE_MARKER_RE.pattern!r}"


def test_neither_side_uses_locale_dependent_classes() -> None:
    """`\\d` 在两个引擎语义不同 —— 显式 ASCII 集合是硬要求，不是风格偏好。"""
    ts_source = _extract_ts_regex()
    for forbidden in _FORBIDDEN_CLASSES:
        assert forbidden not in ts_source, f"FE 正则用了 {forbidden}（跨语言语义不同）"
        assert forbidden not in CITE_MARKER_RE.pattern, f"server 正则用了 {forbidden}"


def test_strips_cite_markers_but_never_touches_numbered_citations() -> None:
    """只剥 `[cite:N]`；`[N]` 是正典引用编号，导出件里本来就该出。"""
    assert strip_cite_markers("结论。 [cite:1]") == "结论。 "
    assert strip_cite_markers("甲 [cite:1] 乙 [cite:23] 丙") == "甲  乙  丙"
    # Positive control: preserve every character that should remain.
    assert strip_cite_markers("从19.82亿降至9.78亿 [1]") == "从19.82亿降至9.78亿 [1]"
    assert strip_cite_markers("数组 arr[0] 与 [cite] 都不算") == "数组 arr[0] 与 [cite] 都不算"
    assert strip_cite_markers("[citex:1] 也不算") == "[citex:1] 也不算"


def test_full_width_digits_are_not_matched_on_either_side() -> None:
    """全角数字不匹配 —— 这正是 `\\d` 会让两侧分叉的那个输入。"""
    assert strip_cite_markers("全角 [cite:１]") == "全角 [cite:１]"


# ─────────────────────────────────────────────────────────────
# Unit tables are also duplicated across languages (same family as the warning at the top).
# ─────────────────────────────────────────────────────────────
def _extract_ts_set(name: str) -> set[str]:
    text = _TS_SOURCE.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", text, re.S)
    assert match, f"没在 citation-marks.ts 里找到 {name} —— 抠取失效，这条闸会假绿"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _extract_ts_map_keys(name: str) -> set[str]:
    text = _TS_SOURCE.read_text(encoding="utf-8")
    match = re.search(rf"const {name}: Record<string, string> = \{{(.*?)\n\}};", text, re.S)
    assert match, f"没在 citation-marks.ts 里找到 {name} —— 抠取失效，这条闸会假绿"
    body = re.sub(r"//[^\n]*", "", match.group(1))  # Strip comments so their words do not count as keys.
    return {m.group(1) or m.group(2) for m in re.finditer(r'^\s*(?:"([^"]+)"|(\w+))\s*:', body, re.M)}


def test_currency_scale_units_match_frontend() -> None:
    """★缩放集分叉的后果是「同一个金额画布与导出不一致」——港币在一侧渲成
    `70.17亿港币`、另一侧渲成 `7016893124HKD`。"""
    assert _extract_ts_set("CURRENCY_SCALE_UNITS") == set(_CURRENCY_SCALE_UNITS)


def test_ascii_unit_map_keys_match_frontend() -> None:
    """单位后缀表两侧必须认识同一批单位串."""
    assert _extract_ts_map_keys("ASCII_UNIT_MAP") == set(_ASCII_UNIT_MAP)


def _extract_ts_regex_named(name: str) -> str:
    """抠 FE 的正则字面。容忍 prettier 把长正则换行（`= \n  /…/g;`）——
    写死单行的话，一次格式化就让这条闸静默失效。"""
    text = _TS_SOURCE.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} =\s*/(.+?)/g;", text, re.S)
    assert match, f"没在 citation-marks.ts 里找到 {name} —— 抠取失效，这条闸会假绿"
    return match.group(1)


def test_provenance_strip_covers_every_valid_kind() -> None:
    """★两端都要认得**全部**合法 kind —— 漏一个那个标签就会露给用户看。

    kind 词表在 `provenance.py::_VALID_KINDS`，FE 是手抄的第二份。
    """
    from bowhead_core.provenance import _VALID_KINDS

    ts = _extract_ts_regex_named("PROVENANCE_TAG_RE")
    for kind in _VALID_KINDS:
        assert kind in ts, f"FE provenance 正则漏了 kind {kind!r}"
    # ★★ Negative gate: `data:` **must not** enter the stripping list. `[data:art_xxx]` is active syntax rendered
    # as an artifact chip by md.ts's remark plugin; stripping it deletes artifact references (2026-08-26 review correction:
    # I briefly listed it as a junk tag).
    assert "data" not in ts, "data: 进了剥离名单——产物 chip 会被删掉"


def test_provenance_strip_agrees_across_languages() -> None:
    """同一段文本两端剥出来必须一样 —— 分叉的后果是画布与导出不一致。"""
    from bowhead_core.provenance import strip_provenance_tags

    ts = _extract_ts_regex_named("PROVENANCE_TAG_RE")
    py_equivalent = re.compile(ts)
    for sample in (
        "结论。 [llm-std:药品命名常识]",
        "营收 12 亿 [bi:art_001] 同比明显。",
        # Preserve artifact chips verbatim.
        "见 [data:art_abc123] 这张表",
        "数组 arr[0] 与 [1] 都不该动",
        "推导 [llm-rsn:bi:art_001|fact:cogs] 完成",
        # Shape observed on 116: model puts tags inside full-width parentheses.
        "上面是通用财务概念（[llm-std:]）。",
        # ★ False-positive guard: do not consume empty parentheses in code references.
        "调 foo() 之后再看 bar()",
        "（见上 [llm-std:]）",
    ):
        assert py_equivalent.sub("", sample).rstrip() == strip_provenance_tags(sample), sample


def test_provenance_strip_keeps_artifact_chips() -> None:
    """★★`[data:art_xxx]` 是产物 chip 的活语法，不是 provenance 标签。

    md.ts 的 remark 插件把它渲成可点的引用角标；剥掉等于把产物引用删掉。
    2026-08-26 review 订正：我一度按「教给过 Lead 但不是合法 kind」把它列进剥离名单。
    """
    from bowhead_core.provenance import strip_provenance_tags

    assert strip_provenance_tags("见 [data:art_abc123] 这张表") == "见 [data:art_abc123] 这张表"


def test_provenance_strip_never_touches_citation_numbers() -> None:
    """★误杀闸：`[N]` 是正典引用编号，`arr[0]` 是代码 —— 一个都不许动。"""
    from bowhead_core.provenance import strip_provenance_tags

    assert strip_provenance_tags("从19.82亿降至9.78亿 [1]") == "从19.82亿降至9.78亿 [1]"
    assert strip_provenance_tags("数组 arr[0] 正常") == "数组 arr[0] 正常"
    assert strip_provenance_tags("[cite:1] 不归它管") == "[cite:1] 不归它管"
