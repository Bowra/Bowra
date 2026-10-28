"""第一层粗筛（B 线批 1a ＝ 记忆二期 FR5 ①）。

★ 本文件的断言串**全部来自 116 真实语料**（2087 条，2026-09-08 空跑）。
不用自编串 —— 仓内先例：35 条自编串空跑 0 误判，真数据一片误杀。
"""

from __future__ import annotations

from bowhead_core.memory_capture_trigger import (
    DECISION_LANGUAGE_SETTING,
    DECISION_NONE,
    DECISION_PERSISTENT,
    classify_user_message,
)


def _d(text: str) -> str:
    return classify_user_message(text)[0]


def test_real_long_term_instructions_are_let_through():
    """真语料里确凿的长期指令，一条都不能漏。"""
    for text in (
        "记一下：以后凡是排名的图，都画成横着的条，别用竖着的柱子。",
        "记住我的一个长期偏好：做品牌对比时，默认只看最近 12 个月的数据。",
        "Please remember: when comparing brands, always show year-over-year growth alongside absolute values",
        "以后我提head to head，我希望对比同环比，我 vs 竞争对手在占比上的变化，这很重要需要你记住",
    ):
        assert _d(text) == DECISION_PERSISTENT, text


def test_the_language_family_is_decided_first():
    """★ 顺序即正确性：这些句子**同时**命中长期族（「以后都」「记住」），
    但它们是**设置**不是记忆 —— 写进记忆下次会话根本不生效
    （写入闸第一闸拒的就是这个）。语言族必须先判。

    ★ 突变验证：把 `classify_user_message` 里语言族那个 `if` 挪到长期族之后
    ⇒ 本用例红（它们会被判成 persistent_instruction）。
    """
    for text in (
        "记住我的偏好：以后都用英文回答我。",
        "改回来，以后用中文回答我。",
        "以后你回答都用英文吧",
    ):
        assert _d(text) == DECISION_LANGUAGE_SETTING, text


def test_one_off_wording_is_not_a_long_term_instruction():
    """一次性族其次判：本轮范围的话不该触发自动记录。

    ★ 突变验证：删掉 `one_off_markers` 那个 `if` ⇒ 第二条（同时含「以后都」和
    「这次」的矛盾句）会被判成 persistent，本用例红。
    """
    assert _d("这次先只看华东") == DECISION_NONE
    # Contradictory semantics ⇒ automatic writes have side effects; doing nothing when ambiguous is correct.
    assert _d("以后都要详细一点，不过这次先简单说") == DECISION_NONE


def test_the_business_noun_changqi_does_not_fire():
    """★ 真数据逮到的最大误报源：18 条命中里「长期」一个人贡献 6 条，
    **全是同一句业务查询**。它在中文商业语境里是名词（长期待确认 / 长期资产），
    不是时间跨度。

    ★ 突变验证：把 `长期` 加回 `_PERSISTENT_MARKERS` ⇒ 本用例红。
    这条串验不出来的话就会重蹈覆辙 —— 它正是自编串写不出的那种句子。
    """
    assert _d("今年帮我看下短期已解决但是长期待确认的订单数量有多少") == DECISION_NONE
    assert _d("帮我看下短期已解决但是长期待确认的订单数量有多少") == DECISION_NONE


def test_in_turn_corrections_do_not_fire():
    """★ 同样来自真数据：「别再 / 不要再」抓到的是**本轮纠正**不是长期指令。

    ★ 突变验证：把 `别再` / `不要再` 加回词表 ⇒ 本用例红。
    真正的长期否定形态由 `以后别` / `以后不要` 覆盖 —— 下一条就是它的正对照。
    """
    assert _d("刚才那份报告里，利润率最低的三家门店分别是哪几家？简单列一下就行，别再出报告了") == DECISION_NONE
    assert _d("写一份最近90天金银珠宝品类的量价拆解分析报告。口径已定，不要再问我") == DECISION_NONE
    # Positive control: genuine persistent negatives must still match; otherwise the negatives merely trade away recall.
    assert _d("以后别用竖向柱状图了") == DECISION_PERSISTENT


def test_ordinary_questions_are_the_overwhelming_majority():
    """负样本：普通提问一条都不该触发（真语料里 98.3% 落这一档）。"""
    for text in (
        "帮我看下本月各省的营业额和利润率，出个报告。",
        "2026年各品牌营业额排名，出一张图。",
        "北京今天天气怎么样",
        "",
    ):
        assert _d(text) == DECISION_NONE, text


def test_a_temporary_instruction_is_not_captured_as_permanent():
    """★★ 2026-09-08 收口 review 逮到的具体反例：`记住，暂时都用横向条形图`。

    `记住` 命中长期族，而写入闸的 `_ONE_OFF_MARKERS` **认不出 `暂时`** ——
    那份词表被朝相反方向调过：对闸而言漏认一条一次性只是「多存一条」，
    误杀一条真偏好才是重伤，所以它刻意删掉了 `暂时`/`这段` 这类模糊词。
    但在本层，漏认一条一次性 = **零点击写一条永久记忆**，代价反过来了。

    ⇒ 共用实现、**不共用调参**：本层另有 `_TEMPORARY_MARKERS`。

    ★ 突变验证：把 `or temporary_markers(body)` 去掉 ⇒ 本用例红。
    """
    assert _d("记住，暂时都用横向条形图") == DECISION_NONE
    assert _d("记住，这段时间都用横向条形图") == DECISION_NONE
    assert _d("以后都用横向条形图，先这样") == DECISION_NONE
    # Positive control: genuine persistence must still match; otherwise the negatives merely trade away recall.
    assert _d("记一下：以后排名图都用横向条形图") == DECISION_PERSISTENT
