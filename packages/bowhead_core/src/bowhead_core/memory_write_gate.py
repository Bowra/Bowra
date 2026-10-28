"""记忆重做 批2 —— FR8 write gate（`docs/sprints/memory-spike-2026-08-29/tech-design.md` §4.3）。

四条判据落成**一个函数**，`remember` 与候选 approve **两条写路都必须过**
—— 不是只在 prompt 里写「要严格」。存量 58 条候选里 3 条垃圾 + ~12 条把一次性要求
当长期偏好，就是「只在 prompt 里要求严格」的实测结果。

| # | 判据          | 不过时                                            |
| - | ------------- | ------------------------------------------------- |
| ① | durability    | 一次性要求（本轮/这次/临时/for now…）→ 拒写      |
| ② | attribution   | 既没 turn_id 也没 UI actor → 拒写                |
| ③ | dedup 优先    | **不在本模块**：`commit_memory_write` 第①道已做   |
| ④ | 不含密钥凭据  | 复用 `secret_redaction.extract_secrets` → 拒写   |

③ 为什么不在这里：判重要读库（要拿这个用户现有的条目逐条比），而本模块刻意保持
**纯函数、无 IO** —— 判据能被单测直接问「这句算不算一次性」，不必先起一个库。
判重已经在 `UserMemoryStore.commit_memory_write` 的第①道里做掉了（批1），
在这里再写一份就是「一个概念 N 处实现」。

★ **④ 的 seam 是 `secret_redaction`，不是 tech-design 写的 `memory_privacy`**：
`memory_privacy` 整个模块是删除账本 / 销毁操作 / 隐私擦除，**不做内容扫描**；
真正认得出 `xxx_api_key=sk-...` 的是 `secret_redaction.extract_secrets`。
（tech-design §4.3 那处指错了，已在其 Changelog 记档。）

★★ **判定顺序即正确性**（本仓踩过：族间子串包含会让被包含的那族永远走不到）：
**语言族必须先于一次性族判**。「这次之后都改用英文回答」含一次性词形 `这次`，
但它是一条**持久**的语言偏好，该走 `config_propose` 而不是被当成一次性要求打发掉。
判据上线前在 35 条真串上空跑过：正确顺序 0 误判，把两族调个个儿 → 正好这一条翻车。
"""

from __future__ import annotations

from dataclasses import dataclass

#: Language family = language noun AND answer/output verb.
#: ⚠ Both conditions are required: language nouns alone falsely reject many real memories:
#: "报告里品牌名保留英文原文", "字段名用英文缩写", "表头中英文对照" all contain "英文",
#: but none specifies the reply language. Recall can be borrowed across surfaces; false-positive rates cannot.
_LANGUAGE_NOUNS: tuple[str, ...] = (
    "中文",
    "英文",
    "日文",
    "英语",
    "汉语",
    "中英",
    "english",
    "chinese",
    "japanese",
    "zh-cn",
    "en-us",
)
#: ⚠ Exclude `输出` / `书写` / `output`: in BI these are generic verbs
#: ("中文报告**输出**时保留英文品牌名", "表头中英文对照，**输出**到 Excel"),
#: which would classify many real memories as reply-language settings and reject writes. Reproduced in review.
#: Accept only verbs explicitly referring to replying.
_ANSWER_VERBS: tuple[str, ...] = (
    "回答",
    "回复",
    "作答",
    "答复",
    "说话",
    "对话",
    "respond",
    "reply",
    "answer",
    "speak",
)

#: One-off family.
#: ⚠ Every term needs a qualifying prefix (`本轮`/`这轮`, not bare `轮`; `本次`/`这次`, not bare `次`).
#: Bare terms reject explicit long-term preferences such as "**每次**分析都要先给结论" and "**每轮**都要列出数据来源"
#: as one-off requests: precisely the harm caused by substring containment across families.
#: ★ Round two removed more bare terms from this family: `这张` ("**这张**排名图一律用横向条形图"),
#: `本题`/`这道` ("**本题**类问题都要先给结论"), and English `one-off`.
#: The last is especially harmful: `exclude one-off items from revenue` is a valid long-term financial definition,
#: yet was classified as a one-off request. Record two consecutive failures to follow the same rule.
#: ★ The initial version violated its own rule with bare `临时` / `暂时` / `这段`, so
#: "不要建**临时**表", "数据**暂时**以财务口径为准", "回款口径**这段**业务按合同签署日" (`这段`/`本段` removed for the same reason)
#: were all classified as one-off requests. Review reproduced this; remove the entire group. Missing some one-off requests
#: is preferable to falsely rejecting a real preference (this sprint consistently takes that risk direction).
_ONE_OFF_MARKERS: tuple[str, ...] = (
    "本轮",
    "这轮",
    "本次",
    "这次",
    "这一次",
    "只此一次",
    "just for this",
    "for now",
    "this time",
    "temporarily",
)

