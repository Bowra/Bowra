"""Plan 文档标题 —— 决议 #6.6「标题由 Lead 出」的真落地（G2，2026-08-25）。

**为什么需要它**：决议 #6.6 写的是「Lead 用它自己的收尾理解生成标题」，但实现落下来
是一条**确定性兜底链**（收口章 title → 聚合 primary title → `"计划执行报告"`）。
确定性链的天花板就在这儿：任何确定性选择都只能是**某一章的标题**。
116 真机 `t_c9853b490565` 实测后果——封面 h1 与第 8 章 h1 逐字相同
（都是「尽调结论与报价建议：估值区间·报价策略·交易结构」），同一句话在一份文档里
出现两次；而这份文档真正该叫的是「区域商超连锁收购尽调报告」这类名字。

**这不是被否掉的「写手」**。用户 2026-08-24 拍板「不引入强制收敛写手」，否的是
「让 LLM 重写全文正文」（§4.1 实证：写手把同一批数字用 7 种开场白讲 7 遍）。
本模块只生成**一行标题**：输入是各章标题 + 用户诉求，输出十几个字，
**不碰任何一章的正文**，也不改任何 payload 字段。

设计要点：
- **全程 fail-open**：LLM 故障/超时/输出不可用 → 返 `""`，调用方走既有确定性兜底链。
  标题生成失败绝不能让一份已经跑了十几分钟的报告出不来。
- **反照抄闸是承重的**：决议 #6.6 定死「任何情况下不再回落到用户原话」。
  prompt 里说了不算——`sanitize_title` 程序上再拦一道（模型完全可能把提问吐回来，
  那正是要消灭的旧形态：`plans.title` ＝用户原话前 80 字截断，生产 41/32 张如此）。
- **宁可返空也不截断**：截在半句话上就是原 bug 的形态本身。
- **语言必填**：`lang` 是必填关键字，漏传直接 TypeError —— 同族教训是
  「默认参数藏住漏接线，i18n 只有切语言才看得见」。
- **走 `narrative` 角色**：组稿类调用不吃 lead 最贵模型（写作收敛 T3 的既有口径）。
- **必须记账**：`usage_out` 回传给调用方折进 turn 账（写作收敛 T4：别再造「编外幽灵工」）。
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import anyio

from bowhead_router import LLMRequest, Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_router import Router

logger = logging.getLogger(__name__)

# Title length limit. Overlong returns empty for fallback; truncation was the original bug, do not repeat it.
# ★Two distinct measures (review §5.10 R-3): Chinese prompt says "within 25 characters" ⇒ 60 characters allows margin;
# English prompt says "<10 words" ⇒ a 60-character cap rejects compliant titles. See `_is_overlong`.
_TITLE_CAP = 60  # Non-Latin (mainly Chinese): characters.
_TITLE_WORD_CAP_LATIN = 12  # Latin: words (prompt <10, 2-word margin).
_TITLE_CAP_LATIN = 140  # Latin: hard character cap, only to reject one huge string counted as one word.
# Objective prefix length that counts as copying. 12 Chinese characters suffice to detect
# a restated question opening; shorter would reject normal titles (e.g. "商超收购尽调报告").
# ⚠ This is also calibrated for Chinese (12 English characters ≈2 words). Unlike `_TITLE_CAP`,
# English false positives **have not yet been observed** (`startswith` is case-sensitive; 12 characters must match exactly),
# so leave it for now, remembering the same class of risk.
_ECHO_PROBE = 12
_CHAPTER_CAP = 24  # Maximum chapter titles supplied.
_CHAPTER_CHARS = 60
_OBJECTIVE_CHARS = 400

_STRIP_CHARS = " \t\r\n\"'`*#《》「」『』“”‘’()（）【】[]"


def plan_doc_title_enabled() -> bool:
    """Flag `BOWHEAD_PLAN_DOC_TITLE` —— **默认 ON**，`=0` 回滚到纯确定性兜底链。

    小用户基数不做灰度（既有口径）；这是回滚绳不是灰度闸。
    """
    return os.environ.get("BOWHEAD_PLAN_DOC_TITLE", "1").strip() != "0"


def _is_overlong(text: str, *, lang: str) -> bool:
    """长度闸 —— **按语言取口径**（review §5.10 R-3）。

    原实现只有一个 `cap=60` **字符**，配的注释是「中文 25 字」。但 en 的 prompt 给的
    约束是 "Keep it under 10 words"，10 个英文词轻松超过 60 字符 ⇒ 完全**合规**的英文
    标题被判过长 ⇒ 按「宁可返空也不截断」的策略被丢弃 ⇒ 调用方回落到确定性链 ⇒
    封面又变成某一章的标题，**正是 G2 要消灭的形态**。实测（都 <10 words）：
    `Enterprise Customer Churn Drivers and Retention Strategy Review`（8 词 63 字符）、
    `Supply Chain Resilience Assessment for the European Distribution Network`
    （9 词 72 字符）——两条都被旧闸丢掉。

    ⇒ 拉丁文按**词数**判（prompt 说 <10，闸取 12 留余量），另加一道字符硬顶挡住
    「一个超长串算一个词」这种病态输出；中文仍按字符判。
    同族教训：`feedback_default_param_hides_missing_wiring`——只有切语言才看得见。
    """
    if str(lang).lower().startswith(("en", "de", "fr")):
        return len(text.split()) > _TITLE_WORD_CAP_LATIN or len(text) > _TITLE_CAP_LATIN
    return len(text) > _TITLE_CAP


def sanitize_title(raw: str, *, objective: str, lang: str) -> str:
    """模型原始输出 → 可用的文档标题；不可用一律返 `""`（让调用方走兜底链）。

    拦四类：空/纯标点、带书名号引号 `#` 的包装、多行解释、**照抄用户提问**、过长。

    `lang` 必填（同 `build_title_messages`）：长度闸是**按语言取口径**的，漏传就会
    拿中文的字符上限去卡英文标题——那正是 R-3 那个 bug。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # First line only: model often returns title plus explanation.
    text = text.split("\n", 1)[0]
    # Common wrappers: `# `, book-title brackets, quotes, Markdown bold.
    text = text.strip(_STRIP_CHARS)
    # Trailing periods and similar punctuation add nothing.
    text = text.rstrip("。.!！?？:：;；,，").strip()
    if not text:
        return ""
    if _is_overlong(text, lang=lang):
        # ★Return empty for fallback rather than truncate: cutting mid-sentence is precisely the bug being fixed.
        logger.info("plan_doc_title.rejected_overlong lang=%s len=%d words=%d", lang, len(text), len(text.split()))
        return ""
    # ★Anti-copy gate (decision #6.6: never fall back to original user wording).
    obj = str(objective or "").strip()
    if obj:
        probe = text[:_ECHO_PROBE]
        if probe and (obj.startswith(probe) or text in obj):
            logger.info("plan_doc_title.rejected_echo title=%r", text[:40])
            return ""
    return text


