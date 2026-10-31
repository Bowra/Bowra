"""said-not-done Seam B / WP-2 —— 通用 over-claim 判定（"说做了，其实没有"）。

**判据与产出路径无关**，只看两件事同时成立：

    ① 答案里声明了某类产物已完成    AND    ② 账单里该类为 0

现状的窄闸（`deck_render_inputs.deck_overclaim_caveat_needed`）第一步就要求
`subagent_spawn(profile="consultant")` —— 于是 08-04 上午走 `skill_invoke(bowhead-slides)`
的那几轮**根本没触发**，「已生成PPT演示稿，可直接下载」原样发给了用户。
本模块不问是谁产的、走哪条路产的，只问"该有的东西在不在"。

**双条件缺一不可，这是刻意的**：
误判（给一个真出了文件的轮次扣上"没出"的帽子）比漏判更伤用户，所以绝不允许只凭文本触发。
同理，未知声明类型走 `DeliverableLedger.count_for()` 的 `-1` 分支 → 放行不判。

**词表的失败方向是漏报**（discovery §4.5 已承认）：本文作者自己就漏过
「PDF 已经导出好了」「已成功导出」。2026-08-08 已用逐格式、数量、诚实部分成功和
账单不可用混合语料完成首轮校准并默认启用干预；显式 flag 仍可退回仅度量。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from bowhead_core.deliverable_ledger import DeliverableLedger

__all__ = [
    "CLAIM_KINDS",
    "claimed_file_count",
    "claimed_file_formats",
    "peer_facts_and_directive",
    "caveat_for",
    "detect_completion_claims",
    "overclaimed_kinds",
    "reconcile_overclaimed_text",
    "reconcile_unverified_claims",
    "unverifiable_claimed_kinds",
]

CLAIM_KINDS = ("file", "report", "memory")

# Completion verbs: all extracted from actual production answers (discovery §2.3).
_DONE_VERB = re.compile(
    r"已生成|已导出|已经导出|已成功导出|生成完毕|导出好了"
    r"|可直接下载|可下载|下载链接|已为你(生成|导出)|已保存为"
)
_NEW_FILE_VERB = re.compile(r"已生成|已导出|已经导出|已成功导出|生成完毕|导出好了|已为你(?:生成|导出)|已保存为", re.I)


def _file_format_count(text: str, ledger: DeliverableLedger, ext: str) -> int:
    return ledger.count_for_ext(ext) if _NEW_FILE_VERB.search(text) else ledger.count_available_for_ext(ext)


def _file_count(text: str, ledger: DeliverableLedger) -> int:
    return ledger.count_for("file") if _NEW_FILE_VERB.search(text) else ledger.count_available_files()


# Each claim also needs an artifact noun; verb alone insufficient ("分析已完成" is no file claim).
#
# ⚠ report **excluded** (116 dogfood 2026-08-05 false positive): export-only answer said
# "PDF **已生成**好了……完整覆盖了**画布**上的标题"; file completion verb plus incidental
# "画布" became a false report-generated claim despite normal export sections=0.
# Tests missed cross-kind contamination by supplying only isolated single-kind text.
# ⇒ report recognizes only **adjacent** wording (`_REPORT_CLAIM` / `_REPORT_POINTER`), never this broad table.
_KIND_NOUN = {
    "file": re.compile(
        r"PPT|演示稿|deck|幻灯|PDF|Excel|表格文件|Word|HTML|CSV|JSON|Markdown"
        r"|PNG|JPE?G|WEBP|GIF|SVG|MP4|WEBM|MOV|文件",
        re.I,
    ),
}


def _lat(token: str) -> str:
    """拉丁词界：两侧不得紧挨拉丁字母，但**允许紧挨中文**。

    不能用 `\\b`：Python 的 `\\w` 含中文，"生成PDF文件" 里 `F` 与 `文` 之间没有词界，
    `\\bPDF\\b` 会漏掉这种最常见的中英混排写法。
    反过来也不能不加界：裸 `word` 会命中 "in other words" / "password" / "keyword"，
    给一轮本来诚实的回答扣上"要 Word 没产出"的帽子。
    """
    return rf"(?<![A-Za-z]){token}(?![A-Za-z])"


# **Explicitly named format** → extension. Subset of `_KIND_NOUN["file"]`:
# latter asks "was a file mentioned?", this asks "which format was named?".
#
# Needed for actual t_1c11f59a7ba3, 1.0.70: user wanted .docx, sandbox docx transfer failed;
# peer used canvas-pdf fallback, produced PDF, then said "Word 文档已导出完成…可以直接下载使用",
# with 0 docx stored. Old criterion saw files=1 (PDF), disabling double condition and missing false claim.
# **Detection vocabulary was correct** (`detect_completion_claims` returned `file`); **ledger granularity failed**.
_FMT_NOUN: dict[str, re.Pattern[str]] = {
    ".pptx": re.compile("|".join([_lat("pptx?"), "演示稿", "演示文稿", "幻灯", _lat("decks?"), _lat("slides?")]), re.I),
    ".pdf": re.compile(_lat("pdf"), re.I),
    ".xlsx": re.compile("|".join([_lat("xlsx"), _lat("excel"), "电子表格", "表格文件"]), re.I),
    ".docx": re.compile("|".join([_lat("docx?"), _lat("word")]), re.I),
    ".html": re.compile("|".join([_lat("html?"), _lat("xhtml")]), re.I),
    ".csv": re.compile(_lat("csv"), re.I),
    ".tsv": re.compile(_lat("tsv"), re.I),
    ".json": re.compile(_lat("json"), re.I),
    ".md": re.compile("|".join([_lat("md"), _lat("markdown")]), re.I),
    ".txt": re.compile(_lat("txt"), re.I),
    ".png": re.compile(_lat("png"), re.I),
    ".jpg": re.compile("|".join([_lat("jpg"), _lat("jpeg")]), re.I),
    ".webp": re.compile(_lat("webp"), re.I),
    ".gif": re.compile(_lat("gif"), re.I),
    ".svg": re.compile(_lat("svg"), re.I),
    ".mp4": re.compile(_lat("mp4"), re.I),
    ".webm": re.compile(_lat("webm"), re.I),
    ".mov": re.compile(_lat("mov"), re.I),
}

_FILE_COUNT = re.compile(r"(?P<count>\d+|[零〇一二两三四五六七八九十]+)\s*(?:个|份)\s*文件")
_CLAIM_SEGMENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n+")
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


# Markdown list items (`- ` / `* ` / `1. `): combine intro + list into one claim block.
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _claim_blocks(text: str) -> list[str]:
    """把文本切成「声明块」：一行 + 紧随其后的连续 markdown 列表项。

    为什么需要（真机 116 `t_72011a27fce9`）：一次声明在真实答案里常常**跨行**——
    动词在引导句、格式名在列表项里：

        两份文件都已生成完毕：          ← 有动词 + 「文件」，但没有格式名
        - **Word 文档**：xxx.docx，…    ← 有格式名，但没有动词
        - **PPT 演示稿**：…             ← 同上

    逐行判「动词与名词共现」会把这次声明切断 ⇒ `claimed_file_formats` 恒返空集，
    按格式对账（"用户要的那种在不在"）在最常见的答案形态上直接失效。
    实测该轮：声称 Word+PPT、账单只有 .docx，靠数量启发式才勉强逮到。

    ⚠ 共现约束**本身是对的**（挡住"你上传的 PDF 我看了"这类误判），
    这里只放宽**块的边界**，不放宽共现要求。
    """
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        block = [lines[i]]
        j = i + 1
        # Intro must not itself be list item, or adjacent siblings absorb one another.
        if lines[i].strip() and not _LIST_ITEM.match(lines[i]):
            # ⚠ Intro/list commonly separated by **blank lines** (Markdown convention, actual t_72011a27fce9):
            # skip blanks first, but absorb them **only if list items actually follow**;
            # no list after blanks → preserve separation, never swallow unrelated paragraphs.
            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines) and _LIST_ITEM.match(lines[k]):
                j = k
                while j < len(lines) and (_LIST_ITEM.match(lines[j]) or not lines[j].strip()):
                    if lines[j].strip():
                        block.append(lines[j])
                    j += 1
        blocks.append("\n".join(block))
        i = j if j > i + 1 else i + 1
    return blocks


# Completing ingestion of existing bytes does not claim a newly delivered file.
_FILE_PROCESSING_DONE = re.compile(
    r"(?:处理|解析|索引|入库|操作)(?:[、与和及](?:处理|解析|索引))*[均都]?(?:已经|已)(?:完成|就绪)"
    r"|(?:已经|已)完成(?:处理|解析|索引|入库)"
)
# Generic completion must belong to the file itself, not an operation/status
# elsewhere in the sentence. Explicit creation/export/download still co-occurs.
_FILE_READY_CLAIM = re.compile(
    rf"(?:{_KIND_NOUN['file'].pattern}|{'|'.join(noun.pattern for noun in _FMT_NOUN.values())})[\s*`]*(?:均|都)?已(?:完成|就绪)",
    re.I,
)
_NEGATED_FILE_CLAUSE = re.compile(
    r"(?:没有|并未|尚未|未能|未)\s*(?:成功)?(?:生成|导出|制作|提供)"
    r"[^，,。！？!?；;\n]*?(?=[，,。！？!?；;\n]|但|不过|然而|$)"
)


def _file_claim_segments(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    segments: list[str] = []
    for block in _claim_blocks(text):
        hit = False
        for raw in _CLAIM_SEGMENT_SPLIT.split(block):
            segment = raw.strip()
            positive = _NEGATED_FILE_CLAUSE.sub("", _FILE_PROCESSING_DONE.sub("", segment))
            if not segment or not (_DONE_VERB.search(positive) or _FILE_READY_CLAIM.search(positive)):
                continue
            if _KIND_NOUN["file"].search(segment) or any(noun.search(segment) for noun in _FMT_NOUN.values()):
                segments.append(segment)
                hit = True
        # Block merging expands **inspection scope**, never co-occurrence: **one segment itself** must first match
        # completion verb + artifact noun, then include the whole block solely to expose list
        # formats (`- **Word 文档**：xxx.docx`) to `claimed_file_formats`.
        #
        # ★ Independent 2026-08-11 review found my introduced defect: previously
        # "verb anywhere + noun anywhere in block" **relaxed co-occurrence itself**,
        # contradicting P4 commit promise, misclassifying ordinary analysis as false file delivery:
        #     分析已完成，结论如下：
        #     - 数据来自你上传的 3 个 PDF，覆盖 2024 全年
        # verb in intro, `PDF` as **source data** in list; before `()`, after
        # `('file',)`, default intervention ⇒ users saw "no downloadable file successfully generated this turn".
        # True positive t_72011a27fce9 unaffected: intro "两份文件都已生成完毕："
        # **itself** has verb + file noun, already matched by per-segment scan.
        if hit and "\n" in block:
            whole = block.strip()
            if whole not in segments:
                segments.append(whole)
    return tuple(segments)


def _parse_count(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1:
        return _CN_DIGITS.get(value)
    return None


def claimed_file_count(text: str | None) -> int | None:
    """Return the largest explicit positive file-count claim, if present."""
    counts: list[int] = []
    for segment in _file_claim_segments(text):
        for match in _FILE_COUNT.finditer(segment):
            value = _parse_count(match.group("count"))
            if value is not None:
                counts.append(value)
    return max(counts) if counts else None


# Report adjacency claims: noun and verb must abut, never half a sentence apart.
_REPORT_CLAIM = re.compile(r"报告(已|都已)?(生成|写好|写完|完成|就绪)|(已|正式)?(生成|写)了?[一]?份?报告")

# r4 terminal-answer gate revision family: actual 116 R6 wording "全报告各段正文与 5 张图均按新口径重算重画"
# is neither report-generated nor necessarily see-canvas, missed by both patterns. Recalculate / redraw / revise themselves claim revision;
# update / change-to are broad, requiring adjacent report / canvas / body / chart / chapter / paragraphs nouns.
_REPORT_REVISION_CLAIM = re.compile(
    r"(已|均|都)[^。\n]{0,12}?(重算|重画|修订)|(报告|画布|正文|图表|章节|各段)[^。\n]{0,30}?(已|均|都)[^。\n]{0,12}?(更新|改为|改成)"
)

# Memory claims lack noun+verb structure; each is a complete assertion.
_MEMORY_CLAIM = re.compile(r"我记住了|已记住|已为你记住|我记下了|已记录到(长期)?记忆")

# Reports have separate pointer wording (no generation/export verbs); detect independently.
# ⚠ System **automatically adds** "完整报告见画布" after actual report persistence, so its presence
# need not be model-authored; double condition sections==0 is the actual criterion.
_REPORT_POINTER = re.compile(r"见画布|见 ?canvas|已在画布|落到画布|放到了画布|(?i:(?:the )?(?:full|complete) report is (?:now )?on the canvas)")


# ★ Strip guard's **own** status blocks (`_交付核验：…_` / `_注：…_`) before detection.
# Honest explanation contains completion verb "可下载" + artifact noun "文件", making the guard treat
# itself as a **new** false claim. Actual dev t_930e3dcb4d5a emitted an extra
# `lead.deliverable_overclaim kind=file` despite model honestly saying "未生成独立文件".
# Before caveat detect() was empty; afterward {'file'}. Cleanup runs more than once per turn,
# so second pass sees caveat-bearing text.
# Stripping makes detection **idempotent** on its own output: no inflated metrics or duplicate append.
_GUARD_STATUS_BLOCK = re.compile(r"(?m)^_(?:交付核验|注)：.*?_[ \t]*$")


def detect_completion_claims(text: str | None) -> frozenset[str]:
    """答案里声称完成了哪几类产物。检测不到返回空集（→ 不判、放行）。"""
    if not text:
        return frozenset()
    text = _GUARD_STATUS_BLOCK.sub("", text)
    kinds: set[str] = set()
    if _MEMORY_CLAIM.search(text):
        kinds.add("memory")
    if _REPORT_POINTER.search(text) or _REPORT_CLAIM.search(text):
        kinds.add("report")
    if _file_claim_segments(text):
        kinds.add("file")
    return frozenset(kinds)


def detect_report_work_claims(text: str | None) -> bool:
    """r4 终答闸的判据：答案声称**这一轮**对报告做了事（生成 / 修订 / 重算 / 重画），或明确说报告已生成。

    只指路「见画布」不算——那可能是在指上一轮的报告（「上一轮的报告已在画布，你想改哪一段？」），退回它反而错。
    """
    if not text:
        return False
    text = _GUARD_STATUS_BLOCK.sub("", text)
    return bool(_REPORT_REVISION_CLAIM.search(text) or _REPORT_CLAIM.search(text))


def claimed_file_formats(text: str | None) -> frozenset[str]:
    """答案里**点名了**哪几种文件格式（返回扩展名，小写含点）。

    识别不出 → 空集，调用方须退回"按交付物总数"的旧判据（宁可漏判不可误判）。
    """
    if not text:
        return frozenset()
    segments = _file_claim_segments(text)
    return frozenset(ext for ext, noun in _FMT_NOUN.items() if any(noun.search(segment) for segment in segments))


def overclaimed_kinds(text: str | None, ledger: DeliverableLedger) -> tuple[str, ...]:
    """声明了但账单为 0 的那几类 —— 即"说做了其实没有"。

    `count_for()` / `count_for_ext()` 返回 -1 表示该项无法对账 ⇒ **放行**，不计入。

    `file` 这一类走**两级判据**：
      ① 能从声明里识别出具体格式 → 按格式核（用户要的那种在不在），
      ② 识别不出（只说"文件已生成"）→ 退回按交付物总数核。
    ⇒ **收紧方向是单向的**：新判据只在①成立时生效，识别不出一律走老路，
      不会让任何原本放行的普通轮次变成误报。
    """
    bad: set[str] = set()
    for kind in detect_completion_claims(text):
        if kind != "file":
            if ledger.count_for(kind) == 0:
                bad.add(kind)
            continue
        if any(_invalid_file_claim_segment(segment, ledger) for segment in _file_claim_segments(text)):
            bad.add("file")
    return tuple(sorted(bad))


def unverifiable_claimed_kinds(text: str | None, ledger: DeliverableLedger) -> tuple[str, ...]:
    """Completion claims whose corresponding ledger source could not be read."""
    unverified: set[str] = set()
    for kind in detect_completion_claims(text):
        if ledger.count_for(kind) < 0:
            unverified.add(kind)
            continue
        if kind == "file" and any(
            _file_count(segment, ledger) < 0 or any(_file_format_count(segment, ledger, ext) < 0 for ext in claimed_file_formats(segment))
            for segment in _file_claim_segments(text)
        ):
            unverified.add(kind)
    return tuple(sorted(unverified))


# chrome-i18n (dogfood residue): corrective notes are user-visible; English turns must not append Chinese.
# file-branch _file_reconciliation_status details remain zh-only (deep string assembly,
# separate batch); entire branch stays Chinese under en, recorded as residual.
_CAVEAT_BY_LANG = {
    "zh-CN": {
        "file": "本轮没有成功生成可下载文件（上面的内容已在画布上）。可以重试，或换个格式再让我生成。",
        "report": "本轮没有内容落到画布上。",
        "memory": "这条信息**没有**被写入长期记忆，下次对话不会自动带上；需要的话请再说一次。",
    },
    "en-US": {
        "file": "No downloadable file was successfully produced this turn (the content above is on the canvas). You can retry, or ask for a different format.",
        "report": "No content landed on the canvas this turn.",
        "memory": "This was **not** written to long-term memory and won't carry into future conversations; please say it again if needed.",
    },
    "de-DE": {
        "file": "In diesem Durchgang wurde keine herunterladbare Datei erstellt (der Inhalt oben befindet sich auf der Arbeitsfläche). Sie können es erneut versuchen oder ein anderes Format anfordern.",
        "report": "In diesem Durchgang wurden keine Inhalte auf der Arbeitsfläche gespeichert.",
        "memory": "Diese Information wurde **nicht** dauerhaft gespeichert und wird in künftigen Gesprächen nicht übernommen. Bitte erwähnen Sie sie bei Bedarf erneut.",
    },
}
_CAVEAT_BY_LANG["fr-FR"] = {
    "file": "Aucun fichier téléchargeable n’a pu être produit pour cette demande ; le contenu ci-dessus se trouve sur le canevas. Vous pouvez réessayer ou demander un autre format.",
    "report": "Aucun contenu n’a été ajouté au canevas pour cette demande.",
    "memory": "Cette information n’a **pas** été enregistrée dans la mémoire à long terme et ne sera pas reprise dans les prochaines conversations. Rappelez-la si nécessaire.",
}
_CAVEAT = _CAVEAT_BY_LANG["zh-CN"]  # Existing detection (kinds ∈ _CAVEAT) retains zh keys.


_PEER_NO_FILE_DIRECTIVE = (
    "\n⚠ **本轮没有任何可下载文件登记成功**（produced.files = 0）。"
    "上面 subagent 的话里虽然提到已生成/已导出文件，但那个文件**没有**进入"
    "可下载产物列表，用户拿不到。**不要**在回答里说「已生成 / 已导出 / "
    "可直接下载 / 见附件」；如实说明文件没能产出、可以重试。"
)

_PEER_WRONG_FORMAT_DIRECTIVE = (
    "\n⚠ **用户点名要的格式（{want}）本轮并没有产出**。"
    "已登记的可下载文件见 produced.file_names —— 里面**没有** {want}。"
    "上面 subagent 可能用别的格式兜了底，但那不是用户要的东西。"
    "**不要**说「{want} 已生成 / 可直接下载」；如实说明要的格式没能产出、"
    "实际产出的是哪个文件，让用户决定是否接受或重试。"
)


def peer_facts_and_directive(ledger: DeliverableLedger, peer_answer: str | None) -> tuple[dict[str, Any], str]:
    """Seam B 预防路：peer 返回时给 Lead 的「产出事实」+（必要时）一句禁令。

    返回 `(produced_facts, directive_addendum)`；`directive_addendum` 为空串表示不追加。

    **只在 peer 自己声称产了文件、而账单里没有时才追加禁令** —— 否则会给本来就不该
    有文件的普通分析轮乱加噪音（误判比漏判更伤，同本模块的双条件原则）。

    设计要点（tech-design-seam-b.md）：价值在 `produced` 这一半（**给事实**），
    禁令只是附赠。dogfood 实证模型不是想撒谎，是它只看到 skill 返回 ok:true、
    不知道产物没落库；而本仓已反复证明"写禁令"不保证生效。
    """
    facts: dict[str, Any] = {
        "files": ledger.files if ledger.files_verified else None,
        # ★ Main path here: supply **names**, not only counts.
        # Actual t_bef61acf9854/t_1c11f59a7ba3 prove "files: 1" is **true but insufficiently specific**;
        # Lead assumes requested format exists and says "download directly" anyway.
        # Names let it detect wrong formats itself; this half does not depend on model obeying prohibitions.
        "file_names": list(ledger.file_names) if ledger.files_verified else [],
        "reused_files": ledger.reused_files if ledger.reused_files_verified else None,
        "reused_file_names": list(ledger.reused_file_names) if ledger.reused_files_verified else [],
        "canvas_sections": ledger.sections if ledger.sections_verified else None,
        "memories": ledger.memories if ledger.memories_verified else None,
        "verified": {
            "files": ledger.files_verified,
            "reused_files": ledger.reused_files_verified,
            "canvas_sections": ledger.sections_verified,
            "memories": ledger.memories_verified,
        },
    }
    # Three stages; order matters:
    #   ① Peer never claimed file delivery → no prohibition. **Completion verb + file noun required**;
    #      format mention alone must not trigger, or honest "I cannot generate PDF" also gains prohibitions
    #      (false positives worse than misses, consistent with double-condition principle).
    #   ② No files produced → say "nothing produced", more precise/actionable than "wrong format".
    #   ③ Files produced but **requested named format absent** → identify actual fallback (docx→PDF incident).
    if "file" not in detect_completion_claims(peer_answer) or not ledger.files_verified:
        addendum = ""
    elif ledger.files == 0 and ledger.reused_files == 0 and ledger.reused_files_verified:
        addendum = _PEER_NO_FILE_DIRECTIVE
    else:
        missing = sorted(f for f in claimed_file_formats(peer_answer) if ledger.count_available_for_ext(f) == 0)
        addendum = _PEER_WRONG_FORMAT_DIRECTIVE.format(want="/".join(missing)) if missing else ""
        if ledger.reused_files:
            addendum += "\nReused files are existing, authorized inputs, not new outputs. Report that they remain available; do not claim they were newly generated or ask to regenerate an already available format."
    return facts, addendum


def caveat_for(kinds: tuple[str, ...] | list[str], lang: str = "en-US") -> str:
    """给用户的诚实说明。空 kinds → 空串（调用方据此判断要不要追加）。

    只说事实 + 一条可自救的下一步，不解释内部机制（用户不关心是 consultant 还是 skill）。
    """
    if lang not in _CAVEAT_BY_LANG:
        lang = "en-US"
    table = _CAVEAT_BY_LANG[lang]
    lines = [table[k] for k in kinds if k in table]
    if not lines:
        return ""
    prefix = {
        "en-US": "\n\n_Note: ",
        "de-DE": "\n\n_Hinweis: ",
        "fr-FR": "\n\n_Remarque : ",
    }.get(lang, "\n\n_注：")
    return prefix + " ".join(lines) + "_"


_FORMAT_LABEL = {
    ".pptx": "PPTX",
    ".pdf": "PDF",
    ".xlsx": "Excel",
    ".docx": "Word",
    ".html": "HTML",
    ".csv": "CSV",
    ".tsv": "TSV",
    ".json": "JSON",
    ".md": "Markdown",
    ".txt": "TXT",
    ".png": "PNG",
    ".jpg": "JPEG",
    ".webp": "WebP",
    ".gif": "GIF",
    ".svg": "SVG",
    ".mp4": "MP4",
    ".webm": "WebM",
    ".mov": "MOV",
}


def _invalid_file_claim_segment(segment: str, ledger: DeliverableLedger) -> bool:
    if not _file_claim_segments(segment):
        return False
    file_count = _file_count(segment, ledger)
    if file_count < 0:
        return False
    count = claimed_file_count(segment)
    if count is not None and count > file_count:
        return True
    formats = claimed_file_formats(segment)
    if any(_file_format_count(segment, ledger, ext) == 0 for ext in formats):
        return True
    return file_count == 0 and not formats


# Strip at **clause level**, including commas, matching `_strip_claimed_kinds`.
#
# Previously split only at sentence-ending `[。！？!?；;]`; Chinese comma-linked false claims and true data
# caused whole-sentence deletion. Measured 95 entries across 116 DB: 18 changed, 17.2% body deletion
# (median 13.5%, 2 >50%, worst 86.4%). Typical victim:
#   "PPT 已生成。来看7月营业额的核心发现：7月整体营业额 1.36 亿，日均约 439 万…"
# lost genuine data alongside false claim.
#
# ⚠ Two stripping functions with different granularity directly caused this incident; share one constant,
# never let them drift independently again.
_CLAUSE_SPLIT = re.compile(r"(?<=[。！？!?；;，,])")

# Once `files == 0` is established, remaining **verb-only** clauses (no artifact noun, undetectable claims)
# still promise downloads. Actual case: after stripping "PDF 已经导出好了！",
# "你可以直接下载使用" survived. Ledger=0 makes every downloadable assertion false.
# ⚠ Enable only with files==0; files>0 would remove correct pointers to **real files**.
_DANGLING_DOWNLOAD = re.compile(r"可直接下载|可以直接下载|可下载|请下载|见附件|在附件中|附件里")


def _strip_invalid_file_claims(text: str, ledger: DeliverableLedger) -> str:
    no_files = ledger.count_available_files() == 0
    lines: list[str] = []
    for line in text.splitlines():
        parts = _CLAUSE_SPLIT.split(line)
        kept_parts = []
        for part in parts:
            if _invalid_file_claim_segment(part, ledger):
                continue
            if no_files and _DANGLING_DOWNLOAD.search(part):
                continue
            kept_parts.append(part)
        kept = "".join(kept_parts).strip()
        if kept:
            lines.append(kept)
    return "\n".join(lines).strip()


def _strip_claimed_kinds(text: str, kinds: tuple[str, ...]) -> str:
    if not kinds:
        return text.strip()
    rejected = set(kinds)
    lines: list[str] = []
    for line in text.splitlines():
        parts = _CLAUSE_SPLIT.split(line)
        kept = "".join(part for part in parts if detect_completion_claims(part).isdisjoint(rejected)).strip()
        if kept:
            lines.append(kept)
    return "\n".join(lines).strip()


def _file_reconciliation_status(text: str, ledger: DeliverableLedger, lang: str) -> str:
    retained = ""
    if ledger.reused_files_verified and ledger.reused_file_names:
        retained = (
            "已有文件仍可下载：" + "、".join(ledger.reused_file_names) + "。"
            if lang == "zh-CN"
            else "Existing files remain available: " + ", ".join(ledger.reused_file_names) + "."
        )
    if lang == "zh-CN":
        if ledger.files <= 0:
            if retained:
                return "本轮没有新生成文件。" + retained
            return "本轮没有成功生成可下载文件。可以重试，或换个格式再让我生成。"
        produced = "已成功生成：" + "、".join(ledger.file_names) if ledger.file_names else f"已核验 {ledger.files} 个可下载文件"
    else:
        if ledger.files <= 0:
            if retained:
                return "No new files were produced this turn. " + retained
            return "No downloadable file was successfully produced this turn. You can retry or ask for another format."
        produced = "Successfully produced: " + ", ".join(ledger.file_names) if ledger.file_names else f"Verified {ledger.files} downloadable files"
    missing = sorted(ext for ext in claimed_file_formats(text) if ledger.count_available_for_ext(ext) == 0)
    if missing:
        labels = ("、" if lang == "zh-CN" else ", ").join(_FORMAT_LABEL.get(ext, ext.lstrip(".").upper()) for ext in missing)
        if lang == "zh-CN":
            return f"{produced}；{labels} 未生成。可以重试缺失格式。"
        return f"{produced}; {labels} was not produced. You can retry the missing formats."
    count = claimed_file_count(text)
    if count is not None and count > ledger.files:
        if lang == "zh-CN":
            return f"{produced}；回答声称 {count} 个，但当前只核验到 {ledger.files} 个。可以重试缺失文件。"
        return f"{produced}; the answer claimed {count} files, but only {ledger.files} were verified. You can retry the missing files."
    return produced + ("。" if lang == "zh-CN" else ".") + (" " + retained if retained else "")


def reconcile_overclaimed_text(
    text: str | None,
    ledger: DeliverableLedger,
    kinds: tuple[str, ...] | list[str],
    lang: str = "en-US",
) -> str:
    """Replace contradicted completion claims with one ledger-backed final status."""
    bad = tuple(kind for kind in kinds if kind in _CAVEAT)
    if not bad:
        return text or ""
    reconciled = (text or "").strip()
    if "file" in bad:
        reconciled = _strip_invalid_file_claims(reconciled, ledger)
        prefix = "_交付核验：" if lang == "zh-CN" else "_Delivery check: "
        status = prefix + _file_reconciliation_status(text or "", ledger, lang) + "_"
        reconciled = (reconciled.rstrip() + "\n\n" + status).strip()
    remaining = tuple(kind for kind in bad if kind != "file")
    if remaining:
        reconciled = _strip_claimed_kinds(reconciled, remaining)
        reconciled = reconciled.rstrip() + caveat_for(remaining, lang)
    return reconciled


def reconcile_unverified_claims(
    text: str | None,
    ledger: DeliverableLedger,
    kinds: tuple[str, ...] | list[str],
    lang: str = "en-US",
) -> str:
    """Remove only unverifiable success sentences and append a conservative status."""
    if lang not in _CAVEAT_BY_LANG:
        lang = "en-US"
    reconciled = (text or "").strip()
    pending = tuple(kind for kind in kinds if kind in CLAIM_KINDS)
    if "file" in pending:
        lines: list[str] = []
        for line in reconciled.splitlines():
            parts = re.split(r"(?<=[。！？!?；;])", line)
            kept = "".join(part for part in parts if not _file_claim_segments(part)).strip()
            if kept:
                lines.append(kept)
        reconciled = "\n".join(lines).strip()
        note = {
            "en-US": "_Delivery check: the number and format of files from this turn could not be verified. Please check the results list or try again._",
            "de-DE": "_Lieferprüfung: Anzahl und Format der Dateien aus diesem Durchgang konnten nicht verifiziert werden. Bitte prüfen Sie die Ergebnisliste oder versuchen Sie es erneut._",
            "fr-FR": "_Vérification des résultats : le nombre et le format des fichiers produits pour cette demande n’ont pas pu être vérifiés. Consultez la liste des résultats ou réessayez._",
        }.get(lang, "_交付核验：暂时无法核验本轮文件的数量和格式，请以成果列表为准或重试。_")
        reconciled = (reconciled.rstrip() + "\n\n" + note).strip()
    remaining = tuple(kind for kind in pending if kind != "file")
    if remaining:
        reconciled = _strip_claimed_kinds(reconciled, remaining)
        note = (
            "_Delivery check: some completion statuses could not be verified yet — please rely on the corresponding results page._"
            if lang == "en-US"
            else "_Vérification des résultats : certains états d’achèvement n’ont pas encore pu être vérifiés. Consultez la page des résultats correspondante._"
            if lang == "fr-FR"
            else "_Lieferprüfung: Einige Abschlussstatus konnten noch nicht verifiziert werden. Bitte prüfen Sie die entsprechende Ergebnisseite._"
            if lang == "de-DE"
            else "_交付核验：部分完成状态暂时无法核验，请以对应成果页面为准。_"
        )
        reconciled = (reconciled.rstrip() + "\n\n" + note).strip()
    return reconciled
