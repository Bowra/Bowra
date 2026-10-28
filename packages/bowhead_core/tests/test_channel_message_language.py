"""Channels without browser context follow the incoming message language when nothing is saved.

IM bridges and scheduled jobs call `resolve_turn_language(follow_message_when_unset=True)`.
Web chat keeps `follow_message_when_unset=False`: its interface follows the browser, while
unset answer preferences follow the question as on every surface.
"""

from __future__ import annotations

import pytest
from bowhead_core.language_context import resolve_turn_language

pytestmark = pytest.mark.unit

_TEAM_ZH_DEFAULT = {"version": 1, "defaults": {"output_language": "zh-CN"}}
_TEAM_EN_LOCK = {"version": 1, "defaults": {"output_language": "en-US"}, "locks": {"output_language": True}}


def _channel(message: str, **kwargs):
    return resolve_turn_language(message_text=message, follow_message_when_unset=True, **kwargs)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("上个月各省的销售额是多少？", "zh-CN"),
        ("What was revenue by province last month?", "en-US"),
        ("Quel est le chiffre d'affaires du mois dernier par région ?", "fr-FR"),
    ],
    ids=["zh", "en", "fr"],
)
def test_unset_preferences_follow_the_message_language(message: str, expected: str) -> None:
    ctx = _channel(message)
    assert (ctx.ui_language, ctx.output_language) == (expected, expected)


def test_german_message_is_followed_only_when_german_output_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "Wie hat sich der Umsatz im letzten Monat entwickelt?"
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    assert _channel(message).output_language == "de-DE"
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "0")
    ctx = _channel(message)
    # German interface copy stays available; model output falls back to English.
    assert (ctx.ui_language, ctx.output_language) == ("de-DE", "en-US")


def test_automatic_saved_preferences_still_follow_the_message() -> None:
    ctx = _channel("帮我看下本月的销售趋势", user_settings={"ui_language": "auto", "output_language": "auto"})
    assert (ctx.ui_language, ctx.output_language) == ("zh-CN", "zh-CN")


@pytest.mark.parametrize("message", ["1", "2026", "👍"])
def test_message_without_language_evidence_follows_the_previous_answer(message: str) -> None:
    ctx = _channel(message, session_last_language="zh-CN")
    assert (ctx.ui_language, ctx.output_language) == ("zh-CN", "zh-CN")


def test_message_without_language_evidence_and_no_history_uses_english() -> None:
    ctx = _channel("1")
    assert (ctx.ui_language, ctx.output_language) == ("en-US", "en-US")


def test_message_language_beats_the_previous_answer_language() -> None:
    ctx = _channel("Show revenue by region", session_last_language="zh-CN")
    assert ctx.output_language == "en-US"


def test_saved_answer_preference_beats_the_message() -> None:
    ctx = _channel("上个月各省的销售额是多少？", user_settings={"output_language": "en-US"})
    assert ctx.output_language == "en-US"
    # Interface copy is still unset, so it follows the message.
    assert ctx.ui_language == "zh-CN"


def test_saved_interface_preference_sets_the_interface_but_not_the_answer() -> None:
    # language-policy.md: interface and answer language are independent; an unset answer
    # preference follows the question on every surface, so only the interface stays zh-CN.
    ctx = _channel("What was revenue last month?", user_settings={"ui_language": "zh-CN"})
    assert (ctx.ui_language, ctx.output_language) == ("zh-CN", "en-US")


def test_space_interface_preference_sets_the_interface_but_not_the_answer() -> None:
    ctx = _channel("What was revenue last month?", workspace_settings={"default_ui_language": "zh-CN"})
    assert (ctx.ui_language, ctx.output_language) == ("zh-CN", "en-US")


def test_message_language_beats_the_team_default() -> None:
    # language-policy.md: a concrete team default ranks below the message and the previous answer.
    assert _channel("What was revenue last month?", team_language_policy=_TEAM_ZH_DEFAULT).output_language == "en-US"
    assert _channel("1", team_language_policy=_TEAM_ZH_DEFAULT, session_last_language="fr-FR").output_language == "fr-FR"
    # No evidence and no previous answer: the team default applies.
    assert _channel("1", team_language_policy=_TEAM_ZH_DEFAULT).output_language == "zh-CN"


