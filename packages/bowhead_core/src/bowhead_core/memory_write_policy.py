"""记忆重做 批1 —— 写入裁决的**纯逻辑**（无 DB、无 IO、无时间）。

上游：`docs/sprints/memory-spike-2026-08-29/tech-design.md` §4.2 (b)(d)。

放在独立模块的理由：判据本身要能被单测直接钉住（给两个串问「算不算同一条」、
给两个 confidence 问「能不能覆盖」），不必先起一个库。`user_memory.commit_memory_write`
只负责把这里的判定翻译成 SQL。

三条判据，**都不采信模型自称**：

1. `confidence_for_source` —— 置信度由**写入身份**推出。模型自评置信度是它自己的输出，
   拿它当「谁能覆盖谁」的判据 = 让写入方给自己发通行证（tech-design §8b）。
2. `can_overwrite` —— 四格裁决表。唯一被拒的一格是 `low → high`：
   后台推断永远不能悄悄改掉用户亲口说的（「误覆盖比多一条更伤」，PRD §7）。
3. `normalize_for_dedup` / `is_near_duplicate` —— 逐字判重（确定性，命中就 bump
   `last_confirmed_at` 不新建）与近重复提示（**只点名不自动合并**）。
"""

from __future__ import annotations

import json
import unicodedata

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

MEMORY_TYPE_PROCEDURAL = "procedural"
MEMORY_TYPE_SEMANTIC = "semantic"
#: Unknown / absent always uses `semantic`; fail-safe means "never expires or loses weight" (after r3 removed
#: `episodic`, both types share injection/forgetting policies, so misclassification has no hard cost; tech-design §6.1).
DEFAULT_MEMORY_TYPE = MEMORY_TYPE_SEMANTIC

#: Token Jaccard ≥ this ⇒ possible duplicate. **Hint only**: automatic merging can silently swallow a
#: truly new preference, worse than retaining an extra entry (tech-design §4.2 d-3).
NEAR_DUPLICATE_THRESHOLD = 0.8

#: Writes initiated directly by user/UI. All others get `low`, including unknown sources:
#: fail-safe means "no high-confidence overwrite rights", not "overwrite by default".
_HIGH_CONFIDENCE_SOURCES = frozenset(
    {
        "user_directive",  # `remember` tool: explicit request in this turn.
        "user_explicit",  # Manual creation on /memory page (POST /memory/user/remember).
        "user_edit",  # Manual edit on /memory page.
    }
)

#: ★ `feedback_confirmed` (track B batch 1a zero-click direct writes) is **deliberately absent above**,
#: receiving `low`: an **intentional deviation** from tech-design §1.2 "confidence=high".
#:
#: §1.2 justified high as "avoid MemoryConfidenceConflictError when hitting a manually written high entry
#: and rolling back the whole transaction". 2026-09-08 closing review established:
#: **that justification does not apply here**: only `personal_memory_governance` raises that exception;
#: `commit_memory_write` **returns `action="blocked"`**; direct writes cannot encounter it.
#: The **actual effect** of high is opposite: `can_overwrite(high, high)` is true ⇒
#: an automatic write with LLM-drafted content and key can **overwrite a user's manually written memory entry**
#: (a mistaken colliding key suffices), retaining one old version only in `previous_value`.
#: With `low`, the adjudication table blocks it: precisely why this table exists.
#:
#: ⇒ Use `low`: it updates its own class, but never overwrites user-authored entries.
#: Cost: collisions with high return `blocked` (caller must acknowledge truthfully, never claim written).

# Stopword removal serves Jaccard only (exact dedup retains them; exact means exact). Keep this list short:
# more stopwords make **different** preferences more likely to appear near-duplicate and refer to each other.
_STOPWORDS = frozenset(
    {
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "都",
        "要",
        "请",
        "把",
        "a",
        "an",
        "the",
        "to",
        "of",
        "for",
        "and",
        "or",
        "in",
        "on",
    }
)


def confidence_for_source(source: str | None) -> str:
    """写入身份 → 置信度。不看模型自称的任何字段。"""
    return CONFIDENCE_HIGH if (source or "") in _HIGH_CONFIDENCE_SOURCES else CONFIDENCE_LOW


def normalize_confidence(raw: object) -> str:
    value = str(raw or "").strip().lower()
    return value if value in (CONFIDENCE_HIGH, CONFIDENCE_LOW) else CONFIDENCE_LOW


def normalize_memory_type(raw: object) -> str:
    value = str(raw or "").strip().lower()
    return value if value in (MEMORY_TYPE_PROCEDURAL, MEMORY_TYPE_SEMANTIC) else DEFAULT_MEMORY_TYPE


def can_overwrite(*, new_confidence: str, existing_confidence: str | None) -> bool:
    """四格裁决表。`existing_confidence=None`（存量未标注）按 `low` 待遇——
    存量行没有「用户亲口说的」这层证据，不该凭空获得挡住新写入的权力。"""
    new = normalize_confidence(new_confidence)
    existing = normalize_confidence(existing_confidence)
    return not (new == CONFIDENCE_LOW and existing == CONFIDENCE_HIGH)


#: These symbols **change meaning**, not typography: removing them equates opposite memories.
#: Actual counterexample: `毛利率 ≥ 30%` and `毛利率 ≤ 30%` both normalize to `毛利率30` ⇒ exact duplicate ⇒
#: the new value **never gets written**, yet acknowledgment says "remembered". Same family: `+5%` vs `-5%`,
#: `预算 > 100 万` vs `< 100 万`.
#: (Previously only punctuation between two digits was protected, covering `5.5` vs `55`, not leading symbols.)
_SEMANTIC_SYMBOLS = frozenset("<>≤≥≠=+±%*/^~")