#: Write-refusal reasons, passed unchanged to model/user in receipts; wording is product copy.
REASON_LANGUAGE_IS_A_SETTING = "language_is_a_setting"
REASON_ONE_OFF = "one_off"
REASON_NO_ATTRIBUTION = "no_attribution"
REASON_CONTAINS_SECRET = "contains_secret"
#: Track B batch 1b: entries matching official team terminology are rejected in the personal domain and directed to the team channel.
#: Personal memory cannot change team definitions: redefining gross margin with a personal formula
#: makes the same metric differ from colleagues' views while the user thinks the company-wide definition changed.

#: Refusal copy by language. ★ These strings are user-visible: the model relays `remember` receipts,
#: and the frontend displays approve's 422 `detail.message` unchanged in a toast. Hardcoded Chinese gives English users Chinese text
#: plus a backend tool name (`config_propose` / `form_id`). This module's `_format_bullet` was already fixed
#: for Chinese prefixes in English users' notes; this repeats the same mistake.
_MESSAGES: dict[str, dict[str, str]] = {
    "zh-CN": {
        # ⚠ State only memory-side facts: do not claim saving has no effect (saying "以后都用英文回答" in chat makes Lead call `config_propose(form_id=user_language)`, risk_tier=1, persisting without a confirmation bar; 2026-09-14 116 dogfood: zh-CN→en-US),
        # nor tell users to change it in settings (that page has no such control). See `memory_auto_capture.REASON_MESSAGES`.
        REASON_LANGUAGE_IS_A_SETTING: "回答语言由设置决定，不作为记忆保存，所以这条没有记进记忆。",
        REASON_ONE_OFF: "这听起来是这一轮的临时要求，不是长期偏好，没有存进长期记忆。",
        REASON_NO_ATTRIBUTION: "这条写入没有来源（既不属于某一轮对话，也不是你在「设置 › 记忆」中手动新建），没有保存。",
        REASON_CONTAINS_SECRET: "这条内容里有密钥/凭据类信息，没有存进长期记忆。",
    },
    "en-US": {
        REASON_LANGUAGE_IS_A_SETTING: "Answer language is a setting, not a memory, so this was not saved to memory.",
        REASON_ONE_OFF: "This looks like a one-off request for this turn rather than a lasting preference, so it was not saved to long-term memory.",
        REASON_NO_ATTRIBUTION: "This write has no provenance (neither part of a conversation turn nor created by you in Settings › Memory), so it was not saved.",
        REASON_CONTAINS_SECRET: "This content contains credentials or secrets, so it was not saved to long-term memory.",
    },
    "de-DE": {
        REASON_LANGUAGE_IS_A_SETTING: "Die Antwortsprache ist eine Einstellung und keine Erinnerung. Deshalb wurde dies nicht als Erinnerung gespeichert.",
        REASON_ONE_OFF: "Das klingt nach einer einmaligen Anforderung für diesen Gesprächsschritt und nicht nach einer dauerhaften Präferenz. Deshalb wurde sie nicht im Langzeitgedächtnis gespeichert.",
        REASON_NO_ATTRIBUTION: "Diese Angabe hat keine nachvollziehbare Herkunft: Sie gehört weder zu einem Gesprächsschritt noch wurde sie von Ihnen unter „Einstellungen › Erinnerungen“ erstellt. Deshalb wurde sie nicht gespeichert.",
        REASON_CONTAINS_SECRET: "Dieser Inhalt enthält Zugangsdaten oder Geheimnisse und wurde deshalb nicht im Langzeitgedächtnis gespeichert.",
    },
    "fr-FR": {
        REASON_LANGUAGE_IS_A_SETTING: "La langue des réponses est un paramètre, pas un souvenir. Cet élément n’a donc pas été enregistré en mémoire.",
        REASON_ONE_OFF: "Cela ressemble à une demande ponctuelle pour cet échange plutôt qu’à une préférence durable ; elle n’a donc pas été enregistrée dans la mémoire à long terme.",
        REASON_NO_ATTRIBUTION: "L’origine de cet élément n’est pas identifiable : il ne provient ni d’un échange de conversation ni d’une création manuelle de votre part dans « Paramètres › Mémoire ». Il n’a donc pas été enregistré.",
        REASON_CONTAINS_SECRET: "Ce contenu contient des identifiants ou des secrets ; il n’a donc pas été enregistré dans la mémoire à long terme.",
    },
}


