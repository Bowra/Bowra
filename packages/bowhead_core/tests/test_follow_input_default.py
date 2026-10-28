"""Automatic defaults and explicit question/interface/team language preferences."""

from __future__ import annotations

import pytest
from bowhead_core.language_context import LanguageContext, needs_input_carrier
from bowhead_core.team_language_policy import (
    SOURCE_LOCK,
    SOURCE_NONE,
    SOURCE_TEAM_DEFAULT,
    SOURCE_USER,
    parse_team_language_policy,
)

_TEAM_EN_DEFAULT = '{"version":1,"defaults":{"output_language":"en-US"}}'
_TEAM_EN_LOCKED = '{"version":1,"defaults":{"output_language":"en-US"},"locks":{"output_language":true}}'


def _resolve(**kwargs):
    base = {"user_settings": {"ui_language": "zh-CN"}}
    base.update(kwargs)
    return LanguageContext.resolve(**base)


# Rule (docs/design/language-policy.md, "AI answers"): an unset answer preference
# (missing or personal "auto") follows the question's language, then the session's
# previous answer, then a concrete team default, then the interface language.
# Team locks and saved concrete account choices win over the question.


@pytest.mark.parametrize("unset", [{}, {"output_language": "auto"}])
def test_unset_answer_preference_follows_the_question_language(unset: dict[str, str]) -> None:
    """Missing and personal auto both count as unset and follow the question."""
    ctx = _resolve(user_settings={"ui_language": "zh-CN", **unset}, input_carrier_language="en-US")
    assert ctx.output_language == "en-US"
    ctx = _resolve(user_settings=unset, accept_language="en-US", input_carrier_language="zh-CN")
    assert ctx.output_language == "zh-CN"


def test_explicit_follow_input_setting_follows_the_question_language() -> None:
    ctx = _resolve(
        user_settings={"ui_language": "zh-CN", "output_language": "follow_input"},
        input_carrier_language="en-US",
    )
    assert ctx.output_language == "en-US"


def test_explicit_user_language_still_beats_the_question_language() -> None:
    """A saved concrete answer language is an explicit choice above the question language."""
    ctx = _resolve(
        user_settings={"ui_language": "zh-CN", "output_language": "zh-CN"},
        input_carrier_language="en-US",
    )
    assert ctx.output_language == "zh-CN"


def test_team_lock_beats_the_question_language() -> None:
    ctx = _resolve(
        team_language_policy=parse_team_language_policy(_TEAM_EN_LOCKED),
        input_carrier_language="zh-CN",
    )
    assert ctx.output_language == "en-US"


def test_question_language_beats_the_team_default_for_unset_accounts() -> None:
    """A concrete team default is a fallback for unset accounts, below the question and previous answer."""
    policy = parse_team_language_policy(_TEAM_EN_DEFAULT)
    for user_settings in ({}, {"output_language": "auto"}):
        assert _resolve(user_settings=user_settings, team_language_policy=policy, input_carrier_language="zh-CN").output_language == "zh-CN"
        # No question evidence: the previous answer still ranks above the team default.
        ctx = _resolve(user_settings=user_settings, team_language_policy=policy, input_carrier_language=None, session_last_language="zh-CN")
        assert ctx.output_language == "zh-CN"
        # No evidence and no previous answer: the team default ranks above the interface.
        ctx = _resolve(user_settings=user_settings, accept_language="fr-FR", team_language_policy=policy, input_carrier_language=None)
        assert ctx.output_language == "en-US"


def test_team_default_auto_follows_the_question_then_the_interface() -> None:
    policy = parse_team_language_policy('{"version":1,"defaults":{"output_language":"auto"}}')
    ctx = _resolve(user_settings={}, accept_language="fr-FR", team_language_policy=policy, input_carrier_language="zh-CN")
    assert ctx.output_language == "zh-CN"
    ctx = _resolve(user_settings={}, accept_language="fr-FR", team_language_policy=policy, input_carrier_language=None)
    assert ctx.output_language == "fr-FR"


def test_explicit_follow_input_skips_the_team_default_fallback() -> None:
    policy = parse_team_language_policy(_TEAM_EN_DEFAULT)
    ctx = _resolve(
        user_settings={"output_language": "follow_input"},
        team_language_policy=policy,
        input_carrier_language="zh-CN",
    )
    assert ctx.output_language == "zh-CN"
    # Without evidence, follow_input falls back to the interface, not the team default.
    ctx = _resolve(
        user_settings={"output_language": "follow_input"},
        accept_language="fr-FR",
        team_language_policy=policy,
        input_carrier_language=None,
    )
    assert ctx.output_language == "fr-FR"