def build_title_messages(*, chapter_titles: Sequence[str], objective: str, lang: str) -> tuple[str, str]:
    """(system, user) —— **通用 prompt**，不为任何一道题特写。

    `lang` 必填：漏传直接 TypeError，别让 i18n 漏接线藏在默认值后面。
    """
    normalized_lang = str(lang).lower()
    en = normalized_lang.startswith("en")
    de = normalized_lang.startswith("de")
    fr = normalized_lang.startswith("fr")
    chapters = [str(t).strip()[:_CHAPTER_CHARS] for t in (chapter_titles or ()) if str(t).strip()][:_CHAPTER_CAP]
    chapter_lines = "\n".join(f"- {c}" for c in chapters)
    obj = str(objective or "").strip()[:_OBJECTIVE_CHARS]

    if en:
        system = (
            "You name documents. Given a report's chapter headings and the request that produced it, "
            "return one short title for the whole document.\n"
            "Rules: output ONLY the title text — no quotes, no markdown, no trailing punctuation, "
            "no explanation. Keep it under 10 words. Name the document as a whole; do NOT reuse any "
            "single chapter heading verbatim, and do NOT echo or paraphrase the request back."
        )
        # ★Language needs an **explicit instruction**, not merely an English prompt: actual English turn 116 had
        # **Chinese** planner step objective / deliverable (UI chrome already English),
        # hence often Chinese chapter titles; without explicit instruction, title follows material language.
        user = (
            f"Chapter headings:\n{chapter_lines}\n\nThe request that produced it:\n{obj}\n\n"
            "Write the title in English, even if the chapter headings above are in another language.\n"
            "Title:"
        )
    elif de:
        system = (
            "Sie benennen Dokumente. Erstellen Sie aus den Kapitelüberschriften eines Berichts und dem "
            "Anlass des Berichts einen kurzen Titel für das gesamte Dokument.\n"
            "Regeln: Geben Sie NUR den Titel aus – ohne Anführungszeichen, Markdown, Satzzeichen am Ende "
            "oder Erklärung. Halten Sie ihn unter 10 Wörtern. Benennen Sie das gesamte Dokument; übernehmen "
            "Sie NICHT wortgetreu eine einzelne Kapitelüberschrift und wiederholen oder paraphrasieren Sie NICHT die Anfrage."
        )
        user = (
            f"Kapitelüberschriften:\n{chapter_lines}\n\nAnlass des Berichts:\n{obj}\n\n"
            "Schreiben Sie den Titel auf Deutsch, auch wenn die Kapitelüberschriften oben in einer anderen Sprache sind.\n"
            "Titel:"
        )
    elif fr:
        system = (
            "Vous nommez des documents. À partir des titres de chapitres d'un rapport et de la demande qui l'a "
            "produit, proposez un titre court pour l'ensemble du document.\n"
            "Règles : n'écrivez QUE le titre — sans guillemets, sans markdown, sans ponctuation finale ni "
            "explication. Moins de 10 mots. Nommez le document dans son ensemble ; ne reprenez PAS mot pour mot "
            "un titre de chapitre et ne répétez ni ne paraphrasez PAS la demande."
        )
        user = (
            f"Titres des chapitres :\n{chapter_lines}\n\nDemande à l'origine du rapport :\n{obj}\n\n"
            "Rédigez le titre en français, même si les titres de chapitres ci-dessus sont dans une autre langue.\n"
            "Titre :"
        )
    else:
        system = (
            "你负责给文档起名字。给你一份报告的各章标题和它的来源诉求，返回**整份文档**的标题。\n"
            "规则：**只输出标题本身**——不要引号书名号、不要 markdown、结尾不要标点、不要任何解释。"
            "控制在 25 字以内。命名的是整份文档，**不要照抄任何一章的标题**，"
            "也**不要复述用户的提问**。"
        )
        user = f"各章标题：\n{chapter_lines}\n\n来源诉求：\n{obj}\n\n用中文写这个标题。\n标题："

    return system, user