def _is_digit(ch: str) -> bool:
    return ch.isdigit()


def _normalize(text: object, *, keep_spaces: bool) -> str:
    """NFKC（全半角）→ casefold（大小写）→ 去标点，空白按 `keep_spaces` 处理。

    ★ **数字之间的标点不去**：千分位与小数点在数据里兼职（`5.5` 与 `55` 不是一回事，
    `2026-08-29` 与 `20260829` 也不必强行判等）。代价是 `1,000` 与 `1000` 判不出相等
    ——那只是少合并一条，方向与本 sprint 的风险取向（误覆盖比多一条更伤）一致。
    """
    raw = unicodedata.normalize("NFKC", str(text or "")).casefold()
    out: list[str] = []
    for i, ch in enumerate(raw):
        if ch.isspace():
            if keep_spaces:
                out.append(" ")
            continue
        category = unicodedata.category(ch)
        if category.startswith(("P", "S")):
            prev_ch = raw[i - 1] if i > 0 else ""
            next_ch = raw[i + 1] if i + 1 < len(raw) else ""
            if ch in _SEMANTIC_SYMBOLS or (_is_digit(prev_ch) and _is_digit(next_ch)):
                out.append(ch)
            elif keep_spaces:
                # Punctuation separates tokens; dedup removes it entirely (see normalize_for_dedup).
                out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


def normalize_for_dedup(text: object) -> str:
    """逐字判重用的规范化形态：连空白一起去掉，`排名图用 Top 10。` == `排名图用Top10`。"""
    return _normalize(text, keep_spaces=False)


def comparable_text(value_json: object) -> str:
    """一条记忆存的 JSON → **可比较文本**：字符串取其内容，其余按 key 排序序列化。

    判重比的是这个而不是原始 JSON 串 —— 否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}`
    这种同值不同键序的写入会被当成两条不同的记忆。两条写路（`remember` / 候选 approve）
    共用本函数：**判据只有一份**，否则两条路会对「算不算同一条」慢慢给出不同答案。
    """
    try:
        parsed = json.loads(str(value_json))
    except (json.JSONDecodeError, TypeError):
        return str(value_json or "")
    if isinstance(parsed, str):
        return parsed
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


def is_verbatim_duplicate(left: object, right: object) -> bool:
    left_norm = normalize_for_dedup(left)
    return bool(left_norm) and left_norm == normalize_for_dedup(right)


def _is_cjk(ch: str) -> bool:
    # U+4E00–U+9FFF Han · U+3040–U+30FF Kana (write code points in source, not literal characters:
    # literal range endpoints hide their code points in editor/terminal; one mistaken edit gives no warning).
    return "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"


def dedup_tokens(text: object) -> frozenset[str]:
    """Jaccard 用的 token 集：拉丁/数字按空白与标点切词，CJK 按**单字**切。

    ⚠ 分词**不能**复用 `normalize_for_dedup`——它把空白也去掉了，整句英文会缩成一个
    巨大的 token，两句只差一个 `the` 的英文偏好相似度会算成 **0.0**（真踩过）。

    CJK 按单字而不是 bigram：在真实偏好串上量过两种切法，单字把「该命中的换措辞」
    与「不该命中的换指标」正好分在 0.8 两侧（`一律`→`都` = 0.800 命中；
    净利率 vs 毛利率 = 0.778 不命中），bigram 则把两边一起压到 0.6 上下 ⇒ 判据恒不触发、
    等于没做。判据上线前在真串上空跑过一遍，这就是那次空跑的结论。
    """
    tokens: set[str] = set()
    run: list[str] = []

    def _flush() -> None:
        if run:
            tokens.add("".join(run))
            run.clear()

    for ch in _normalize(text, keep_spaces=True):
        if _is_cjk(ch):
            _flush()
            tokens.add(ch)
        elif ch.isalnum():
            run.append(ch)
        else:
            _flush()
    _flush()
    return frozenset(t for t in tokens if t not in _STOPWORDS)


def jaccard(left: object, right: object) -> float:
    a = dedup_tokens(left)
    b = dedup_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_near_duplicate(left: object, right: object, *, threshold: float = NEAR_DUPLICATE_THRESHOLD) -> bool:
    """近重复**提示**判据。命中只意味着「点名疑似」，绝不意味着自动合并。"""
    return jaccard(left, right) >= threshold


def near_duplicate_matcher(text: object, *, threshold: float = NEAR_DUPLICATE_THRESHOLD):
    """把 `text` 的 token 集**算一次**，返回一个可以反复问的判定器。

    `commit_memory_write` 要拿新写入的文本去比该用户的每一条记忆，而且这段循环跑在
    `BEGIN IMMEDIATE` 里（握着整库写锁）。直接调 `is_near_duplicate` 会对**每一行**
    重算一遍新文本的 NFKC+casefold+分词 —— 200 条记忆就是 200 遍，全在锁里。
    """
    base = dedup_tokens(text)

    def _match(other: object) -> bool:
        if not base:
            return False
        candidate = dedup_tokens(other)
        if not candidate:
            return False
        return len(base & candidate) / len(base | candidate) >= threshold

    return _match


__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "DEFAULT_MEMORY_TYPE",
    "MEMORY_TYPE_PROCEDURAL",
    "MEMORY_TYPE_SEMANTIC",
    "NEAR_DUPLICATE_THRESHOLD",
    "can_overwrite",
    "comparable_text",
    "confidence_for_source",
    "dedup_tokens",
    "is_near_duplicate",
    "is_verbatim_duplicate",
    "jaccard",
    "near_duplicate_matcher",
    "normalize_confidence",
    "normalize_for_dedup",
    "normalize_memory_type",
]