# ─────────── No language switch when the question carries no evidence ──────


@pytest.mark.parametrize("output_language", [None, "follow_input"])
def test_undecidable_input_follows_the_previous_turn_not_the_ui(output_language: str | None) -> None:
    """Replies such as "1" have no carrier language; falling back to the UI would switch languages mid-session."""
    settings = {"ui_language": "zh-CN"} | ({"output_language": output_language} if output_language else {})
    ctx = _resolve(user_settings=settings, input_carrier_language=None, session_last_language="en-US")
    assert ctx.output_language == "en-US"


def test_this_turn_carrier_beats_the_previous_turn() -> None:
    """The previous answer only fills in when this question has no evidence."""
    ctx = _resolve(user_settings={"ui_language": "zh-CN"}, input_carrier_language="zh-CN", session_last_language="en-US")
    assert ctx.output_language == "zh-CN"


def test_no_previous_turn_falls_back_to_ui() -> None:
    """First turn without evidence (a number-only opener) uses the interface language."""
    ctx = _resolve(user_settings={"ui_language": "zh-CN"}, input_carrier_language=None, session_last_language=None)
    assert ctx.output_language == "zh-CN"


def test_previous_turn_never_overrides_an_explicit_setting() -> None:
    ctx = _resolve(
        user_settings={"ui_language": "zh-CN", "output_language": "en-US"},
        input_carrier_language=None,
        session_last_language="zh-CN",
    )
    assert ctx.output_language == "en-US"


# ─────────── Two matching sites (batch 6-C half-effective failure) ───────────


@pytest.mark.parametrize(
    ("raw_output", "source"),
    [
        (None, SOURCE_NONE),
        ("auto", SOURCE_TEAM_DEFAULT),
        ("auto", SOURCE_LOCK),
        ("follow_input", SOURCE_USER),
        ("follow_input", SOURCE_TEAM_DEFAULT),
        ("zh-CN", SOURCE_USER),
        ("en-US", SOURCE_LOCK),
        ("en-US", SOURCE_TEAM_DEFAULT),
        ("de-DE", SOURCE_USER),
    ],
)
def test_needs_input_carrier_agrees_with_the_resolver(raw_output: str | None, source: str) -> None:
    """Where `needs_input_carrier` skips detection, the resolver must not use the carrier.

    If the two drift apart, a preference is accepted while the carrier stays None and
    the answer silently falls back to the interface (batch 6-C, team follow_input).
    """
    from bowhead_core.language_context import _resolve_output_language

    # Test several carrier values: a single one can coincide with another layer's fallback.
    outcomes = {
        c: _resolve_output_language(raw_output, source, ui="fr-FR", session_last_language=None, input_carrier_language=c)
        for c in (None, "zh-CN", "en-US")
    }
    carrier_mattered = len(set(outcomes.values())) > 1
    assert carrier_mattered == needs_input_carrier(raw_output, source), (
        f"raw={raw_output} source={source}: needs_input_carrier disagrees with the resolver ({outcomes})"
    )


# ─────────── Batch 7-F: shared entry (same Web / IM behavior) ───────────


def test_resolve_turn_language_is_the_single_entry_for_every_surface() -> None:
    """`resolve_turn_language` binds preference decision, carrier detection and resolution."""
    from bowhead_core.language_context import resolve_turn_language

    # Unset answer preference: the question language wins over the interface.
    ctx = resolve_turn_language(user_settings={"ui_language": "zh-CN"}, message_text="Show me revenue by province")
    assert ctx.output_language == "en-US"
    assert ctx.ui_language == "zh-CN"

    # Explicit setting: the question language does not participate.
    assert (
        resolve_turn_language(
            user_settings={"ui_language": "zh-CN", "output_language": "zh-CN"},
            message_text="Show me revenue by province",
        ).output_language
        == "zh-CN"
    )

    # Undetermined carrier and no previous answer: the interface language.
    assert resolve_turn_language(accept_language="en-US", message_text="1").output_language == "en-US"


