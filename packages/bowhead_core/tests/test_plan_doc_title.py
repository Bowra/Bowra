"""plan-report-assembly G2 — 文档标题由 Lead 出（决议 #6.6 的真落地）。

背景：决议 #6.6 写的是「标题由 Lead 生成」，但实现落下来是一条**确定性兜底链**
（收口章 title → 聚合 primary title → `"计划执行报告"`）。后果是封面标题必然是
**某一章的标题**——116 真机 `t_c9853b490565` 实测：封面 h1 与第 8 章 h1 逐字相同
（都是「尽调结论与报价建议：估值区间·报价策略·交易结构」），同一句话在一份文档里
出现两次。

这不是「写手」（那条被用户否掉的是让 LLM 重写全文正文，§4.1 有「同一批数字用 7 种
开场白讲 7 遍」的实证）——这里只生成**一行标题**，不碰任何一章的正文。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.plan_doc_title import (
    build_title_messages,
    generate_document_title,
    sanitize_title,
)

pytestmark = pytest.mark.unit


_OBJ = "我在评估收购一家区域商超连锁，请做一份完整尽调。要求分头取数再逐层收敛：① 先并行取三类数据…"


# ── sanitize_title: pure function ───────────────────────────────


def test_sanitize_strips_quotes_and_markdown_heading() -> None:
    """模型爱给标题加书名号/引号/`#`——都不是标题的一部分。"""
    assert sanitize_title("《区域商超连锁收购尽调报告》", objective=_OBJ, lang="zh-CN") == "区域商超连锁收购尽调报告"
    assert sanitize_title("# 区域商超连锁收购尽调报告", objective=_OBJ, lang="zh-CN") == "区域商超连锁收购尽调报告"
    assert sanitize_title('"区域商超连锁收购尽调报告"', objective=_OBJ, lang="zh-CN") == "区域商超连锁收购尽调报告"
    assert sanitize_title("「区域商超连锁收购尽调报告」", objective=_OBJ, lang="zh-CN") == "区域商超连锁收购尽调报告"


def test_sanitize_takes_first_line_only() -> None:
    """模型有时会先给标题再解释一段——只要第一行。"""
    raw = "区域商超连锁收购尽调报告\n\n这个标题概括了……"
    assert sanitize_title(raw, objective=_OBJ, lang="zh-CN") == "区域商超连锁收购尽调报告"


def test_sanitize_rejects_echo_of_the_user_prompt() -> None:
    """★这条是本次改动的**存在理由**：决议 #6.6 定的是「任何情况下不再回落到用户原话」。

    模型完全可能把提问原样吐回来（那正是我们要消灭的旧形态：`plans.title` ＝
    用户原话前 80 字截断，生产 41 张多腿 plan 里 32 张如此）。LLM 说了不算，
    程序上必须再拦一道。
    """
    assert sanitize_title(_OBJ[:30], objective=_OBJ, lang="zh-CN") == ""
    assert sanitize_title("我在评估收购一家区域商超连锁", objective=_OBJ, lang="zh-CN") == ""


def test_sanitize_rejects_overlong_instead_of_truncating() -> None:
    """★宁可返空走兜底，也**不截断**——截在半句话上正是原 bug 的形态。"""
    assert sanitize_title("区" * 200, objective=_OBJ, lang="zh-CN") == ""


def test_sanitize_rejects_empty_and_punctuation_only() -> None:
    for raw in ("", "   ", "\n\n", "。", "《》", "**"):
        assert sanitize_title(raw, objective=_OBJ, lang="zh-CN") == "", raw


def test_sanitize_keeps_a_normal_english_title() -> None:
    """en 轮同样要能出标题（真机英文轮的 plan 收尾散文至今仍是中文，别再添一处）。"""
    out = sanitize_title("Regional Supermarket Chain Acquisition Due Diligence", objective="I'm evaluating the acquisition…", lang="en-US")
    assert out == "Regional Supermarket Chain Acquisition Due Diligence"


# ── build_title_messages: generic prompt with language and anti-copy constraints ──


def test_prompt_carries_chapter_titles_and_forbids_echoing_the_question() -> None:
    """prompt 必须通用（不为某道题特写），且把两条硬约束写进去。"""
    sys_p, user_p = build_title_messages(
        chapter_titles=["行业宏观基准表", "竞品经营对标表", "尽调结论与报价建议"],
        objective=_OBJ,
        lang="zh-CN",
    )
    for t in ("行业宏观基准表", "竞品经营对标表", "尽调结论与报价建议"):
        assert t in user_p
    joined = sys_p + user_p
    assert "照抄" in joined or "不要复述" in joined
    # Title only, no explanation.
    assert "只输出" in joined


def test_prompt_switches_language_explicitly() -> None:
    """★语言必须**显式**传进 prompt。

    同族教训：默认参数藏住漏接线，i18n 只有切语言才看得见。所以
    `build_title_messages` 的 lang 是**必填关键字**，漏传直接 TypeError。
    """
    _, zh = build_title_messages(chapter_titles=["A"], objective="x", lang="zh-CN")
    _, en = build_title_messages(chapter_titles=["A"], objective="x", lang="en-US")
    assert zh != en
    assert "English" in en
    _, fr = build_title_messages(chapter_titles=["A"], objective="x", lang="fr-FR")
    assert "en français" in fr and fr != en
    with pytest.raises(TypeError):
        build_title_messages(chapter_titles=["A"], objective="x")  # type: ignore[call-arg]


# ── generate_document_title: LLM call, fail-open throughout ──────


class _Resp:
    def __init__(self, content: str, tin: int = 120, tout: int = 12) -> None:
        self.content = content
        self.usage = SimpleNamespace(input_tokens=tin, output_tokens=tout, cached_tokens=0)


class _Router:
    def __init__(self, resp: object = None, exc: BaseException | None = None) -> None:
        self._resp, self._exc = resp, exc
        self.calls: list[tuple] = []

    async def invoke(self, agent_type, req):  # noqa: ANN001
        self.calls.append((agent_type, req))
        if self._exc is not None:
            raise self._exc
        return self._resp


async def test_generate_returns_clean_title_and_books_usage() -> None:
    router = _Router(_Resp("《区域商超连锁收购尽调报告》"))
    usage: list[tuple[int, int, int]] = []
    out = await generate_document_title(
        router,
        chapter_titles=["行业宏观基准表", "尽调结论与报价建议"],
        objective=_OBJ,
        lang="zh-CN",
        usage_out=usage,
    )
    assert out == "区域商超连锁收购尽调报告"
    # Writing convergence T4: every new LLM call must be billed; no unaccounted ghost work.
    assert usage == [(120, 12, 0)], usage
    # Compose calls use narrative role, not most expensive lead model (writing convergence T3).
    assert router.calls[0][0] == "narrative"


async def test_generate_is_fail_open_on_llm_error() -> None:
    """★任何故障返空串——调用方据此走既有确定性兜底链，绝不让报告因为标题炸掉。"""
    router = _Router(exc=RuntimeError("boom"))
    assert await generate_document_title(router, chapter_titles=["A"], objective="x", lang="zh-CN") == ""


async def test_generate_is_fail_open_on_timeout() -> None:
    router = _Router(exc=TimeoutError())
    assert await generate_document_title(router, chapter_titles=["A"], objective="x", lang="zh-CN") == ""


async def test_generate_rejects_a_model_echo_of_the_question() -> None:
    """端到端复现最坏情况：模型把用户提问吐回来 —— 必须被拦成空串。"""
    router = _Router(_Resp(_OBJ[:40]))
    assert await generate_document_title(router, chapter_titles=["A"], objective=_OBJ, lang="zh-CN") == ""


async def test_generate_returns_empty_without_chapter_titles() -> None:
    """负对照：没有任何章标题时不调 LLM（没素材，白烧钱）。"""
    router = _Router(_Resp("随便什么"))
    assert await generate_document_title(router, chapter_titles=[], objective="x", lang="zh-CN") == ""
    assert router.calls == []


# ── review §5.10 R-3: language-specific length measure ───────────
#
# Old implementation had one `cap=60` **characters** (calibrated for Chinese 25 characters), but English prompt
# says "Keep it under 10 words" ⇒ compliant English title deemed overlong ⇒ return-empty-rather-than-truncate
# discards it ⇒ deterministic fallback ⇒ cover again has a chapter title, exactly what G2 aims to eliminate.
# Only existing English sample was 51 characters, **just below old gate**, so test never failed.


_EN_OBJ = "I'm evaluating the acquisition of a regional supermarket chain, please run a full due diligence."


def test_sanitize_keeps_prompt_compliant_english_titles() -> None:
    """★这两条在修复前**必红**：都 <10 words，但 63 / 72 字符，被旧的 60 字符闸丢掉。"""
    for raw in (
        "Enterprise Customer Churn Drivers and Retention Strategy Review",  # 8w / 63ch
        "Supply Chain Resilience Assessment for the European Distribution Network",  # 9w / 72ch
        "Regional Supermarket Chain Acquisition Due Diligence Report",  # 7w / 59ch
    ):
        assert sanitize_title(raw, objective=_EN_OBJ, lang="en-US") == raw, raw


def test_sanitize_still_rejects_a_runaway_english_title() -> None:
    """正对照：英文侧的闸没有被拆掉，只是换成了词数口径。"""
    runaway = " ".join(["word"] * 30)
    assert sanitize_title(runaway, objective=_EN_OBJ, lang="en-US") == ""
    # Pathology: one enormous string counts as one word; hard character cap catches it.
    assert sanitize_title("x" * 200, objective=_EN_OBJ, lang="en-US") == ""


def test_sanitize_chinese_cap_unchanged() -> None:
    """正对照：中文口径**一个字没动**（60 字符），别为了修英文把中文放开。"""
    assert sanitize_title("区" * 61, objective=_OBJ, lang="zh-CN") == ""
    assert sanitize_title("区" * 59, objective=_OBJ, lang="zh-CN") == "区" * 59


def test_sanitize_lang_is_required() -> None:
    """`lang` 必填 —— 漏传直接 TypeError，别让「按哪种语言判长度」藏在默认值后面。

    同 `build_title_messages` 的立场（`feedback_default_param_hides_missing_wiring`）。
    """
    with pytest.raises(TypeError):
        sanitize_title("T", objective=_OBJ)  # type: ignore[call-arg]