@dataclass(frozen=True)
class WriteGateVerdict:
    """一次写入闸的判定。`allowed=False` 时 `reason` 必非空。"""

    allowed: bool
    reason: str | None = None
    message: str = ""
    #: Redacted text on criterion ④ matches, for logs/receipts; never expose the original.
    redacted_text: str | None = None


_ALLOWED = WriteGateVerdict(allowed=True)


def _refuse(reason: str, *, lang: str, redacted_text: str | None = None) -> WriteGateVerdict:
    from bowhead_core.presentation_locale import resolve_presentation_locale

    table = _MESSAGES[resolve_presentation_locale(lang)]
    # ⚠ `table[reason]` deliberately raises KeyError when a new refusal reason lacks registered copy,
    # rather than falling back to generic text that silently lets users see error-code bugs
    # (already fixed twice in this sprint).
    text = table[reason]
    return WriteGateVerdict(
        allowed=False,
        reason=reason,
        message=text,
        redacted_text=redacted_text,
    )


#: Language-setting key shapes (structured candidates put semantics in the key).
#: ★ Depending on answer/reply occurring in the key is luck: `answer_language` / `reply_language` match,
#: but `output_language` / `response_language` / `preferred_language` / `ui_language`
#: all slip through. Language candidates are the largest group among 58 entries, so keys need their own criterion.
_LANGUAGE_KEY_SUFFIXES: tuple[str, ...] = ("_language", "_lang", "language", "lang")


def _key_is_language_setting(key: str) -> bool:
    k = str(key or "").strip().lower()
    return bool(k) and (k in ("language", "lang") or any(k.endswith(sfx) for sfx in _LANGUAGE_KEY_SUFFIXES))


def looks_like_language_preference(text: str) -> bool:
    """语言名词 **AND** 回答/输出动词。见 `_LANGUAGE_NOUNS` 上的误杀说明。

    ⚠ 这里只看**自然语言正文**。结构化候选的 key 侧判据是 `_key_is_language_setting`，
    两者刻意**不共用一条 AND**：把 key 拼进正文再跑这条 AND，会让 key 提供动词那一半，
    于是 `answer_style` + 「中文摘要放最前」被判成语言设置 —— 一条排版偏好被推去
    改语言设置（那个表单根本表达不了它）。这是修 B2-3 时新引入的误杀，实测复现过。
    """
    low = str(text or "").lower()
    return any(noun in low for noun in _LANGUAGE_NOUNS) and any(verb in low for verb in _ANSWER_VERBS)


def one_off_markers(text: str) -> tuple[str, ...]:
    """命中的一次性词（返回全部命中，便于回执点名与用例断言）。"""
    low = str(text or "").lower()
    return tuple(marker for marker in _ONE_OFF_MARKERS if marker in low)


def combined_text(*, key: str | None, value_text: str) -> str:
    """结构化条目过闸时要检查的文本 = **key + value**。

    ★ 只看 value 会漏掉一整类：`{kind:'preference', key:'answer_language', value:'English'}`
    —— 语义全在 key 上，value 只是个短答案（`English` / `zh-CN` / `Monday`），
    单看 value 每条闸都过得去。而语言候选正是本 sprint 要清的最大一桶（58 条里 8 条）。

    ★ 所有调用点都必须用**同一个函数**取这段文本。初版入队处用 `str(value)`、
    approve 处用 `comparable_text(value_json)`，于是 dict 值的 Python repr
    （单引号）骗过了密钥扫描：入队放行、approve 才炸，用户拿到一个无从下手的 422。
    「词表只有一份」不够，**取文本的方式也必须只有一份**。
    """
    # ★ Join with `=`, not a space: `secret_redaction` recognizes paired `key=value` / `key: value`
    # forms. A space leaves `openai_api_key sk-live-...` unmatched,
    # although named keys with bare credential values are the most common structured-candidate shape,
    # making criterion ④ fail exactly where needed (measured: joining with spaces gives allowed=True).
    # ★ Flatten whitespace here, not independently at call sites: `remember` used
    # `" ".join(text.split())`, while the candidate path did not, producing opposite secret verdicts
    # for the same multiline credential (`_KV_LINE_RE`'s `[ \t]*` cannot cross newlines). One way to derive text
    # must be implemented in this function to count.
    flat = " ".join(str(value_text or "").split())
    flat_key = " ".join(str(key or "").split())
    return f"{flat_key}={flat}" if flat_key else flat


