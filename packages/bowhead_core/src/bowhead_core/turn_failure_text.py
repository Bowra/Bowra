"""轮次失败时的用户面：这段文字给谁看、这一轮的产物留不留。

抽出来不是为了复用，是为了**能跑**：两处原本都埋在几千行的函数里，
`run_subagent` 与 `_lead_emit` 的完整 harness 太重，于是这条链路上一个
断言都没有——而它决定用户在失败时看到的是人话还是一串内部指令。
"""

from __future__ import annotations

#: Peer→Lead fixed prefix. Tool description requires insufficient_data: <reason> if unable;
#: _LEGACY_RESULT_ERROR_PREFIX_TO_PUBLIC_CODE classifies by it, immutable.
INSUFFICIENT_DATA_PREFIX = "insufficient_data: "


def cap_bail_final_answer(profile_name: str, cap_reason: str) -> str:
    """撞到 wallclock / 失败次数硬闸时，`SubagentResult.final_answer` 该写什么。

    peer 的这段有 **Lead（模型）** 读，写协议串是对的。
    Lead 自己的这段**上面没有消费者——它就是用户看到的那句话**。

    116 dogfood 2026-09-24 `t_7f66c0d681c6`：Lead 跑了 1171s 撞上 900s 的闸，
    这串给模型的指令被 silent-done 兜底原样当成终答渲进对话——

        insufficient_data: peer_wallclock_exhausted: 1171s > 900s.
        Stop iterating — return what we have. 已停止迭代以节省用户时间.

    The Lead side returns an empty string: the failure card states the failure from the public
    reason, and classification reads `SubagentResult.error` (`_public_turn_failure_reason`).
    """
    if profile_name == "lead":
        return ""
    return INSUFFICIENT_DATA_PREFIX + cap_reason + " 已停止迭代以节省用户时间."


def choose_silent_done_text(
    carried: str | None,
    *,
    status: str,
    empty_hint: str,
    has_public_failure: bool,
) -> str | None:
    """No ``final_answer`` was emitted this turn: which fallback answer, if any, to publish.

    - Failure terminal with a public reason (``has_public_failure``): ``None``. The failure card,
      driven by ``reason_code``, is the turn's only failure statement (chat turn feedback D3). A
      carried system failure sentence is dropped as well; model content that already streamed as
      ``narrative.delta`` is untouched.
    - Failure terminal without a public reason: the carried text when present, else ``None``. No
      generic failure prose is written by the system any more.
    - Any other status: the carried text when present (an upstream may deliberately place a
      user-facing sentence there), else the localized ``empty_hint`` for a turn with nothing to show.
    """
    failure = status in TERMINAL_FAILURE_STATUSES
    if failure and has_public_failure:
        return None
    if isinstance(carried, str) and carried.strip():
        return carried
    return None if failure else empty_hint


#: Done failure statuses: **both forms in use**;
#: turn_runner uses TurnStatus.FAILED.value ("failed"), Lead synthetic done uses "error".
#: Recognizing one silently misses half the cases.
TERMINAL_FAILURE_STATUSES = frozenset({"error", "failed"})


def keep_sections_visible_on_failure(status: str | None, response_kind: str) -> bool:
    """这一轮判成 chat（本轮 section 本该被隐藏），但轮次失败了——还隐藏吗？

    不隐藏。隐藏的**前提**是「这些图会内联进对话气泡」：chat 模式下画布让位给对话。
    轮次失败时对话侧根本没渲染过终答，前提不成立，于是两边都没有——

    116 dogfood 2026-09-24 `t_7f66c0d681c6`：画了 2 张图 + 2 段代码，4 个 section
    全被打上 `hidden_for_chat_mode=1`（理由 `chat-mode (no insight/recs/exec-narrative)`），
    而那一轮撞 wallclock 失败 ⇒ 图既不在画布也不在对话，**用户完全够不着**，
    20 分钟的取数与出图白跑。

    与 `_chat_unrenderable_kinds` 的「翻案补发」是同一个形状：早先的判定基于一个
    后来不成立的前提，就得撤回，而不是让产物凭空消失。
    """
    return response_kind == "chat" and (status or "") in TERMINAL_FAILURE_STATUSES