@pytest.mark.parametrize(
    ("resume_context", "expected"),
    [
        ({"language_context": {"ui_language": "zh-CN", "output_language": "en-US"}}, ("zh-CN", "en-US")),
        ({"language_context": {"ui_language": "en-US", "output_language": "en-US"}}, ("zh-CN", "zh-CN")),
        ({"language_context": {"ui_language": "xx", "output_language": "en-US"}}, ("zh-CN", "zh-CN")),
        ({}, ("zh-CN", "zh-CN")),
        (None, ("zh-CN", "zh-CN")),
    ],
    ids=["matching", "mismatched-locale", "invalid", "missing-key", "no-context"],
)
def test_resume_language_context_keeps_the_admitted_snapshot(resume_context, expected) -> None:
    """chat-pipeline.md: a same-turn resume keeps the admitted answer language."""
    from bowhead_core.language_context import resume_language_context

    ctx = resume_language_context(resume_context, presentation_locale="zh-CN")
    assert (ctx.ui_language, ctx.output_language) == expected


def test_language_context_snapshot_round_trips() -> None:
    """写侧存快照、读侧还原 —— 澄清 resume 靠这一对保住本轮语言。"""
    from types import SimpleNamespace

    from bowhead_core.language_context import language_context_snapshot

    ctx = LanguageContext(ui_language="zh-CN", output_language="en-US", data_language="zh-CN")
    snap = language_context_snapshot(SimpleNamespace(language_context=ctx))
    assert snap is not None
    assert LanguageContext.from_dict(snap) == ctx
    # No language context (legacy path / i18n-disabled space) → None; caller omits the key.
    assert language_context_snapshot(SimpleNamespace(language_context=None)) is None
    assert language_context_snapshot(SimpleNamespace()) is None


# ─────────── Judge model output: insufficient evidence must not change the pinned language (actual 116 regression) ──


def test_observed_language_keeps_the_pin_for_entity_dense_english() -> None:
    """英文骨架 + 一串中文实体 ⇒ 判"不确定"，让本轮原本定好的语言留着。

    两条都是 116/1.2.50 的真答案（`ws_942fd77aac33`，门店/品类名全中文）。
    旧判据把它们判成 zh-CN ⇒ chrome 会翻成中文，正是批3 要消灭的"一份报告两种语言"。
    """
    from bowhead_core.effective_language import observed_language

    stores = (
        "The top 10 stores by foot traffic in 2026 (Jan–Dec) are: 东湖街 (1,482,309), "
        "南湖花园城 (861,574), 西岸二期 (792,118), 北滨里 (673,945), 湖景公寓 (654,230), "
        "双桥 (618,792), 望湖 (603,481), 翠堤湾 (547,026), 锦城中环 (521,863), and 东方金座 (509,317)."
    )
    categories = (
        "In 2026 (Jan–Dec), the top three 种类大类 by refund rate are 建材设备 (51.7%), "
        "厨房用具 (38.9%), and 厨卫电器 (33.1%), using amount-based refund rate (退款额 ÷ 营业额)."
    )
    assert observed_language(stores) == ""
    assert observed_language(categories) == ""


def test_observed_language_still_flips_on_a_genuinely_chinese_answer() -> None:
    """★ 正对照：把阈值抬高不能把「真的翻成中文了」这件事一起挡掉。

    这条是同一天的真失败样本（汉字 25 / 拉丁 3，加权 62:3 ≈ 20 倍）。
    """
    from bowhead_core.effective_language import observed_language

    zh_answer = "2026年（1–12月）总营业额为 ¥108.7 亿。Top 5 省份：上海 ¥33.9 亿、北京 ¥32.8 亿、江苏 ¥15.2 亿。"
    assert observed_language(zh_answer) == "zh-CN"
    assert observed_language("Revenue totaled 10.9 billion yuan, up 0.08% year over year.") == "en-US"


def test_chrome_repin_keeps_the_sensitive_judge() -> None:
    """chrome 回钉**不用**保守判据 —— 400 轮真数据复核后退回来的（2026-09-08）。

    带内唯一一例真正改变行为的轮次 `t_7722383e74b6`：后端定 en-US、模型交了中文报告、
    比值 1.36×。灵敏判据把 chrome 跟着中文报告翻过去（与用户眼前那份一致）；
    保守判据反而留下"英文外壳 + 中文报告"。⇒ 两个消费点目的与代价不同，判据不能通吃。
    """
    import inspect

    from bowhead_core import canvas_composer, report_writer_loop

    for mod in (canvas_composer, report_writer_loop):
        src = inspect.getsource(mod)
        assert "carrier_language(payload_prose_for_language(payload))" in src, f"{mod.__name__}: chrome 回钉该用灵敏判据"
        assert "observed_language(payload_prose_for_language(payload))" not in src, f"{mod.__name__}: 保守判据不该用在 chrome 回钉"


