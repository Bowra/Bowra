"""多语言批1：本轮生效语言（turn pin）的裁决与回落。

tech-design §4.1。重点不是"函数返回什么"，而是**那条存量 bug 的正对照**：
turn 头补写 pin 之前，silent-done 路径（本轮没产出/报错）的三条文案一律落 zh-CN，
英文用户看到的失败提示是中文的。
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.effective_language import (
    LEGACY_PIN_ATTR,
    PIN_ATTR,
    effective_output_language,
    pin_turn_output_language,
    read_turn_pin,
)
from bowhead_core.language_context import LanguageContext


def _bus() -> SimpleNamespace:
    return SimpleNamespace()


def test_chain_pin_beats_context() -> None:
    bus = _bus()
    pin_turn_output_language(bus, "en-US")
    ctx = LanguageContext(output_language="zh-CN")
    assert effective_output_language(bus=bus, language_context=ctx) == "en-US"


def test_chain_falls_back_to_context_then_default() -> None:
    ctx = LanguageContext(output_language="en-US")
    assert effective_output_language(bus=_bus(), language_context=ctx) == "en-US"
    # Missing language contexts now use English.
    assert effective_output_language(bus=_bus(), language_context=None) == "en-US"
    assert effective_output_language() == "en-US"


def test_unknown_language_is_not_pinned() -> None:
    """Unsupported language values must never become the authoritative pin."""
    bus = _bus()
    # de-DE and fr-FR are supported now, so use ja-JP as the unsupported sample.
    assert pin_turn_output_language(bus, "ja-JP") == ""
    assert pin_turn_output_language(bus, "auto") == ""
    assert pin_turn_output_language(bus, None) == ""
    assert read_turn_pin(bus) == ""
    # Fall back to the context instead of passing ja-JP through.
    assert effective_output_language(bus=bus, language_context=LanguageContext(output_language="en-US")) == "en-US"


def test_legacy_pin_attr_is_double_written_and_read() -> None:
    """过渡期：别的代码路径可能仍读/写旧名，一次性改名会静默丢语言。"""
    bus = _bus()
    pin_turn_output_language(bus, "en-US")
    assert getattr(bus, PIN_ATTR) == "en-US"
    assert getattr(bus, LEGACY_PIN_ATTR) == "en-US"
    # Read legacy-only pins too (written by another sprint).
    legacy_only = SimpleNamespace(**{LEGACY_PIN_ATTR: "en-US"})
    assert read_turn_pin(legacy_only) == "en-US"


def test_pin_survives_a_duck_bus() -> None:
    """鸭子 bus（测试替身/已关闭的 bus）不该让 turn 崩。"""

    class _Frozen:
        __slots__ = ()

    assert pin_turn_output_language(_Frozen(), "en-US") == ""
    assert pin_turn_output_language(None, "en-US") == ""


def test_silent_done_hints_follow_output_language() -> None:
    """★ 存量 bug 的正对照：silent-done 三条文案此前恒中文（pin 没被写过）。

    钉了 en-US pin 之后必须出英文；没钉过时仍是 zh-CN（zh 轮逐字节不变）。
    """
    from bowhead_core.report_chrome_text import chrome_text

    bus = _bus()
    pin_turn_output_language(bus, "en-US")
    lang = effective_output_language(bus=bus)
    assert chrome_text("turn_empty_hint", lang).startswith("Nothing displayable")
    assert chrome_text("no_report_this_turn", lang) == "No canvas report was produced this turn."

    zh_lang = effective_output_language(bus=_bus(), language_context=LanguageContext(output_language="zh-CN"))
    assert chrome_text("no_report_this_turn", zh_lang) == "本次未生成画布报告。"


def test_no_direct_legacy_pin_reads_outside_the_helper() -> None:
    """防漂移闸：pin 只准经本模块读写，否则下一个 sprint 又会各读各的。

    只扫 `_lead_chrome_lang` 的**直接 getattr/赋值**；本模块自身与测试豁免。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "bowhead_core"
    direct = re.compile(r"(getattr\([^,]+,\s*[\"']_lead_chrome_lang|\._lead_chrome_lang\s*=)")
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "effective_language.py" and direct.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"这些文件绕开了 effective_language 直接读写 pin: {offenders}"


# ─── Carrier-language criterion (scheme B) ──────────────────────
# Positive/negative examples use actual 116 G3 / G4 sentences (dogfood-116-four-cells §1);
# criterion must agree with the two already-correct model judgments, confirming evidence rather than overturning it.

G3_SENTENCE = "帮我看下本月各省的 revenue 和 profit margin trend，出个报告。"
G4_SENTENCE = "Show me this month's revenue and profit margin for 北京市 and 上海市, with a report."


def test_carrier_language_matches_the_two_measured_cells() -> None:
    from bowhead_core.effective_language import carrier_language

    # Chinese structure + English terms → Chinese (terms do not change classification).
    assert carrier_language(G3_SENTENCE) == "zh-CN"
    # ★ English structure + Chinese entities → English: flagship guard for English users with data_language=zh.
    # Choosing the less common language whenever both appear incorrectly selects Chinese for precisely these priority customers.
    assert carrier_language(G4_SENTENCE) == "en-US"


def test_carrier_language_pure_and_empty() -> None:
    from bowhead_core.effective_language import carrier_language

    assert carrier_language("本月各省营业额") == "zh-CN"
    assert carrier_language("Revenue by province this month") == "en-US"
    assert carrier_language("") == ""
    assert carrier_language("2026-08 / 12,345.67 · %") == ""  # Numbers/punctuation only: undetermined; do not force.


