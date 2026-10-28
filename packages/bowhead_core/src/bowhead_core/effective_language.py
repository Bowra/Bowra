"""本轮生效语言（turn-scoped）—— 多语言 sprint 批1 的结构底座。

设计见 `docs/sprints/multilingual-2026-08-28/tech-design.md` §3。

## 为什么要有这个模块

语言此前被**六处各自裁决**、各读各的源，于是「一份报告里两种语言」是结构必然
（116 实测 G2：h1/KPI/洞察全中文，而 meta pills/来源标签/步骤行全英文）。
本模块把「本轮生效语言」收成 **一个写点 + N 个读点**：谁都不许再自己算一遍。

## pin 不是新东西，是升格

`bus._lead_chrome_lang` 从 chrome-i18n（2026-08-18）起就已经是 turn 级 pin，只是：

- **写得晚且有条件** —— 主写点在 chat final 组装处，plan 路那个写点还要
  `_pv2() and _claims_canvas_pointer(text)` 才写；
- **只服务 chrome 一族**。

⇒ 批1 做两件事：(a) 在 **turn 头**无条件写一次（与 `set_current_ui_language` 同生命周期），
(b) 所有读点改走 `effective_output_language`。

## ⚠ 这不是纯重构：它修掉一个存量 bug

turn 头补写之前，`no_report_this_turn` / `turn_failed_hint` / `turn_empty_hint`
这三条**恰恰跑在 pin 没被写过的路径上**（silent-done：本轮没产出/报错，chat final
组装根本没走到）⇒ 它们一律落 `"zh-CN"` 兜底。**英文用户的失败提示今天是中文的**，
而这正是 chrome-i18n 当初要消灭的那一类。批1 之后它们跟 `output_language`。

- zh-CN 轮：逐字节不变（兜底本来就是 zh-CN）。
- en-US 轮：失败/空轮文案由中文变英文 —— **期望内的修复**，有正对照钉住。
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from bowhead_core.language_context import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, german_ai_output_enabled

# New pin attribute (sole source of truth from this sprint).
PIN_ATTR = "_turn_output_language"
# Legacy chrome-i18n name. Transitional **dual write/read**: other sprints may use the old name;
# immediate rename silently loses language in rebase windows (`bus` is duck-typed, without attribute checks).
LEGACY_PIN_ATTR = "_lead_chrome_lang"


def _normalize(raw: Any) -> str:
    """只认受支持语言；其余（含 None / "auto"）→ ""，交给上层继续回落。"""
    value = str(raw or "").strip()
    return value if value in SUPPORTED_LANGUAGES else ""


def pin_turn_output_language(bus: Any, language: Any) -> str:
    """把本轮生效语言钉到 bus（唯一写点）。返回真正钉进去的值。

    未知语言不钉（返回 ""）—— 宁可让读点回落到 `language_context`，也不要把一个
    locale 表里没有的值钉成"真相"（`chrome_text` 会静默落 zh-CN 表，看起来像没坏）。
    """
    normalized = _normalize(language)
    if not normalized or bus is None:
        return ""
    try:
        setattr(bus, PIN_ATTR, normalized)
        # Synchronize legacy name during transition: other code paths still read it (plan path / other sprints'
        # in-flight changes). Remove only once all readers use this module, together with tests.
        setattr(bus, LEGACY_PIN_ATTR, normalized)
    except Exception:  # noqa: BLE001 — duck-typed buses (test doubles/closed buses) must not crash turns.
        return ""
    # Batch 3: pin ContextVar too; a dozen `chrome_lang` call sites lack bus (only
    # `request`/`ctx`), so ContextVar ensures "one language per turn" for every chrome string.
    with contextlib.suppress(Exception):
        from bowhead_core.agents._lead_context import set_current_output_language

        set_current_output_language(normalized)
    return normalized


def read_turn_pin(bus: Any) -> str:
    """只读 pin（新名优先，回落旧名）。没钉过 → ""。"""
    if bus is None:
        return ""
    return _normalize(getattr(bus, PIN_ATTR, None)) or _normalize(getattr(bus, LEGACY_PIN_ATTR, None))


def effective_output_language(
    *,
    bus: Any = None,
    language_context: Any = None,
) -> str:
    """本轮生效语言 = turn pin > `language_context.output_language` > zh-CN。

    **所有用户可见输出的语言判定只准走这个函数**（步骤行那条 `ui_language` 轴除外，
    见 tech-design C1：步骤行属于操作界面，不是交付物）。

    Args:
        bus: turn 事件总线（pin 的载体）。None 或没钉过 → 跳过这一层。
        language_context: 本轮 `LanguageContext`（None = 老路径/flag 关）。
    """
    return read_turn_pin(bus) or _normalize(getattr(language_context, "output_language", None)) or DEFAULT_LANGUAGE


__all__ = [
    "LEGACY_PIN_ATTR",
    "PIN_ATTR",
    "carrier_language",
    "channel_message_language",
    "latin_language",
    "observed_language_of_markdown",
    "observed_language",
    "effective_output_language",
    "language_decision_v2_enabled",
    "pin_turn_output_language",
    "read_turn_pin",
    "session_follow_language_of_markdown",
    "strip_channel_markup",
]


def language_decision_v2_enabled() -> bool:
    """Master switch for multilingual sprint behavior (default on since 2026-10-14; 0 disables).

    批1 不需要它（那是纯接线 + 一个存量 bug 修复）；从批2 起每一个**模型面**的行为
    变更都挂在它下面，关时逐字节回到今天。灰度按既有纪律：116 单空间 ≥24h 再全量。
    """
    import os

    return os.environ.get("FEATURE_LANGUAGE_DECISION_V2", "1").strip().lower() in {"1", "true", "yes", "on"}


# ─── Carrier-language criterion (scheme B / tech-design §4.2, §4.4 share implementation) ──
#
# Judge **sentence structure**, not embedded words. Two positive/negative 116 cases (dogfood-116-four-cells §1):
#   G3 "帮我看下本月各省的 revenue 和 profit margin trend，出个报告。" → Chinese carrier.
#   G4 "Show me this month's revenue and profit margin for 北京市 and 上海市…" → English carrier.
# Model **already judged these correctly**; this formalizes existing behavior as deterministic criteria, not new constraints.
#
# ⚠ No "entity stripping": identifying entities requires DB/dictionary lookup, making the criterion
# depend on external state; wrongly stripping one ordinary word can reverse classification. Instead **weight**:
# ~2.5 Latin letters per Han character (average English word length), so "English structure + a few Chinese entities" stays English,
# and "Chinese structure + a few English terms" stays Chinese; dominant text outweighs insertions without identifying them.
_LATIN_PER_CJK = 2.5
# Tie band: differences within this ratio ⇒ scheme B tie-break (prefer non-English).
_TIE_BAND = 1.25


_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)


def _script_counts(text: str) -> tuple[int, int]:
    """(汉字数, 拉丁字母数)。两个判据共用一处计数，别各数各的。"""
    cjk = 0
    latin = 0
    for ch in text:
        if "一" <= ch <= "鿿":
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    return cjk, latin


def carrier_language(text: str, *, tie_break: str = "zh-CN") -> str:
    """一段**用户输入**的载体语言；判不出来（无字母无汉字）→ ""。

    Args:
        text: 原文。调用方负责先剥掉代码块 —— 代码里的英文标识符不是语言证据。
        tie_break: 势均力敌时取谁。口径 B：**非英语的那门赢**（英语是通用语，
            混排时用户母语更可能是另一门），v1 取值域只有中英 ⇒ 默认 zh-CN。

    ⚠ **这个函数是为「判用户提问」设计的**，G3/G4 真机验过。判**模型输出**请用
    `observed_language` —— 两者对"证据不足"的处理必须不同，理由见那边的 docstring。
    """
    if not text:
        return ""
    cjk, latin = _script_counts(text)
    if cjk == 0 and latin == 0:
        return ""
    if cjk == 0:
        return "en-US"
    if latin == 0:
        return "zh-CN"
    zh_weight = cjk * _LATIN_PER_CJK
    if zh_weight > latin * _TIE_BAND:
        return "zh-CN"
    if latin > zh_weight * _TIE_BAND:
        return "en-US"
    return tie_break


# ─── Distinguish English / German / French in Latin questions (**user input** only) ──
#
# No language-detection library in repo (no lingua / langdetect / fasttext dependency); `observed_language`
# and `carrier_language` only distinguish Chinese/English. Avoid new dependency; use a small **function-word + analytical-term** list:
# - Only ask whether Latin text is German / French: one of three, not general language detection.
# - Libraries are unreliable for one/two-word questions (lingua itself notes low short-text confidence),
#   the most common inputs here; word-list criteria are readable, testable, deterministic, and add no image size.
# - Wrong judgments have fallback: uncertainty preserves prior behavior (German/French UI language, otherwise English).
#
# Avoid false positives: count only **language-exclusive** words (cross-language homographs `in` / `die` / `was` / `des` / `est` / `me` / `total`
# are excluded); diacritic-bearing words count half, so French/German proper names
# (Société Générale, Müller) in English do not flip the sentence. Ignore capitalized words
# (products, metrics, people), except sentence-initial words. Chinese bypasses this (unchanged `carrier_language`).
_LATIN_WORDS: dict[str, frozenset[str]] = {
    "en-US": frozenset(
        [
            "the",
            "and",
            "of",
            "to",
            "for",
            "with",
            "what",
            "which",
            "who",
            "how",
            "why",
            "when",
            "where",
            "is",
            "are",
            "were",
            "does",
            "did",
            "do",
            "show",
            "give",
            "list",
            "my",
            "our",
            "this",
            "that",
            "these",
            "those",
            "last",
            "next",
            "by",
            "from",
            "than",
            "sales",
            "revenue",
            "growth",
            "month",
            "year",
            "quarter",
            "week",
            "trend",
            "please",
            "can",
            "could",
            "would",
            "should",
            "between",
            "each",
            "per",
            "over",
            "about",
            "into",
            "it",
            "its",
            "be",
            "been",
            "has",
            "have",
            "had",
        ]
    ),
    "de-DE": frozenset(
        [
            "der",
            "dem",
            "den",
            "das",
            "und",
            "ist",
            "sind",
            "wie",
            "welche",
            "welcher",
            "welches",
            "wer",
            "wo",
            "warum",
            "wann",
            "nicht",
            "für",
            "mit",
            "auf",
            "aus",
            "bei",
            "nach",
            "von",
            "vom",
            "zum",
            "zur",
            "im",
            "ein",
            "eine",
            "einen",
            "einem",
            "einer",
            "bitte",
            "zeige",
            "zeig",
            "zeigen",
            "gib",
            "vergleiche",
            "wurde",
            "wurden",
            "hat",
            "haben",
            "gibt",
            "es",
            "letzten",
            "letzte",
            "diesem",
            "dieser",
            "diese",
            "dieses",
            "umsatz",
            "monat",
            "monats",
            "jahr",
            "jahres",
            "quartal",
            "woche",
            "wachstum",
            "entwicklung",
            "vergleich",
            "gegenüber",
            "über",
            "auch",
            "noch",
            "schon",
        ]
    ),
    "fr-FR": frozenset(
        [
            "le",
            "les",
            "du",
            "une",
            "un",
            "et",
            "sont",
            "quel",
            "quelle",
            "quels",
            "quelles",
            "qui",
            "que",
            "quoi",
            "comment",
            "pourquoi",
            "quand",
            "où",
            "pour",
            "avec",
            "sur",
            "dans",
            "ce",
            "cette",
            "ces",
            "mon",
            "ma",
            "mes",
            "nos",
            "notre",
            "votre",
            "vos",
            "pas",
            "ne",
            "montre",
            "montrez",
            "donne",
            "donnez",
            "comparez",
            "affiche",
            "affichez",
            "peux",
            "pouvez",
            "dernier",
            "dernière",
            "derniers",
            "mois",
            "année",
            "trimestre",
            "semaine",
            "ventes",
            "chiffre",
            "affaires",
            "croissance",
            "évolution",
            "tendance",
            "entre",
            "chaque",
            "au",
            "aux",
            "par",
            "sans",
            "très",
            "merci",
        ]
    ),
}
_ELISION_FR_RE = re.compile(r"\b(?:l|d|qu|c|n|j|s)['’]\w", re.I)
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_DE_MARKS = set("äöüß")
_FR_MARKS = set("éèêàçùâîôûëïœ")
_CONFIDENT_MIN = 2.0
_CONFIDENT_RATIO = 2.0


# At least this many letters, and this uppercase share ⇒ all-caps writing; lowercase sentence before scoring,
# or every word is skipped as a proper noun, losing all language evidence.
_ALL_CAPS_MIN_LETTERS = 6
_ALL_CAPS_RATIO = 0.8


def _is_mostly_upper(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < _ALL_CAPS_MIN_LETTERS:
        return False
    return sum(ch.isupper() for ch in letters) >= _ALL_CAPS_RATIO * len(letters)


def _latin_language_scores(text: str) -> dict[str, float]:
    scores = dict.fromkeys(_LATIN_WORDS, 0.0)
    if _is_mostly_upper(text):
        text = text.lower()
    for index, match in enumerate(_WORD_RE.finditer(text)):
        raw = match.group(0)
        if index > 0 and raw[:1].isupper():
            continue  # Proper/product/metric name: no language evidence.
        word = raw.lower().replace("’", "'")
        for lang, words in _LATIN_WORDS.items():
            if word in words:
                scores[lang] += 1.0
        chars = set(word)
        if chars & _DE_MARKS:
            scores["de-DE"] += 0.5
        if chars & _FR_MARKS:
            scores["fr-FR"] += 0.5
    scores["fr-FR"] += 0.5 * len(_ELISION_FR_RE.findall(text))
    return scores


def latin_language(text: str) -> str:
    """一段拉丁字母为主的**用户输入**有把握是哪门语言；拿不准 → ""。

    有把握 = 最高分 ≥ 2 且至少是第二名的 2 倍。一两个词的短句通常拿不准，
    交给调用方按界面语言兜底。
    """
    return _confident_latin_language(text, min_score=_CONFIDENT_MIN, min_ratio=_CONFIDENT_RATIO)


def _confident_latin_language(text: str, *, min_score: float, min_ratio: float) -> str:
    """Best-scoring Latin-script language when it clears both bars; otherwise ""."""
    scores = _latin_language_scores(text)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    (best, top), (_second, runner_up) = ranked[0], ranked[1]
    if top >= min_score and top >= min_ratio * runner_up:
        return best
    return ""


# Channel markup that carries no language: Teams mentions, Slack/Teams angle-bracket
# markup (links, user/channel mentions), and Slack emoji shortcodes.
_TEAMS_AT_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.I | re.S)
_ANGLE_MARKUP_RE = re.compile(r"<[^<>\n]*>")
_EMOJI_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:")


def strip_channel_markup(text: str) -> str:
    """Remove IM markup and code that are not language evidence."""
    stripped = _CODE_FENCE_RE.sub(" ", text or "")
    stripped = _TEAMS_AT_RE.sub(" ", stripped)
    stripped = _ANGLE_MARKUP_RE.sub(" ", stripped)
    return _EMOJI_SHORTCODE_RE.sub(" ", stripped)


def channel_message_language(text: str) -> str:
    """Language of a question (web, IM, or scheduled job), or "" when it carries no reliable evidence.

    The single detector for the answer language of every surface. It is strict
    because a wrong verdict switches the whole turn; without evidence the caller
    falls back to the previous answer, the team default, then the interface:

    - Latin-only text counts only when `latin_language` is confident. Short replies
      ("ok", "yes", "A", "SKU TOP10") are no evidence, so the caller keeps the session's
      previous answer language.
    - Text with Han characters is Chinese unless its Latin part dominates the message
      and is a confident Latin-script sentence ("Show me revenue for 北京市 and 上海市").
      Chinese sentences with English product names or metrics stay Chinese.
    """
    stripped = strip_channel_markup(text)
    cjk, latin = _script_counts(stripped)
    if cjk == 0 and latin == 0:
        return ""
    if cjk == 0:
        return latin_language(stripped)
    if carrier_language(stripped) == "en-US":
        return latin_language(stripped) or "zh-CN"
    return "zh-CN"


# Dominance ratio for **model output**. 1.25 tie band suffices for questions, not answers;
# see `observed_language` docstring.
_OBSERVED_MIN_RATIO = 3.0


def observed_language(text: str, *, min_ratio: float = _OBSERVED_MIN_RATIO) -> str:
    """模型**实际写出来的**语言；证据不够压倒性 → ""（调用方保持原本定好的语言）。

    ## 为什么不能直接用 `carrier_language`

    116 实测（2026-09-08，`ws_942fd77aac33`）：BI 答案里的实体值——门店名、品牌名、
    品类名——**在这个空间里全是中文**，于是"英文骨架 + 一串中文实体"会把汉字计数
    顶上去，×2.5 加权后压过英文：

    - `The top 10 stores by foot traffic are: 东湖街(…), 南湖花园城(…), …`
      → 汉字 34 / 拉丁 54 ⇒ 加权 85 vs 54 ⇒ 旧判据判 **zh-CN（错）**
    - `In 2026, the top three 种类大类 by refund rate are 建材设备(51.7%)…`
      → 汉字 216 / 拉丁 521 ⇒ 加权 540 vs 521 ⇒ 旧判据判 **zh-CN（错）**

    判反的代价是实打实的：这个判据的两个消费点都拿它**改写本轮已定的语言**——
    chrome 回钉（`canvas_composer` / `report_writer_loop`）与「本会话上一轮」回落。
    一份英文报告被判成中文，chrome 就翻成中文 ⇒ **正是批3 当初要消灭的"一份报告两种语言"**。

    ## 判据：宁可不改钉，也不要改错

    只有当一侧**压倒**另一侧（默认 ≥3 倍）才认；否则返回 ""，让本轮原本定好的语言留着。
    同一批真实样本上的表现：真中文答 62:3 = 20× 照样翻钉；两条误判 1.04× / 1.6× 落进
    "不确定"、保持原判。今天该账号 62 轮里落进这一带的有 3 轮（≈5%）。

    ## ⚠ 只给「定下一轮语言」用，**不给 chrome 回钉用**（2026-09-08 实测反转过一次）

    我最初把 chrome 回钉也换成了这把尺子，400 轮真数据复核后退回去了：
    带内唯一一例真正改变行为的轮次（`t_7722383e74b6`：后端定 en-US、模型交了中文报告、
    比值 1.36×），**旧的灵敏判据才是对的** —— 它把 chrome 跟着中文报告翻过去，
    与用户眼前那份报告一致；保守判据反而留下"英文外壳 + 中文报告"。

    两个消费点的**目的与代价都不同**：

    - **chrome 回钉**：目的是"外壳跟眼前这份报告一致"，判错只是局部外壳不搭；
      且画布是长散文、实体被稀释 ⇒ **要灵敏**，用 `carrier_language`。
    - **本会话上一轮**（本函数）：目的是"定**下一轮整轮**的语言"，判错会把一整轮带反；
      且它读的是 chat 短答案——实体占比最高、最容易判反的那种文本 ⇒ **要保守**。

    ⚠ 别把这条阈值挪到 `carrier_language` 上：判提问时"不确定"没有安全的默认可退
    （用户就是没给足信号），而判输出时有——后端本来就定过语言。
    """
    if not text:
        return ""
    cjk, latin = _script_counts(text)
    if cjk == 0 and latin == 0:
        return ""
    if cjk == 0:
        return "en-US"
    if latin == 0:
        return "zh-CN"
    zh_weight = cjk * _LATIN_PER_CJK
    if zh_weight >= latin * min_ratio:
        return "zh-CN"
    if latin >= zh_weight * min_ratio:
        return "en-US"
    return ""


def observed_language_of_markdown(text: str, *, min_ratio: float = _OBSERVED_MIN_RATIO) -> str:
    """先剥代码块，再判模型实际写出来的语言（`observed_language` 的 markdown 入口）。"""
    return observed_language(_CODE_FENCE_RE.sub(" ", text or ""), min_ratio=min_ratio)


def session_follow_language_of_markdown(answer: str) -> str:
    """Language of the previous turn's answer, used only to carry it into the next turn.

    The only callers are the "same session, previous turn" fallbacks (web chat and Feishu):
    when the new message has no language evidence (a bare "1" picking an option), the turn
    follows the language the user last read. `observed_language` only separates Chinese from
    English, so a German or French answer came back as "en-US" and the next turn switched to
    English.

    On top of `observed_language`, a Latin-script verdict is refined with the same German /
    French word scorer the question side uses, but only over prose paragraphs (quotes,
    blockquotes, tables, list items and code are data the answer carries, not its language) and
    with model-output strength (at least 4 points and 3x the runner-up, against 2 and 2x for a
    question). When that scorer is not confident the answer keeps its previous verdict ("en-US").
    The Chinese / English verdicts and the "undecidable" band are unchanged.

    German is only reported while German AI output is admitted (`FEATURE_GERMAN_AI_OUTPUT`).
    With the gate closed, `LanguageContext.resolve` would turn a followed "de-DE" into the UI
    language, so a Chinese-UI user reading a German answer would get the next turn in Chinese;
    keeping the previous "en-US" verdict leaves that case exactly as it was.

    This only decides which language the next turn follows. It never judges whether an answer
    is correct, and the answer-language probe keeps using `observed_language_of_markdown`.
    """
    stripped = _CODE_FENCE_RE.sub(" ", answer or "")
    observed = observed_language(stripped)
    if observed != "en-US":
        return observed
    refined = _confident_latin_language(
        _prose_only(stripped),
        min_score=_FOLLOW_MIN_SCORE,
        min_ratio=_FOLLOW_MIN_RATIO,
    )
    if refined == "fr-FR" or (refined == "de-DE" and german_ai_output_enabled()):
        return refined
    return observed


# Judging a model answer needs stronger evidence than judging a question (the same reasoning
# as `_OBSERVED_MIN_RATIO`): an English answer often carries quoted or tabulated French /
# German data, and a wrong verdict switches the whole next turn.
_FOLLOW_MIN_SCORE = 4.0
_FOLLOW_MIN_RATIO = 3.0

_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# Quoted spans in the usual Latin-script quote styles, plus parenthesised glosses: text the
# answer cites or labels, not the language it is written in.
_QUOTED_SPAN_RE = re.compile(r'«[^»]*»|„[^“”"]*[“”"]|“[^”]*”|"[^"\n]*"|\([^()\n]*\)')
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s")


def _prose_only(markdown: str) -> str:
    """Keep only prose paragraphs: drop blockquotes, table rows, list items, code and quoted spans."""
    kept: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or stripped.count("|") >= 2 or _LIST_ITEM_RE.match(line):
            continue
        kept.append(line)
    text = _INLINE_CODE_RE.sub(" ", "\n".join(kept))
    return _QUOTED_SPAN_RE.sub(" ", text)
