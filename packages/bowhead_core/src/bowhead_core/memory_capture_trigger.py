"""第一层：零成本关键词族粗筛（B 线批 1a ＝ 记忆二期 FR5 ①）。

判「用户这句话像不像一条**长期**指令」。命中才允许后面那条自动记录路走；
未命中什么都不发生（零 LLM 增量）。

★ **它不是判据，是授权前置。** 第二层判型由 `auto_learner` 的 LLM 出，
而 LLM 的产出**不能自己给自己发通行证**：没有本层这条确定性命中，
哪怕分类器喊 `modification_request` 也不许走免确认直写。
同一条纪律见写入闸的 `commit_memory_write`「置信度由写入身份决定，调用方无法传入」。

★ **纯函数、无 IO**，与 `memory_write_gate` 立的模一致（那边 docstring 写了理由）。
词表**不另抄一份**：语言族与一次性族都从 `memory_write_gate` 导入同一份实现 ——
「词表只有一份」这条在本仓已经栽过（`combined_text` docstring 记着入队/approve
两处取文本方式不同、dict 值的 repr 骗过密钥扫描那次）。

★ **判定顺序即正确性**（与 `memory_write_gate.check` 同一条纪律）：
① 语言族**先判** —— 「以后都用英文回答」是**设置**不是记忆，走 `user_language` 表单；
   它同时命中长期词族（「以后都」），不先判就会被自动写进记忆，
   而写进去下次会话根本不生效（写入闸第一闸拒的就是这个，见 `REASON_LANGUAGE_IS_A_SETTING`）。
② 一次性族**其次** —— 「这次先只看华东」含「先」但不是长期指令；
   同时含两族时（「以后都要详细，但这次先简单」）语义本身矛盾，
   一律判 `NONE`：自动写入是**有副作用**的动作，模棱两可时不做才是对的方向。
③ 剩下的才判长期族。
"""

from __future__ import annotations

from bowhead_core.memory_write_gate import looks_like_language_preference, one_off_markers

#: Does not resemble a persistent instruction; nothing happens.
DECISION_NONE = "none"
#: Language family → `user_language` confirmation form; **no memory or candidate-pool write**.
DECISION_LANGUAGE_SETTING = "language_setting"
#: Explicit persistent instruction → permit second-layer classification.
DECISION_PERSISTENT = "persistent_instruction"

#: Persistent-instruction keyword family (FR5 ①). **Optimize recall, not precision**; only admits the second layer,
#: where `auto_learner` classifies and the write gate actually prevents writes.
#: ⚠ Still an **authorization prerequisite for automatic writes**, so missed admissions (user can repeat) are preferable
#: to treating questions as instructions. Deliberately **exclude** bare words such as `默认`:
#: `系统默认值是多少` is a question, although §4.7 lists its keyword. Do not add before dry-running real data.
#:
#: ★ **These three keywords were removed after a real-data dry run; never casually restore them** (2,087 real 116 utterances, 2026-09-08):
#: - `长期`: alone contributed 6 false positives among 18 hits, **all the same business query**,
#: `短期已解决但是**长期**待确认的订单数量`; in Chinese business contexts it is a noun
#: (`长期待确认` / `长期资产` / `长期趋势`), not a time horizon.
#: - `别再` / `不要再`: captured **current-turn corrections** (`简单列一下就行，别再出报告了` /
#: `口径已定，不要再问我`), not persistent instructions. Genuine persistent negatives are covered by `以后别`
#: / `以后不要`, so removing them barely reduces recall.
#: ⚠ Invented strings cannot validate this: 35 synthetic dry-run strings had 0 mistakes, while real data had widespread false positives.
_PERSISTENT_MARKERS: tuple[str, ...] = (
    # Chinese: imperative + time horizon
    "以后都",
    "以后请",
    "以后不要",
    "以后别",
    "从今以后",
    "从现在起",
    "从now on",
    "每次都",
    "一律",
    "统一改成",
    "默认改成",
    "默认用",
    "记住",
    "记一下",
    # English
    "from now on",
    "always ",
    "never ",
    "going forward",
    "by default",
    "stop doing",
    "remember that",
    "remember to",
)


#: ★★ **This layer's own temporary-keyword list**, on top of the write gate's list.
#:
#: Why not rely only on write gate `_ONE_OFF_MARKERS`: it was tuned **in the opposite direction**.
#: For the write gate, missing a one-off only stores an extra entry; rejecting a real preference is worse, so
#: it deliberately removed ambiguous terms such as `暂时` / `这段`.
#: Here, missing a one-off means **automatically writing permanent memory**, reversing the cost.
#: Observed counterexample (2026-09-08 consolidation review): `记住，暂时都用横向条形图`:
#: `记住` matches; write gate misses `暂时` ⇒ persistent instruction ⇒ permanent preference written with zero clicks.
#: ⇒ **Share implementation, not tuning**. Before merging these lists, decide whose failure costs should determine tuning.
_TEMPORARY_MARKERS: tuple[str, ...] = (
    "暂时",
    "临时",
    "这段时间",
    "这几天",
    "这阵子",
    "先这样",
    "目前先",
    "for now",
    "temporarily",
    "for the time being",
)


def temporary_markers(text: str) -> tuple[str, ...]:
    """命中的临时性词（本层专用，见上方为什么不与写入闸共用调参）。"""
    low = str(text or "").lower()
    return tuple(m for m in _TEMPORARY_MARKERS if m in low)


def persistent_markers(text: str) -> tuple[str, ...]:
    """命中的长期词（返回全部命中，便于空跑报告与用例断言）。"""
    low = str(text or "").lower()
    return tuple(m for m in _PERSISTENT_MARKERS if m in low)


def classify_user_message(text: str) -> tuple[str, tuple[str, ...]]:
    """粗筛一条用户消息，返回 `(决定, 命中的词)`。

    ⚠ 顺序见模块 docstring —— 改这三个 `if` 的先后就是改正确性，不是改风格。
    """
    body = str(text or "")
    if not body.strip():
        return DECISION_NONE, ()

    # ① Judge language family first: setting, not memory.
    if looks_like_language_preference(body):
        return DECISION_LANGUAGE_SETTING, ()

    # ② One-off/temporary family: its presence rules out persistence, even alongside persistent-family matches
    # (ambiguous → do nothing). Check both lists: write gate's shared implementation and
    # this layer's `_TEMPORARY_MARKERS`, added according to its own failure costs (see explanation above).
    if one_off_markers(body) or temporary_markers(body):
        return DECISION_NONE, ()

    hits = persistent_markers(body)
    return (DECISION_PERSISTENT, hits) if hits else (DECISION_NONE, ())


__all__ = [
    "DECISION_LANGUAGE_SETTING",
    "DECISION_NONE",
    "DECISION_PERSISTENT",
    "classify_user_message",
    "persistent_markers",
    "temporary_markers",
]