def test_input_and_output_judges_stay_different() -> None:
    """判提问那把尺子不许被这条阈值带跑偏——G3/G4 的行为必须原样。

    判提问时"不确定"没有安全的默认可退（用户就是没给足信号），判输出时有（后端定过语言）。
    """
    from bowhead_core.effective_language import carrier_language, observed_language

    mixed = "帮我看下本月各省的 revenue 和 profit margin trend，出个报告。"
    assert carrier_language(mixed) == "zh-CN"  # G3: Chinese structure + English terms.
    g4 = "Show me this month's revenue and profit margin for 北京市 and 上海市."
    assert carrier_language(g4) == "en-US"  # G4: English structure + Chinese entities.
    # Same sentence through output detection: no dominant evidence ⇒ abstain, rather than force either side.
    assert observed_language("东湖街 南湖花园城 top stores") == ""


# ─────────── Language consistency instrumentation (observe only, no behavior change) ──


def _fake_bus(answer: str, pinned: str, *, report: bool = True):
    from types import SimpleNamespace

    bus = SimpleNamespace(
        _pi_turn_state={"final_answer": answer},
        _terminal_final_answer_override=None,
        _terminal_report_kept=report,
    )
    from bowhead_core.effective_language import pin_turn_output_language

    pin_turn_output_language(bus, pinned)
    return bus


def test_answer_language_probe_counts_a_real_mismatch(monkeypatch) -> None:
    """本轮定英文、Lead 写中文 ⇒ 记一次 mismatch。"""
    from bowhead_core.agents.lead_orchestrator import _record_answer_language

    from bowhead_core import metrics

    seen: list[dict] = []
    monkeypatch.setattr(metrics, "inc", lambda name, labels=None, **_kw: seen.append({"name": name, **(labels or {})}))
    _record_answer_language(_fake_bus("2026 年各省营业额合计 108.7 亿元，上海居首。", "en-US"), "t1")
    assert seen and seen[-1]["verdict"] == "mismatch"
    assert seen[-1]["pinned"] == "en-US"


def test_answer_language_probe_does_not_cry_wolf_on_entity_dense_english(monkeypatch) -> None:
    """★ 正对照：英文骨架 + 一串中文实体**不是** mismatch。

    灵敏判据会把它判成中文（今天真踩过两次），埋点若用错尺子，报出来的率全是噪声。
    """
    from bowhead_core.agents.lead_orchestrator import _record_answer_language

    from bowhead_core import metrics

    seen: list[dict] = []
    monkeypatch.setattr(metrics, "inc", lambda name, labels=None, **_kw: seen.append({"name": name, **(labels or {})}))
    answer = (
        "The top 10 stores by foot traffic in 2026 are: 东湖街 (1,482,309), 南湖花园城 (861,574), "
        "西岸二期 (792,118), 北滨里 (673,945), 湖景公寓 (654,230), 双桥 (618,792)."
    )
    _record_answer_language(_fake_bus(answer, "en-US"), "t2")
    assert seen[-1]["verdict"] == "undecidable", f"实体密集的英文答案被判成 {seen[-1]['verdict']}"


def test_answer_language_probe_counts_the_denominator(monkeypatch) -> None:
    """一致的轮次也要记 —— 没有分母就算不出率，而算率正是这次埋点的目的。"""
    from bowhead_core.agents.lead_orchestrator import _record_answer_language

    from bowhead_core import metrics

    seen: list[dict] = []
    monkeypatch.setattr(metrics, "inc", lambda name, labels=None, **_kw: seen.append({"name": name, **(labels or {})}))
    _record_answer_language(_fake_bus("Revenue totaled 10.9 billion yuan, up 0.08% year over year.", "en-US"), "t3")
    assert seen[-1]["verdict"] == "match"


def test_answer_language_probe_is_silent_without_an_answer(monkeypatch) -> None:
    """空答案（报错轮/纯画布轮）不记 —— 否则分母里混进没有正文的轮次。"""
    from bowhead_core.agents.lead_orchestrator import _record_answer_language

    from bowhead_core import metrics

    seen: list[dict] = []
    monkeypatch.setattr(metrics, "inc", lambda name, labels=None, **_kw: seen.append({"name": name}))
    _record_answer_language(_fake_bus("   ", "en-US"), "t4")
    assert seen == []