def test_team_lock_beats_the_message_and_the_saved_preference() -> None:
    ctx = _channel(
        "上个月各省的销售额是多少？",
        user_settings={"output_language": "zh-CN"},
        team_language_policy=_TEAM_EN_LOCK,
    )
    assert ctx.output_language == "en-US"


def test_explicit_follow_input_preference_is_unchanged() -> None:
    ctx = _channel("What was revenue last month?", user_settings={"ui_language": "zh-CN", "output_language": "follow_input"})
    assert (ctx.ui_language, ctx.output_language) == ("zh-CN", "en-US")


def test_web_chat_answers_in_the_question_but_keeps_the_browser_interface() -> None:
    # language-policy.md, AI answers: web answers without a saved preference follow the question.
    ctx = resolve_turn_language(message_text="上个月各省的销售额是多少？", accept_language="en-US,en;q=0.9")
    assert (ctx.ui_language, ctx.output_language) == ("en-US", "zh-CN")


def test_web_follow_input_answers_in_the_question_but_keeps_the_browser_interface() -> None:
    ctx = resolve_turn_language(
        message_text="上个月各省的销售额是多少？",
        accept_language="en-US,en;q=0.9",
        user_settings={"output_language": "follow_input"},
    )
    assert (ctx.ui_language, ctx.output_language) == ("en-US", "zh-CN")


@pytest.mark.parametrize("reply", ["ok", "OK", "yes", "A", "SKU TOP10"])
def test_short_latin_reply_after_a_chinese_answer_stays_chinese(reply: str) -> None:
    ctx = _channel(reply, session_last_language="zh-CN")
    assert (ctx.ui_language, ctx.output_language) == ("zh-CN", "zh-CN")


def test_short_latin_reply_without_history_uses_english() -> None:
    assert _channel("ok").output_language == "en-US"


def test_real_english_sentence_after_a_chinese_answer_switches_to_english() -> None:
    assert _channel("Show revenue by region for last month", session_last_language="zh-CN").output_language == "en-US"


@pytest.mark.parametrize(
    "message",
    ["看下 Salesforce Opportunity pipeline", "用 Tableau dashboard 看 Q3 revenue", "DAU 和 MAU", "帮我看下本月各省的 revenue 和 profit margin trend"],
)
def test_chinese_skeleton_with_english_names_is_chinese(message: str) -> None:
    assert _channel(message, session_last_language="en-US").output_language == "zh-CN"


def test_english_skeleton_with_chinese_entities_is_english() -> None:
    message = "Show me this month's revenue and profit margin for 北京市 and 上海市"
    assert _channel(message, session_last_language="zh-CN").output_language == "en-US"


@pytest.mark.parametrize(
    "message",
    [
        "<https://example.com/the-sales-report-for-this-month|the report for this month> 看一下",
        "<at>Show the sales data for this month</at> 上个月销售额",
        ":this_is_the_way: :what_is_this: 1",
    ],
    ids=["slack-link", "teams-mention", "slack-emoji"],
)
def test_channel_markup_is_not_language_evidence(message: str) -> None:
    assert _channel(message, session_last_language="zh-CN").output_language == "zh-CN"


def test_explicit_follow_input_keeps_the_session_language_on_a_short_reply() -> None:
    ctx = _channel("ok", user_settings={"output_language": "follow_input"}, session_last_language="zh-CN")
    assert ctx.output_language == "zh-CN"


def test_automatic_answer_preference_takes_the_team_default_in_channels() -> None:
    ctx = _channel("👍", user_settings={"output_language": "auto"}, team_language_policy=_TEAM_ZH_DEFAULT)
    assert ctx.output_language == "zh-CN"


def test_automatic_answer_preference_takes_the_team_default_in_web_chat() -> None:
    # Personal auto is unset: the question wins, the team default fills in without evidence.
    kwargs = {"accept_language": "en-US", "user_settings": {"output_language": "auto"}, "team_language_policy": _TEAM_ZH_DEFAULT}
    assert resolve_turn_language(message_text="What was revenue last month?", **kwargs).output_language == "en-US"
    ctx = resolve_turn_language(message_text="1", **kwargs)
    assert (ctx.ui_language, ctx.output_language) == ("en-US", "zh-CN")


def test_concrete_answer_preference_still_beats_the_team_default() -> None:
    ctx = resolve_turn_language(user_settings={"output_language": "fr-FR"}, team_language_policy=_TEAM_ZH_DEFAULT)
    assert ctx.output_language == "fr-FR"