def test_carrier_language_tie_break_prefers_non_english() -> None:
    """口径 B：势均力敌时用更小众的那门（v1 = 中文）。"""
    from bowhead_core.effective_language import carrier_language

    balanced = "Q3 revenue 怎么样 trend 稳吗 margin 如何"
    assert carrier_language(balanced) == "zh-CN"
    # Configurable tie_break: v2 German/Japanese/French passes "least global" choice; criterion need not change.
    assert carrier_language(balanced, tie_break="en-US") == "en-US"


def test_carrier_language_entity_insertions_do_not_flip_the_skeleton() -> None:
    """多塞几个中文实体也不该把英文骨架判反（G4 的加强版）。"""
    from bowhead_core.effective_language import carrier_language

    heavy = "This report covers August 2026 revenue and profit margin for 北京市, 上海市, 天津市 and 江苏省, with a per-city breakdown table."
    assert carrier_language(heavy) == "en-US"


# ─── Batch 3: chrome follows body, not settings (C2 consistency) ──


def test_chrome_follows_the_pin_not_the_setting(monkeypatch) -> None:
    """★ G2 的修法本体：设置是英文、模型写出中文正文 ⇒ chrome 必须**跟正文**。

    否则就是实测到的那份报告：h1/KPI/洞察中文，而 meta pills / 来源标签 / caveats
    标题英文，同一段里英标题配中正文。
    """
    from bowhead_core.agents._lead_context import reset_current_output_language, set_current_output_language
    from bowhead_core.report_chrome_text import chrome_lang, chrome_text

    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "1")
    en_setting = LanguageContext(ui_language="en-US", output_language="en-US")
    token = set_current_output_language("zh-CN")  # Model actually wrote Chinese.
    try:
        assert chrome_lang(en_setting) == "zh-CN"
        assert chrome_text("sources_label", chrome_lang(en_setting)) == "数据来源"
    finally:
        reset_current_output_language(token)


def test_chrome_ignores_the_pin_when_flag_off(monkeypatch) -> None:
    """回滚绳：flag 关 ⇒ 逐字节回到"跟设置"。"""
    from bowhead_core.agents._lead_context import reset_current_output_language, set_current_output_language
    from bowhead_core.report_chrome_text import chrome_lang

    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "0")
    en_setting = LanguageContext(ui_language="en-US", output_language="en-US")
    token = set_current_output_language("zh-CN")
    try:
        assert chrome_lang(en_setting) == "en-US"
    finally:
        reset_current_output_language(token)


def test_pin_writes_the_contextvar(monkeypatch) -> None:
    """唯一写点必须同时钉 bus 与 ContextVar —— 否则拿不到 bus 的 chrome 调用点看不见。"""
    from bowhead_core.agents._lead_context import get_current_output_language, reset_current_output_language

    token = None
    try:
        from types import SimpleNamespace

        bus = SimpleNamespace()
        pin_turn_output_language(bus, "en-US")
        assert get_current_output_language() == "en-US"
    finally:
        if token is not None:
            reset_current_output_language(token)


def test_payload_prose_excludes_non_evidence() -> None:
    """喂给判据的只能是散文：数字/表格/chrome 不是语言证据（chrome 更是自证陷阱）。"""
    from bowhead_core.canvas_composer import payload_prose_for_language

    prose = payload_prose_for_language(
        {
            "report_title": "2026年8月各省经营分析",
            "exec_summary": "上海与北京构成第一梯队。",
            "insights": [{"title": "利润率分化", "thesis": "河南最低"}],
            "kpis": [{"label": "Total Revenue", "value": "¥85.19M"}],
            "caveats": ["Data limitations / notes"],
            "sources": [{"label": "Data sources"}],
        }
    )
    assert "Total Revenue" not in prose
    assert "Data sources" not in prose
    assert "Data limitations" not in prose
    from bowhead_core.effective_language import carrier_language

    assert carrier_language(prose) == "zh-CN"


def test_payload_prose_accepts_both_title_keys() -> None:
    """★ 116 G5 实测：写手路的 payload 用 `title`，composer 路用 `report_title`。

    只认一个键 → 少喂一段散文。这条钉的是"两种写法都收"，不是"哪个对"。
    """
    from bowhead_core.canvas_composer import payload_prose_for_language

    writer_shape = {"title": "August 2026 Revenue by Province", "exec_summary": "Revenue reached ¥899M."}
    composer_shape = {"report_title": "2026年8月各省营收", "exec_summary": "营业额 8.99 亿元。"}
    assert "August 2026" in payload_prose_for_language(writer_shape)
    assert "2026年8月各省营收" in payload_prose_for_language(composer_shape)
    # report_title=None must fall back to title (actual G5 shape).
    assert "August" in payload_prose_for_language({"report_title": None, "title": "August recap", "exec_summary": ""})


def test_both_chrome_assembly_paths_repin_the_language() -> None:
    """防漂移：拼 chrome 的**两条路**都必须回钉，少一条就是 G5 那种半英半中。

    116 G5 逮到的正是「composer 回钉了、写手路没有」。这条闸按调用点数钉死，
    第三条 chrome 装配路出现时会红——那时该收口成一个函数，而不是再抄一遍。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "bowhead_core"
    call_sites = [path.name for path in root.glob("*.py") if "pin_turn_output_language(bus, _observed)" in path.read_text(encoding="utf-8")]
    assert sorted(call_sites) == ["canvas_composer.py", "report_writer_loop.py"], call_sites
