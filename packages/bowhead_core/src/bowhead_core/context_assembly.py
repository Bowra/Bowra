"""按波动率装配 system —— 上下文工程 sprint 批 2-a。

## 为什么顺序就是全部

线上这批 provider 做的是**自动前缀缓存**：不认 `cache_control` 断点，只按「从第一个字节起
逐字节相同的那一段」命中。所以 prompt 的**物理顺序**就是缓存策略本身，没有第二个旋钮。
缓存读远低于全价 ⇒ 把稳定内容排到前面是唯一的一阶杠杆
（`turn-latency` sprint 靠这一条把真实命中率 31.7% → 67.0%）。

## 改前的顺序（`subagent.py`），按波动率标注

```
1 profile.system_prompt   每团队      ← 稳
2 extra_instructions      每次 spawn  ← 变   ⚠ 排在 4/5 之前
3 clarify 假设            每 turn     ← 变   ⚠
4 ws_meta                 每工作区/天
5 <remembered_about_user> 每用户
6 语言 directive          每 turn     ← 变
```

第 2、3 段一旦非空，**它后面的所有内容在跨 turn 维度上全部作废** —— 包括
`ws_meta`（datarc 全部 measures + dimensions，量级可达数 k 字符）和记忆块。
两个并发 peer 的 system 也在第 2 段就分叉，谁也不共享后面那几 k。

（2026-10-17 起第 3、4 段已删除：datarc 的数据只经工具到模型，不再预注入提示词。）

## ⚠ Lead 是例外：它的 `extra_instructions` 就是整份提示词

`lead_orchestrator` 调 `runner.run(profile_name="lead", extra_instructions=lead_prompt)`，
而 Lead 的 `profile.system_prompt` 是**空串** —— 也就是说 Lead 那 11.6k 的提示词整个
走的是 `extra_instructions` 通道。把它当「per-spawn 易变段」搬进尾巴，等于把全系统最大的
稳定块搬出可缓存前缀，与本批的目标正好相反。

所以调用方要用 `extra_is_stable=True` 明确声明这一档（`profile.name == "lead"`）。
这个参数**不是**为 Lead 开的后门，而是「这段文本的波动率是多少」这个信息只有调用方知道 ——
同一个通道，peer 传的是每次 spawn 都不同的子任务指令，Lead 传的是每团队才变一次的提示词。

## 这笔账的取舍（A/B 要量的就是它）

**失去**：peer 的 `extra_instructions` 在**同一 turn 内**逐圈不再被缓存（每圈全价重发，量级几百 token）。
**换来**：同一 turn 内并发的多个 peer、以及跨 turn 的 peer，共享 `[profile + ws_meta + 记忆]` 这一大段。
ws_meta 单独就可能是数 k —— 这笔账应当是正的，但**要用 peer profile 的命中率证明，不是推理**。

⚠ 段落内容**一个字节都不改**，只换位置。否则 A/B 里分不清是「换了位置」还是「换了措辞」造成的差异
（`turn-latency` 实施 A 的既定纪律）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bowhead_core.context_prefix_flags import prefix_order_enabled
from bowhead_core.token_estimator import estimate_tokens


@dataclass(frozen=True, slots=True)
class SystemSplit:
    """`(stable_system, turn_tail, turn_tail_language)`。空串表示该段不存在。

    ★ 语言段单独拆出来（2026-09-09）：它是**指令**，要吃最末位的 recency，
    必须排在 `<available_refs>` **之后**；`turn_tail` 的其余部分是上下文，仍排在 refs 之前。
    同一条道理在 hard-bail 分支里早就写着（"指令靠最末位置吃 recency，refs 只是上下文"），
    只是语言段一直没享受同等待遇 —— 116 实测：Lead 的语言指令后面还跟着几千字符的
    refs 清单（里面绝大多数是中文实体名），而它恰恰在这一格上不听话。
    """

    stable_system: str
    turn_tail: str
    turn_tail_language: str = ""


def _append_legacy(base: str, block: str) -> str:
    """复刻改前的拼接语义：块非空就 ``base + "\\n\\n" + block``。

    ⚠ **无条件加分隔符**，哪怕 ``base`` 是空串 —— 原码就是
    ``system = f"{system}\\n\\n额外指令：\\n{...}"``，而 Lead 的 ``profile.system_prompt``
    恰好是空串，于是它的 system 真的以两个换行开头。
    回滚绳要逐字节成立，这个细节不能用 ``join`` 抹平（第一版实现就是这么错的）。
    """
    return base + "\n\n" + block if block else base


def split_system_for_prefix(
    *,
    profile_prompt: str,
    extra_instructions_block: str,
    memory_block: str,
    language_block: str,
    extra_is_stable: bool = False,
    turn_rules_block: str = "",
    turn_context_block: str = "",
    capability_block: str = "",
) -> SystemSplit:
    """把各段按波动率分成「稳定前缀」与「本轮尾巴」。

    每个参数都是**已经渲染好的整段文本**（含各自的标题行），空串表示该段不存在。
    调用方只负责取值与声明波动率，不负责拼接顺序——顺序是这个函数的全部职责。

    Args:
        extra_is_stable: ``extra_instructions_block`` 是否属于稳定档。Lead 传 ``True``
            （它那一路的 ``extra_instructions`` 就是整份 system prompt，见模块 docstring）。
        capability_block: 本轮工具面推出的**能力协议**（怎么用 datarc 取数 / 怎么交付到画布）。
            属**最稳定**一档：只随工具白名单变，同一个 profile 每轮逐字节相同，因此紧跟
            ``profile_prompt``、在记忆块之前。位置遵循既有的「身份先于规则」
            （review B2-R5：别让 prompt 以规则开场）。空串 = 本轮没有命中的协议。

    flag OFF 时返回改前的拼法（各段依原序拼进 ``stable_system``，``turn_tail`` 为空）。

    ⚠ ``language_block`` 有一条既有的「必须在最末」尾部契约。放进 ``turn_tail`` 后它仍是最末
    （而且更靠近生成点），契约不破；``subagent.py`` 里那句注释已同步改写。
    """
    if not prefix_order_enabled():
        out = profile_prompt
        # Legacy assembly also puts protocol immediately after identity; empty (default) is byte-for-byte unchanged.
        out = _append_legacy(out, capability_block)
        for block in (extra_instructions_block, memory_block):
            out = _append_legacy(out, block)
        # Batch 3 rules (nonempty only when flag ON) go at legacy system's end; empty preserves bytes.
        if turn_rules_block:
            out = _append_legacy(out, turn_rules_block)
        if turn_context_block:
            out = _append_legacy(out, turn_context_block)
        # Original language block uses **direct `+`** (includes leading newlines), not `+ "\n\n" +`.
        return SystemSplit(stable_system=out + language_block, turn_tail="", turn_tail_language="")

    stable = profile_prompt
    stable = _append_legacy(stable, capability_block)
    if extra_is_stable:
        stable = _append_legacy(stable, extra_instructions_block)
    stable = _append_legacy(stable, memory_block)

    tail = ""
    # Batch 3: session continuation rules (per-session, more stable than per-spawn/per-turn) lead the tail block.
    tail = _append_legacy(tail, turn_rules_block)
    # r5: current-turn inventory (pinned datasets / attachments / clarification candidate hints), snapshotted at turn start,
    # constant within a turn, always different across turns. On 116, appending it to lead_prompt's stable prefix added 450 characters per turn,
    # causing all history after system to miss cache each turn. Move to tail.
    tail = _append_legacy(tail, turn_context_block)
    if not extra_is_stable:
        tail = _append_legacy(tail, extra_instructions_block)
    # **Do not** merge language into tail anymore; caller places it after refs (see SystemSplit docstring).
    return SystemSplit(
        stable_system=stable,
        turn_tail=tail.lstrip("\n"),
        turn_tail_language=language_block.lstrip("\n"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Batch 4-b: window guard, the only hard limit and a last resort
#
# Similar form but opposite semantics to ψ-A removed in batch 1: ψ-A cleared at 30k; this clears only to avoid failure.
# Trigger follows the model's **actual window** (batch 4-a reads catalog), never a fixed token count. 1M models never
# reach it; small-window models are rescued. Rewriting the message middle inherently invalidates prefix cache, so act only
# when no rewrite means 400; normal path never triggers (§7 criterion 5: count 0 under 1M configuration).
# ─────────────────────────────────────────────────────────────────────────────

WINDOW_GUARD_RATIO = 0.9
# After triggering, clear to this fraction of the threshold rather than barely under it; otherwise every iteration clears one more result and invalidates prefix again (review fix).
WINDOW_GUARD_HYSTERESIS = 0.8
_CLEARED_PREFIX = "[已移出上下文"


def cleared_marker(tokens: int) -> str:
    """被清掉的工具结果留下的占位。

    review 修：原文案「see ledger」是悬空指针——模型没有任何能读「账本」的工具，看到它只会把同一个工具再跑一遍，
    新结果下一圈又是最老的、再被清，直到迭代上限。占位要说清「别重跑、沿用上文结论」。
    """
    return f"{_CLEARED_PREFIX}以适配模型窗口：{tokens} tokens 的工具结果。不要重跑同一个工具；沿用上文已经得出的结论。]"


def is_cleared(content: Any) -> bool:
    return isinstance(content, str) and content.startswith(_CLEARED_PREFIX)


_is_cleared = is_cleared


def _args_len(message: Any) -> int:
    n = 0
    for tc in getattr(message, "tool_calls", None) or []:
        args = getattr(tc, "arguments", None)
        if isinstance(args, str):
            n += len(args)
        elif args:
            n += len(str(args))
    return n


def _tokens_of(message: Any, estimate: Callable[[str], int]) -> int:
    content = getattr(message, "content", "") or ""
    n = estimate(content) if isinstance(content, str) else 0
    # Assistant tool_calls arguments also consume window; rough JSON length suffices (guard needs only an upper bound).
    for tc in getattr(message, "tool_calls", None) or []:
        args = getattr(tc, "arguments", None)
        if isinstance(args, str):
            n += estimate(args)
        elif args:
            n += estimate(str(args))
    return n


def guard_window(
    messages: list[Any],
    *,
    budget_tokens: int,
    extra_tokens: int = 0,
    estimate: Callable[[str], int] = estimate_tokens,
    hysteresis: float = WINDOW_GUARD_HYSTERESIS,
    protect_first: int = 0,
    cleared_ids: list[str] | None = None,
) -> int:
    """估算 ``extra_tokens + messages`` 超过 ``budget_tokens`` 时，**就地**把最老的工具结果
    换成 ``cleared_marker``，直到降到线下。返回清掉的 token 数（0 = 没动）。

    规则（只有这几条，别加）：
    - 只清 ``role == "tool"`` 的消息；user / assistant 一律不动；
    - **不清最后一条 assistant 之后的工具结果**——那是模型本圈还没消化的结果，清了它
      模型只会再调一次同一个工具、再撞一次窗口；
    - 已经是占位的不重复清；
    - 前 ``protect_first`` 条（turn 级历史）不清——历史里的 `subagent_spawn` 摘要本来就短，占位文案
      「不要重跑同一个工具」对它读起来是「别再派活」（review 修）；历史超线由第二层按整轮掉；
    - 清完仍超线就停（返回已清的量）：估算是上界，真发出去未必 400；
      真 400 了由批 4-c 的 CONTEXT_OVERFLOW 重试再收一次。

    ``budget_tokens <= 0`` 视为「窗口未知」，直接返回 0。调用方按 ``window × WINDOW_GUARD_RATIO`` 给线。
    When given, ``cleared_ids`` collects the ``tool_call_id`` of each cleared message.
    """
    if budget_tokens <= 0 or not messages:
        return 0
    # Cheap precheck: at most 4 tokens per character under cl100k (UTF-8 byte fallback);
    # if characters × 4 stays under threshold, avoid encoder cost. Only Lead-peak scale needs actual estimation.
    # (Review fix: include tool_calls arguments in upper bound too, using the same measure as actual estimation below.)
    char_upper = extra_tokens + 4 * sum(len(getattr(m, "content", "") or "") + _args_len(m) for m in messages)
    if char_upper <= budget_tokens:
        return 0

    per_msg = [_tokens_of(m, estimate) for m in messages]
    total = extra_tokens + sum(per_msg)
    if total <= budget_tokens:
        return 0

    # Once triggered, clear to hysteresis × threshold in one pass, not one result per iteration near threshold.
    target = int(budget_tokens * hysteresis)
    last_assistant = max((i for i, m in enumerate(messages) if getattr(m, "role", None) == "assistant"), default=-1)
    cleared = 0
    for i, m in enumerate(messages):
        if total <= target:
            break
        if i > last_assistant:
            break
        if i < protect_first:
            continue
        if getattr(m, "role", None) != "tool" or _is_cleared(getattr(m, "content", None)):
            continue
        n = per_msg[i]
        marker = cleared_marker(n)
        replaced = estimate(marker)
        if n <= replaced:
            continue  # Clearing results shorter than the placeholder would increase size (review fix)
        m.content = marker
        total -= n - replaced
        cleared += n
        if cleared_ids is not None:
            cleared_ids.append(getattr(m, "tool_call_id", None) or "")
    return cleared


def drop_history_turns(
    messages: list[Any],
    *,
    turn_sizes: list[int],
    budget_tokens: int,
    extra_tokens: int = 0,
    estimate: Callable[[str], int] = estimate_tokens,
    hysteresis: float = WINDOW_GUARD_HYSTERESIS,
) -> tuple[int, int]:
    """守卫的第二层（review 修 / r4 改按轮）：工具结果清完仍超线时，从头掉最老的**整轮**历史。

    历史是批 3 挂在 messages 最前的若干轮，``turn_sizes[i]`` 是第 i 轮占的消息数（r4 起一轮可以是
    ``user → assistant(tool_calls) → tool… → assistant``，不再恒为 2）。它不是 tool，第一层碰不到，
    而它是唯一能在小窗模型上把 Lead 撑爆又没人能收的东西。掉整轮、不切轮内，和 build_turn_history 同一条纪律。
    **就地**从 ``turn_sizes`` 头部弹出被掉的轮。返回 ``(dropped_turns, dropped_tokens)``；
    没有历史或 ``budget_tokens <= 0`` 时 ``(0, 0)``。
    """
    if not turn_sizes or budget_tokens <= 0 or not messages:
        return (0, 0)
    # Same cheap precheck as guard_window (review fix: otherwise Lead runs tiktoken over all messages every iteration, observed 60ms).
    char_upper = extra_tokens + 4 * sum(len(getattr(m, "content", "") or "") + _args_len(m) for m in messages)
    if char_upper <= budget_tokens:
        return (0, 0)
    total = extra_tokens + sum(_tokens_of(m, estimate) for m in messages)
    if total <= budget_tokens:
        return (0, 0)
    target = int(budget_tokens * hysteresis)
    dropped = 0
    dropped_tokens = 0
    while turn_sizes and total > target:
        size = max(0, int(turn_sizes[0]))
        if size <= 0 or size > len(messages):
            break
        turn_tokens = sum(_tokens_of(m, estimate) for m in messages[:size])
        del messages[:size]
        turn_sizes.pop(0)
        total -= turn_tokens
        dropped += 1
        dropped_tokens += turn_tokens
    return (dropped, dropped_tokens)


def drop_history_pairs(
    messages: list[Any],
    *,
    history_pairs: int,
    budget_tokens: int,
    extra_tokens: int = 0,
    estimate: Callable[[str], int] = estimate_tokens,
    hysteresis: float = WINDOW_GUARD_HYSTERESIS,
) -> tuple[int, int]:
    """``drop_history_turns`` 的整对形态（每轮恒 2 条），保留给只有 (user, assistant) 对的调用方。"""
    if history_pairs <= 0:
        return (0, 0)
    sizes = [2] * history_pairs
    return drop_history_turns(
        messages, turn_sizes=sizes, budget_tokens=budget_tokens, extra_tokens=extra_tokens, estimate=estimate, hysteresis=hysteresis
    )


def estimate_messages_tokens(messages: Sequence[Any], *, estimate: Callable[[str], int] = estimate_tokens) -> int:
    """给 4-c 重试用的同源估算（与守卫算法同一把尺）。"""
    return sum(_tokens_of(m, estimate) for m in messages)


def tool_schemas_tokens(schemas: Sequence[Any], *, estimate: Callable[[str], int] = estimate_tokens) -> int:
    """tools 定义占的窗口（render order 位置 0，每次请求都带）。

    收 `ToolSchema` 数据类、dict、或任何带 name/description/input_schema 的对象；
    ⚠ 别直接 `json.dumps(schemas)`——`ToolSchema` 是 dataclass，会抛 TypeError，
    兜成 0 就是「写着会算其实没算」（第一版正是这么错的）。
    """
    rows: list[dict[str, Any]] = []
    for sc in schemas or ():
        if isinstance(sc, dict):
            rows.append(sc)
        else:
            rows.append(
                {
                    "name": getattr(sc, "name", ""),
                    "description": getattr(sc, "description", ""),
                    "input_schema": getattr(sc, "input_schema", None),
                }
            )
    if not rows:
        return 0
    try:
        return estimate(json.dumps(rows, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return estimate(str(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Batch 3: turn-level history replaces digest's **content** part
#
# Verbatim user message + full final answer per turn, append-only before current task, absorbed by prefix cache.
# Replace only digest content; digest rules and per-turn metadata enter tail via `render_history_rules_block`.
# ─────────────────────────────────────────────────────────────────────────────


_HISTORY_TOKENS_MEMO: dict[str, int] = {}
_HISTORY_TOKENS_MEMO_CAP = 50_000


@dataclass(frozen=True, slots=True)
class TurnHistory:
    messages: list[Any]
    turns_used: int
    turns_dropped: int
    tokens: int
    # r4: messages per turn (guard layer 2 drops whole turns by this; `hist_n` log = sum).
    turn_sizes: list[int] = field(default_factory=list)
    # Incomplete turns also retain clarification context without requiring a digest.
    incomplete_turns: int = 0


SPAWN_TOOL_NAME = "subagent_spawn"

# r7 (116 dogfood round 5): include incomplete turns: users **saw their own message**, so their next request to change quarter
# refers to it. Previously completed-only filtering erased cancelled turns and made Lead claim a new session (116 `s_88c72107685c`).
# Render fixed markers for unfinished Turns. Clarification cards use a separate
# data-only projection; running and completed-but-empty Turns remain excluded.
INCOMPLETE_TURN_MARKERS: Mapping[str, str] = {
    "cancelled": "[本轮被用户中止，未完成 / cancelled by the user before completion]",
    "aborted": "[本轮被中止，未完成 / interrupted before completion]",
    "failed": "[本轮执行失败，未完成 / failed before completion]",
}


def incomplete_turn_marker(status: str, error_kind: str | None = None) -> str:
    """未完成轮在历史里的 assistant 侧标记。failed 轮带上落库的 ``error_kind``（`timeout` / `llm_failed` …，软件写的枚举，
    不是散文）——「再试一次」该怎么试取决于它。逐字节稳定：同一轮每次渲出来都一样。"""
    base = INCOMPLETE_TURN_MARKERS[status]
    kind = (error_kind or "").strip()
    if status == "failed" and kind:
        return f"{base[:-1]}: {kind}]"
    return base


def history_call_id(turn_seq: Any, call_id: str) -> str:
    """历史里 tool_call 的 id（r4 review 修）：按轮加前缀。

    当时的 `call_id` 不保证跨轮唯一——文本抢救路径统一发 `salvaged_0`、流里没 id 时发 `call_{idx}`，
    两个历史轮撞同一个 id 会让整个会话的每一次请求都带两个同 id 的 tool_use。前缀取轮 seq（稳定），每轮渲出来仍逐字节一样。
    """
    return f"h{turn_seq}_{call_id}"


def turn_sizes_from_messages(history: Sequence[Any]) -> list[int]:
    """从历史消息本身推每轮的消息数：每条 `role == "user"` 开一轮（历史里 user 只有用户原话，退回指令不进历史）。

    review 修：不再信一条并行传进来的 sizes 列表——对不上时按整对回退会把 [user, assistant(tool_calls)] 掉掉、留下孤儿 tool 消息 ⇒ 两条面都 400。
    """
    sizes: list[int] = []
    for m in history:
        if getattr(m, "role", None) == "user":
            sizes.append(1)
        elif sizes:
            sizes[-1] += 1
        else:
            sizes.append(1)  # Unexpected (history does not start with non-user); tolerate as one turn
    return sizes


def render_spawn_arguments(rec: Any, *, strip: Callable[[str], str] | None = None) -> dict[str, Any]:
    """历史里 `subagent_spawn` 调用的参数（r4）。**键序固定**（缓存前缀要逐字节稳定），只放事实：profile / rationale / task。

    review 修：task / rationale 也过 ``strip``——Lead 写的任务里可能带 `[ref:N]`，那是上一轮的编号。
    **r5（116 第二轮 dogfood A/H 对照）**：`canvas_mode` / `delivery` 是**分类标签**不是事实，渲进历史就被当模板抄——前面两轮 append，
    接着的裸口径 3/3 判 append；前一轮 revise 则 3/3 revise。档位判据只留尾块的续轮档位提示，历史不带。
    记录里仍保留这两个字段（落库事实），只是不渲。
    """
    st = strip or (lambda x: x)
    out: dict[str, Any] = {}
    out["profile"] = str(getattr(rec, "profile", "") or "")
    rationale = st(str(getattr(rec, "rationale", "") or "")).strip()
    if rationale:
        out["rationale"] = rationale
    out["task"] = st(str(getattr(rec, "task", "") or "")).strip()
    return out


def render_spawn_result(rec: Any, *, strip: Callable[[str], str] | None = None) -> str:
    """历史里 `subagent_spawn` 的结果摘要（r4）：状态 / 子工具数 / 画布写了哪些节 / peer 的一句结论。

    只放摘要不放原始数据（批 3「不把上一轮工具原文拖进下一轮」）；**确定性**：全部字段来自落库事件，
    不含相对索引、不含会重算的量（duration 不进）。review 修：结论 / 失败原因过 ``strip``——peer 的 exec_summary
    会被自动追加 `[cite:N]`，那是上一轮的编号，进了历史就会被本轮当成引用。
    """
    st = strip or (lambda x: x)
    status = str(getattr(rec, "status", "") or "unknown")
    parts = [status]
    n_tools = int(getattr(rec, "child_tool_count", 0) or 0)
    parts.append(f"子工具 {n_tools} 次")
    sections = list(getattr(rec, "canvas_sections", ()) or ())
    if getattr(rec, "synthesized", False):
        # r7: aborted peer with no receipt: sections written are unknown; **never claim 0** (116 C3: killed peer's section remained on canvas).
        parts.append("没有回执（被中止前是否已写入画布未知，以画布现状为准）")
    elif sections:
        items = "；".join(f"{kind}「{title}」" if title else str(kind) for kind, title in sections)
        parts.append(f"画布写入 {len(sections)} 节：{items}")
    else:
        parts.append("画布写入 0 节")
    reason = st(str(getattr(rec, "failure_reason", "") or "")).strip()
    if reason:
        parts.append(f"失败原因：{reason}")
    outcome = st(str(getattr(rec, "outcome", "") or "")).strip()
    if outcome:
        parts.append(f"结论：{outcome}")
    return "｜".join(parts)


def build_turn_history(
    turns: Sequence[Any],
    *,
    exclude_turn_id: str | None,
    budget_tokens: int,
    hysteresis: float = 0.75,
    estimate: Callable[[str], int] = estimate_tokens,
    make_message: Callable[[str, str], Any] | None = None,
    strip: Callable[[str], str] | None = None,
    spawns: Mapping[str, Sequence[Any]] | None = None,
    clarifications: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    make_tool_calls_message: Callable[[list[tuple[str, str, dict[str, Any]]]], Any] | None = None,
    make_tool_result_message: Callable[[str, str, str], Any] | None = None,
) -> TurnHistory:
    """Render prior visible Turns as native history within a whole-Turn budget.

    Completed Turns retain their answers; failed/cancelled/aborted Turns retain
    fixed markers and recorded spawn results. Clarification-pending Turns retain
    the original request and read-only card facts, never executable tool calls.
    The current Turn, running Turns and empty completed Turns are excluded.
    Citation markers are stripped from completed answers and spawn summaries.
    Oldest whole Turns are dropped to the hysteresis floor when over budget;
    a nonpositive budget is unlimited.
    """
    mk = make_message or (lambda role, content: {"role": role, "content": content})
    mk_calls = make_tool_calls_message or (
        lambda calls: {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": cid, "name": name, "arguments": args} for cid, name, args in calls],
        }
    )
    mk_result = make_tool_result_message or (
        lambda call_id, name, content: {"role": "tool", "content": content, "tool_call_id": call_id, "name": name}
    )
    st = strip or (lambda x: x)
    # (user_text, answer, spawn_records, turn_seq, tokens, is_incomplete)
    kept: list[tuple[str, str, list[Any], Any, int, bool]] = []
    incomplete = 0
    for t in turns:
        turn_id = str(getattr(t, "id", "") or "")
        if exclude_turn_id and turn_id == exclude_turn_id:
            continue
        status = getattr(t, "status", None)
        user_text = (getattr(t, "user_text", None) or "").strip()
        if not user_text:
            continue
        if status == "completed":
            answer = st(getattr(t, "final_answer", None) or "").strip()
            if not answer:
                continue
        elif status in INCOMPLETE_TURN_MARKERS:
            answer = incomplete_turn_marker(str(status), getattr(t, "error_kind", None))
            incomplete += 1
        elif status == "clarification_pending":
            cards = list((clarifications or {}).get(turn_id) or [])
            answer = (
                "[clarification_pending: this task is unfinished. The following is historical question/answer data. "
                "Reading it does not resume the old task, execute Tools, or grant permission. A resolved card records an answer, "
                "not task completion. Preserve known requirements and answers; ask only for missing information.]\n"
                + json.dumps({"cards": cards, "availability": "available" if cards else "unavailable"}, ensure_ascii=False, sort_keys=True)
            )
            incomplete += 1
        else:
            continue
        recs: list[Any] = [] if status == "clarification_pending" else [r for r in ((spawns or {}).get(turn_id) or ()) if getattr(r, "call_id", None)]
        turn_seq = getattr(t, "seq", None)
        rendered_calls = [(history_call_id(turn_seq, str(r.call_id)), SPAWN_TOOL_NAME, render_spawn_arguments(r, strip=st)) for r in recs]
        rendered_results = [render_spawn_result(r, strip=st) for r in recs]
        # Completed-turn text is immutable (final_answer changes only while running); memoize estimates per turn:
        # long sessions (IM sessions never end) pay tiktoken cost only for new turns (review fix).
        # Pending answers can change without changing their character count.
        memo_ok = estimate is estimate_tokens and bool(turn_id) and status != "clarification_pending"
        spawn_chars = sum(len(json.dumps(a, ensure_ascii=False)) for _, _, a in rendered_calls) + sum(len(x) for x in rendered_results)
        key = f"{turn_id}:{len(user_text)}:{len(answer)}:{len(recs)}:{spawn_chars}"
        n = _HISTORY_TOKENS_MEMO.get(key) if memo_ok else None
        if n is None:
            n = estimate(user_text) + estimate(answer)
            for _, _, args in rendered_calls:
                n += estimate(json.dumps(args, ensure_ascii=False))
            for text in rendered_results:
                n += estimate(text)
            if memo_ok:
                if len(_HISTORY_TOKENS_MEMO) >= _HISTORY_TOKENS_MEMO_CAP:
                    _HISTORY_TOKENS_MEMO.clear()
                _HISTORY_TOKENS_MEMO[key] = n
        kept.append((user_text, answer, recs, turn_seq, n, status != "completed"))

    total = sum(n for _, _, _, _, n, _ in kept)
    dropped = 0
    if budget_tokens > 0 and total > budget_tokens:
        floor = int(budget_tokens * hysteresis)
        while kept and total > floor:
            _, _, _, _, n, was_incomplete = kept.pop(0)
            if was_incomplete:
                incomplete -= 1
            total -= n
            dropped += 1

    messages: list[Any] = []
    sizes: list[int] = []
    for user_text, answer, recs, turn_seq, _, _ in kept:
        start = len(messages)
        messages.append(mk("user", user_text))
        if recs:
            messages.append(
                mk_calls([(history_call_id(turn_seq, str(r.call_id)), SPAWN_TOOL_NAME, render_spawn_arguments(r, strip=st)) for r in recs])
            )
            for r in recs:
                messages.append(mk_result(history_call_id(turn_seq, str(r.call_id)), SPAWN_TOOL_NAME, render_spawn_result(r, strip=st)))
        messages.append(mk("assistant", answer))
        sizes.append(len(messages) - start)
    return TurnHistory(messages=messages, turns_used=len(kept), turns_dropped=dropped, tokens=total, turn_sizes=sizes, incomplete_turns=incomplete)


__all__ = [
    "WINDOW_GUARD_HYSTERESIS",
    "WINDOW_GUARD_RATIO",
    "INCOMPLETE_TURN_MARKERS",
    "incomplete_turn_marker",
    "SPAWN_TOOL_NAME",
    "TurnHistory",
    "build_turn_history",
    "drop_history_turns",
    "history_call_id",
    "render_spawn_arguments",
    "render_spawn_result",
    "turn_sizes_from_messages",
    "SystemSplit",
    "cleared_marker",
    "drop_history_pairs",
    "estimate_messages_tokens",
    "guard_window",
    "is_cleared",
    "split_system_for_prefix",
    "tool_schemas_tokens",
]