def check(
    *,
    value_text: str,
    has_turn_id: bool,
    actor_type: str | None = None,
    key: str | None = None,
    lang: str = "en-US",
) -> WriteGateVerdict:
    """四条闸的唯一入口。两条写路（`remember` / 候选 approve）都过这里。

    `has_turn_id` / `actor_type` 合起来是判据 ②：
    ⚠ **不能只认 `turn_id`** —— `POST /api/v1/memory/user/remember`（用户在记忆页
    手动新建，`source='user_explicit'`）**没有 turn_id**，只认 turn_id 会把用户
    自己的写入全部拒掉。判据 = 「有 turn_id」**或**「有 UI actor」。
    """
    # ★ Derive text only once, here. Previously callers passed their own `combined_text(...)`,
    # making body and key+value indistinguishable inside the gate, so criterion ① received text with the key prepended.
    # `combined_text`'s docstring already required one way to derive text,
    # but that one implementation lived at four call sites; now it lives in this function.
    body = str(value_text or "")
    combined = combined_text(key=key, value_text=body)

    # ① Judge the language family first (ordering determines correctness; see module docstring).
    # Key and body have separate criteria; never combine them (see `looks_like_language_preference`).
    # ⚠ Body checks must receive only `body`, never `combined`: the key would supply the AND's verb half,
    # classifying `answer_style` + "中文摘要放最前" as a language setting. This false positive was documented in
    # `looks_like_language_preference`'s docstring, but `check` never followed it.
    # Documentation does not prove runtime behavior; final review caught this on 2026-09-04,
    # while the gate was already enabled in production.
    if _key_is_language_setting(key) or looks_like_language_preference(body):
        return _refuse(REASON_LANGUAGE_IS_A_SETTING, lang=lang)

    # ① One-off family. Check only the body here too: keys are identifiers and must not supply one-off semantics such as `这次`/`先`.
    if one_off_markers(body):
        return _refuse(REASON_ONE_OFF, lang=lang)

    # ② Attribution. Do not apply retroactively to existing rows; this gate affects only new writes (tech-design §5.2).
    if not has_turn_id and (actor_type or "") != "user":
        return _refuse(REASON_NO_ATTRIBUTION, lang=lang)

    # ④ Secrets/credentials.
    from bowhead_core.secret_redaction import extract_secrets

    # ④ Secrets require paired key=value forms (`secret_redaction` recognizes them), so pass `combined`.
    # ⚠ r3 (2026-09-09): criterion ⑤ formerly checked team terminology. Dry runs on two independent corpora found
    # match-any-term rejection falsely rejected 36.8%/41.1%, with holdout recall only 5/9. Redefinition markers derived from one corpus
    # collapsed to 2/9 on the holdout. Citation versus redefinition is semantic; a term list has no correct tuning.
    # User decision: remove it. Replacement = two write entrances (the personal-domain path structurally cannot reach team surfaces);
    # see tech-design §1.4 and dryrun-team-terms-2026-09-08.md. Do not restore this criterion.
    redacted, hits = extract_secrets(combined)
    if hits:
        return _refuse(REASON_CONTAINS_SECRET, lang=lang, redacted_text=redacted)

    return _ALLOWED


__all__ = [
    "REASON_CONTAINS_SECRET",
    "REASON_LANGUAGE_IS_A_SETTING",
    "REASON_NO_ATTRIBUTION",
    "REASON_ONE_OFF",
    "WriteGateVerdict",
    "check",
    "combined_text",
    "looks_like_language_preference",
    "one_off_markers",
]
