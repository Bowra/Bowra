"""子 agent 的「上次工作备忘」（2026-09-06；116 量出来的第二大浪费源）。

同一会话里同一 profile 再次被派活时（116 90 天：占 17% 的 spawn），它是全新的上下文——上一次查过什么、撞过什么一概不知：
22% 的 BI 查询只是换了个日期重查，出错的重复 spawn 里 48% 重演上一次同样的错（超时后已被告知「别重试」，
下一轮的新 peer 又去撞），耗时只比首次省 15%。

形态（与同轮内既有的 `_prior_peer_bi_queries` 同源，延到上一轮）：同会话、同 profile 的**最近一次** spawn，四样东西——
查过的查询与结果 artifact、跑过的代码与产出、撞过的错误与当时系统给的指令、返回被拒的原因。渲成一段有界文本，
拼进 peer 的 `extra_instructions`（peer 的尾块，system / tools 前缀不动）。

⚠ 与 R6 同源的风险：交给新 peer 的是**记录**，不是可照抄的答案——口径 / 时间范围变了必须重新取数，
artifact 只在口径未变时可复用。每条都带当时的口径与出处，开头明写这条规则。

只读 `turn_events`；flag `BOWHEAD_PEER_PRIOR_RUN_MEMO`（默认 1，置 0 关掉 = 逐字节回到今天）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_FLAG = "BOWHEAD_PEER_PRIOR_RUN_MEMO"
_MAX_CHARS = 6000  # ≈3k-token cap; drop oldest tool records if exceeded.
_QUERY_MAX = 12
_CODE_PREVIEW_CHARS = 400
_ERROR_PREVIEW_CHARS = 220
_REJECT_PREVIEW_CHARS = 200


def peer_prior_run_enabled() -> bool:
    return os.environ.get(_FLAG, "1") == "1"


@dataclass(frozen=True)
class PeerToolCall:
    tool: str
    call_id: str
    args_preview: str
    ok: bool | None
    error: str | None
    result_preview: str
    artifact_ids: tuple[str, ...] = ()
    rows: int | None = None
    queries: tuple[str, ...] = ()
    query_artifacts: tuple[tuple[str, str], ...] = ()  # (query, artifact_id)
    failed_queries: tuple[tuple[str, str], ...] = ()  # (query, error): partial failures in batch bi_query, even when overall ok=True.


@dataclass(frozen=True)
class PeerPriorRun:
    turn_id: str
    turn_seq: int
    user_text: str
    profile: str
    task: str
    status: str
    failure_reason: str | None
    tools: tuple[PeerToolCall, ...] = ()
    return_rejections: tuple[str, ...] = ()
    is_previous_turn: bool = False  # Whether the match is the immediately preceding turn or an earlier one (wording differs).


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}
_QUARTER_MONTHS = {1: "M1-3", 2: "M4-6", 3: "M7-9", 4: "M10-12"}
_RELATIVE_WORDS = ("今年以来", "去年同期", "本季度", "上季度", "本月", "上月", "本周", "上周", "今年", "去年", "本年")
_COMPARISON_WORDS = frozenset({"去年同期", "去年"})  # Only says "compared with what"; does not narrow this turn's scope.
_PERIOD_RE = re.compile(
    r"(?P<ym>\d{4})\s*[-/.年]\s*(?P<ymm>\d{1,2})\s*(?:月(?![-–—~至到])|(?![-–—~至到\d月]))"  # 2026-06 / 2026年6月 (no following range).
    r"|(?P<y>\d{4})\s*年"
    r"|(?P<half>上半年|下半年)"
    r"|全年"
    r"|(?:第\s*)?(?P<q1>[一二三四1-4])\s*季度"
    r"|(?<![A-Za-z0-9])[Qq]\s*(?P<q2>[1-4])(?![0-9A-Za-z=＝]|\s*(?:分位|四分位))"  # Q1=下四分位 / Q3 四分位 refers to quantiles, not quarters (116 turn 4).
    r"|近\s*(?P<rn>\d+)\s*个?\s*(?P<ru>[天日周月年])"
    r"|(?P<r1>\d{1,2})\s*月?\s*[-–—~至到]\s*(?P<r2>\d{1,2})\s*月"
    r"|(?P<m>\d{1,2})\s*月"
    r"|(?P<w>" + "|".join(_RELATIVE_WORDS) + ")"
)


def period_tokens(text: str) -> frozenset[str]:
    """口径（时间范围）记号，**归一化**后比较：判断上一次的查询结果能不能复用。

    review 第二轮：原来拿原始子串当记号，`2026年1-6月` 与 `2026年上半年`、`1-6月` 与 `1至6月`、`06月` 与 `6月`、
    `2026 年` 与 `2026年` 全判成不同口径，恰恰把「换了个日期重查」那群人挡在外面。
    归一：年 → `Y2026`；月 / 月范围 / 半年 / 季度 → `M1-6` 这类月份区间；`近N天` → `R30天`；本月 / 去年同期这类相对词原样。
    `全年` 不出记号（`2026年全年` 与 `2026年` 同口径）。
    """
    out: set[str] = set()
    for m in _PERIOD_RE.finditer(str(text or "")):
        g = m.groupdict()
        if g["ym"]:
            out.add(f"Y{int(g['ym'])}")
            out.add(f"M{int(g['ymm'])}")
        elif g["y"]:
            out.add(f"Y{int(g['y'])}")
        elif g["half"]:
            out.add("M1-6" if g["half"] == "上半年" else "M7-12")
        elif g["q1"] or g["q2"]:
            out.add(_QUARTER_MONTHS[_CN_NUM[g["q1"] or g["q2"]]])
        elif g["rn"]:
            unit = "天" if g["ru"] == "日" else g["ru"]
            out.add(f"R{int(g['rn'])}{unit}")
        elif g["r1"]:
            a, b = int(g["r1"]), int(g["r2"])
            out.add(f"M{a}-{b}" if a != b else f"M{a}")
        elif g["m"]:
            out.add(f"M{int(g['m'])}")
        elif g["w"]:
            out.add(g["w"])
    return frozenset(out)


def _squash(text: str) -> str:
    return "".join(str(text or "").split())


def prior_query_reusable(query: str, *, task_now: str, user_text_now: str) -> bool:
    """上一次这条查询的结果 artifact 本轮能不能复用（口径守卫，与 R6 同源）。

    review 第二轮（P1）：原「上次记号 ⊆ 本轮记号」会把 `2026年`（全年）判成可复用于 `2026年第二季度`——正是守卫要挡的那种。
    现在：上次记号非空、⊆ 本轮记号，且本轮**多出来的记号里没有任何年以下粒度**（月 / 季度 / 近 N 天 / 相对词）；
    或者上一次的查询文本原样出现在本轮任务里（Lead 把同一句查询又写了一遍）。
    """
    q_toks = period_tokens(query)
    scope_now = period_tokens(task_now) | period_tokens(user_text_now)
    # Among new tokens this turn, years and **comparison** words such as "去年同期 / 去年" do not narrow scope (116 turn 4 dogfood: Lead's task said
    # "2026年4-6月 … 以及 2025年4-6月（去年同期）"; `去年同期` blocked a same-scope 2026 Q2 query as "must requery").
    if q_toks and q_toks <= scope_now and all(t.startswith("Y") or t in _COMPARISON_WORDS for t in scope_now - q_toks):
        return True
    # Exact repetition: this turn cannot add any time token (previous scope unmarked + current scope marked ⇒ old artifact scope unknown; do not reuse).
    q = _squash(query)
    return bool(q) and len(q) >= 6 and not (scope_now - q_toks) and q in _squash(task_now)


def render_prior_run_memo(
    run: PeerPriorRun,
    *,
    user_text_now: str = "",
    task_now: str = "",
    max_chars: int = _MAX_CHARS,
) -> str:
    """渲成一段确定性的备忘（同一 run 渲两次逐字节相同；不含相对轮号、不含耗时）。

    口径守卫见 `prior_query_reusable`；超长时先丢查询 / 代码条目，**撞过的错与被拒原因最后才丢**（它们是 48% 错误重演的解药）。
    """
    when = "上一轮" if run.is_previous_turn else "本会话早些时候"
    head = [
        f"## 你上一次在本会话里的记录（{when}用户说「{_clip(run.user_text, 40)}」）",
        "这是**记录**，不是本轮任务。口径 / 时间范围 / 对象与本轮不同时**必须重新取数**；只有标了「可复用」的 artifact 才能用 python_sandbox 的 inputs 复用。",
        f"- 上一次的任务口径：{_clip(run.task, 160)}",
        f"- 上一次结果：{run.status}" + (f"（{_clip(run.failure_reason, 80)}）" if run.failure_reason else ""),
    ]
    queries: list[str] = []
    for t in run.tools:
        if t.tool != "bi_query":
            continue
        if t.query_artifacts:
            for q, aid in t.query_artifacts[:_QUERY_MAX]:
                reusable = prior_query_reusable(q, task_now=task_now, user_text_now=user_text_now)
                tag = f" → artifact_id={aid}（口径相同，可复用）" if reusable else "（口径不同或未标明，须重查）"
                queries.append(f"  - {_clip(q, 80)}{tag}")
        else:
            for q in t.queries[:_QUERY_MAX]:
                queries.append(f"  - {_clip(q, 80)}（须重查）")
    queries = queries[:_QUERY_MAX]
    code: list[str] = []
    for t in run.tools:
        if t.tool in ("python_sandbox", "python_exec") and (t.ok is not False):
            arts = ", ".join(t.artifact_ids[:4]) if t.artifact_ids else "无"
            code.append(
                f"- 跑过代码（{t.tool}）→ 产出 artifact：{arts}（数据口径变了要重跑）；代码开头：{_clip(t.args_preview, _CODE_PREVIEW_CHARS)}"
            )
    errs: list[str] = []
    for t in run.tools:
        if t.ok is False or t.error:
            errs.append(f"  - {t.tool}：{_clip(t.error or t.result_preview, _ERROR_PREVIEW_CHARS)}")
        for q, e in t.failed_queries[:4]:
            errs.append(f"  - {t.tool}「{_clip(q, 60)}」：{_clip(e, _ERROR_PREVIEW_CHARS)}")
    errs = errs[:6]
    rejects = [f"  - {_clip(r, _REJECT_PREVIEW_CHARS)}" for r in run.return_rejections[:3]]
    if not (queries or code or errs or rejects):
        return ""

    def _assemble() -> str:
        lines: list[str] = []
        if queries:
            lines.append("- 查过（bi_query）：")
            lines.extend(queries)
        lines.extend(code)
        if errs:
            lines.append("- 撞过（别再犯同样的错；错误里的指令仍然有效）：")
            lines.extend(errs)
        if rejects:
            lines.append("- 上次 subagent_return 被拒的原因（这次一开始就避开）：")
            lines.extend(rejects)
        return "\n".join(head + lines)

    text = _assemble()
    # Too long: drop queries, then code, then errors, then rejection reasons; drop each category from the end; always retain the first four lines.
    for bucket in (queries, code, errs, rejects):
        while len(text) > max_chars and bucket:
            bucket.pop()
            text = _assemble()
    return text


__all__ = ["PeerPriorRun", "PeerToolCall", "peer_prior_run_enabled", "period_tokens", "prior_query_reusable", "render_prior_run_memo"]