async def generate_document_title(
    router: Router,
    *,
    chapter_titles: Sequence[str],
    objective: str,
    lang: str,
    agent_type: str = "narrative",
    trace_id: str = "",
    timeout_s: float = 20.0,
    usage_out: list[tuple[int, int, int]] | None = None,
) -> str:
    """各章标题 → 一行文档标题。**任何故障返 `""`**，调用方走确定性兜底链。"""
    chapters = [str(t).strip() for t in (chapter_titles or ()) if str(t).strip()]
    if not chapters:
        # No chapter titles = no material; do not waste a call.
        return ""
    system, user = build_title_messages(chapter_titles=chapters, objective=objective, lang=lang)
    try:
        with anyio.fail_after(timeout_s):
            resp = await router.invoke(
                agent_type,
                LLMRequest(
                    model="",
                    messages=[Message(role="user", content=user)],
                    system=system,
                    temperature=0.3,
                    max_output_tokens=80,
                    trace_id=f"plan_doc_title:{trace_id}",
                ),
            )
    except TimeoutError:
        logger.warning("plan_doc_title.timeout trace=%s timeout_s=%s", trace_id, timeout_s)
        return ""
    except Exception as e:  # noqa: BLE001 — fail-open: title failure must not crash report
        logger.warning("plan_doc_title.llm_failed trace=%s err=%s", trace_id, e)
        return ""
    if usage_out is not None:
        _u: Any = getattr(resp, "usage", None)
        if _u is not None:
            usage_out.append(
                (
                    int(getattr(_u, "input_tokens", 0) or 0),
                    int(getattr(_u, "output_tokens", 0) or 0),
                    int(getattr(_u, "cached_tokens", 0) or 0),
                )
            )
    title = sanitize_title(getattr(resp, "content", "") or "", objective=objective, lang=lang)
    if title:
        logger.info("plan_doc_title.ok trace=%s title=%r", trace_id, title[:40])
    return title
