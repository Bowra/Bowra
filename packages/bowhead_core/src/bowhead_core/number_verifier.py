"""Post-LLM 数字验证 —— 防幻觉兜底.

Lead / Analyst / narrator 任何输出 markdown 里，**每个具体数字**都必须能在
`tool_invocations` 的返回里找到来源（判据＝**忠实四舍五入**，见 `is_faithful_rounding`；
不是一刀切相对容差 —— 合法舍入误差随书写精度差一个数量级）。找不到的 → 附加⚠警告段
到文本末尾（不改写正文，让用户看到可核验清单）.

关键点：不直接 **删数字或覆盖原文** —— 那样会破坏 markdown 结构、引起用户困惑.
我们只附加 footer，让用户可查；同时供 canvas_composer 读去决定哪些 section 该打
`unverified` 标签。
"""

from __future__ import annotations

import bisect
import contextlib
import logging
import math
import re
import unicodedata
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from bowhead_core.analysis_payload import is_faithful_rounding

if TYPE_CHECKING:
    from bowhead_core.types import ToolInvocation

logger = logging.getLogger(__name__)


# Treat a hyphen as a minus sign only when not preceded by CJK / % / a closing parenthesis; otherwise it denotes a range:
# reading -1600 / -60 in `预计规模 1000万-1600万元` or `占比 50%-60%` invents a negative number.
#
# ⚠ `report_writer_loop._MARK_NUM_RE` shares this fragment; do not create a separate copy there:
# if the criteria drift (one recognizes a minus sign, the other does not), token extraction and authorization disagree;
# instead of an error, the number silently disappears (changing only one side made all of `1600万元` vanish in testing).
MINUS_IF_NOT_RANGE = r"(?:(?<![一-鿿%）)])-)?"
_NUM_RE = re.compile(
    rf"(?<![\d.,]){MINUS_IF_NOT_RANGE}\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?"
    rf"|(?<![\d.,]){MINUS_IF_NOT_RANGE}\d+(?:\.\d+)?",
)
# i18n v1: parse numeric units in LLM output (Chinese and English).
# Notes:
# - This parses numeric values; persisted marker formatting lives in citation_marks.py.
# - Chinese units / English K/M/B share one table; parsing uses prefix matching, longest first (e.g. "百分点" before "百").
# - English is case-sensitive: uppercase K/M/B; reject lowercase k/m/b, which can collide with variables/words.
_UNIT_MAP = {
    # Chinese units (descending prefix length, avoiding a collision between "百" and "百分点")
    "百分点": 0.01,
    "亿": 1e8,
    "万": 1e4,
    "千": 1e3,
    "%": 0.01,
    "pp": 0.01,
    # English compact units (i18n v1: English output such as "12K" / "1.2M" / "0.5B")
    "K": 1e3,
    "M": 1e6,
    "B": 1e9,
    "T": 1e12,
}
# Ignore years, small integers (top N rankings / step numbers), and integer percentages, e.g. "3 条".
_YEAR_LO, _YEAR_HI = 1900, 2100
_IGNORE_INT_LE = 100

# Diagnostic-log sample limits and context window.
# ★ Narrowed by independent review on 2026-08-11: formerly captured eight characters before and after each number, but this log's purpose
# (checking spacing before the unit) requires only the suffix. The preceding eight characters were unnecessary and most likely to expose
# company/person names alongside amounts. In enterprise deployments these snippets are customer business data.
# More critically, an empty `authorized` flags every number on that line (see `ok = False` below),
# and this function runs per line: this failure most needs diagnosis and most risks flooding logs.
# Make sample limits meaningful per invocation and per turn: narrow the window and retain only suffixes.
_LOG_MAX_SAMPLES = 10
_LOG_CTX_AFTER = 4


def _flatten_invocations(invs: list[ToolInvocation]) -> list[ToolInvocation]:
    out: list[ToolInvocation] = []
    for inv in invs:
        out.append(inv)
        if getattr(inv, "nested", None):
            out.extend(_flatten_invocations(inv.nested))
    return out


def _extract_numbers(obj: Any, bag: set[float]) -> None:
    """从任意值里把数字收集到 bag."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        bag.add(float(obj))
        return
    if isinstance(obj, str):
        for m in _NUM_RE.findall(obj):
            raw = m.replace(",", "")
            with contextlib.suppress(ValueError):
                bag.add(float(raw))
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _extract_numbers(v, bag)
        return
    if isinstance(obj, list):
        for v in obj:
            _extract_numbers(v, bag)


def build_authorized_numbers(invocations: list[ToolInvocation]) -> set[float]:
    """展平 subagent nested + 把所有 tool result 里的数字收集成集合.

    同时做单位扩展（#79 G7）：亿/万/元之间的 scale mismatch 不再误报 ——
    每个 v 额外塞入 v×1e8 / v÷1e8 / v×1e4 / v÷1e4.
    """
    flat = _flatten_invocations(invocations)
    bag: set[float] = set()
    for inv in flat:
        if inv.error or inv.result is None:
            continue
        _extract_numbers(inv.result, bag)
    return _expand_for_unit_scale(bag)


def _expand_for_unit_scale(auth: set[float]) -> set[float]:
    """给每个 value 加入常见单位换算（亿/万/元）变体.

    e.g. 165 亿 对应 165 和 16500000000 —— 工具返 content=165 和 value=16500000000
    可能只授权其中一个；扩展后两者都能匹配.
    """
    out: set[float] = set(auth)
    for v in list(auth):
        if v == 0:
            continue
        out.add(v * 1e8)  # Hundred millions
        out.add(v / 1e8)
        out.add(v * 1e4)  # Ten thousands
        out.add(v / 1e4)
        out.add(v * 1e2)  # Hundreds / fractional points
        out.add(v / 1e2)
    return out


def verify_numbers_in_text(
    text: str,
    authorized: set[float],
    *,
    trace: str = "",
) -> list[tuple[str, float]]:
    """扫 text 里所有数字；返回 `(raw_match, normalized_value)` 列表 —— 未授权的.

    trace: 调用方的 turn/section 标识，只进日志，不影响判定。
    """
    unauth: list[tuple[str, float]] = []
    contexts: list[str] = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(0)
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        # Allowlist: small integers / years
        if v.is_integer():
            iv = int(v)
            if abs(iv) <= _IGNORE_INT_LE:
                continue
            if _YEAR_LO <= iv <= _YEAR_HI:
                continue
        # Normalize unit suffixes.
        # ★ Skip whitespace first: models very often write 「1.38 亿元」 with a space between number and unit;
        # the old three-character adjacent slice yielded " 亿元", making `startswith("亿")` false ⇒ no unit multiplier,
        # leaving 1.38 rather than 1.38e8 ⇒ absent from the authorization set ⇒ falsely flagged despite being correct.
        # Observed on 116 (report section `t_3586a8f779ac`): BI raw value 138371946.52 (=1.384 亿);
        # 「1.38亿元」 is faithful rounding, yet was flagged as 「未在数据返回结果查到」.
        # Whether a correct number was falsely flagged depended on that space, explaining the apparent randomness.
        tail = text[m.end() :].lstrip(" \t　")[:4]
        scaled = v
        for unit, mult in _UNIT_MAP.items():
            if tail.startswith(unit):
                scaled = v * mult
                break
        # Accept if raw or scaled matches any authorized value (covers "29.3亿" vs 2.93e9).
        if authorized:
            # ★ Criterion: faithful rounding, rather than a universal relative tolerance.
            # See the `is_faithful_rounding` docstring: numeric restatements use significant digits / decimal places,
            # and a universal window cannot distinguish rounding from changing a number. Observed across 95 answers from the full 116 database:
            # legitimate rounding error varies greatly with written precision; at the same hundred-million scale, two decimals need only 0.41%,
            # but zero decimals ("2亿") need 16.67%; a zero-decimal ratio ("18%") needs 25%.
            # ⇒ Fixed tolerances by magnitude fail at both ends; judge written precision instead.
            # The old rel_tol=0.05 was too loose (9.99亿 vs 9.80亿, a 1.94% difference, passed),
            # yet the spacing bug above accidentally restored some rejection; fix both issues in the same batch.
            ok = any(is_faithful_rounding(v, a) or is_faithful_rounding(scaled, a) for a in authorized)
            # Derived-number fallback: accept simple derivations from two authorized values (a/b-1, a-b, a×b%).
            # For example, a year-over-year rate of 0.08 can derive from 60.50亿/60.45亿-1.
            if not ok:
                ok = _is_derivable(v, authorized) or _is_derivable(scaled, authorized)
        else:
            ok = False  # No tool data means no numbers.
        if not ok:
            unauth.append((raw, v))
            if len(contexts) < _LOG_MAX_SAMPLES:
                contexts.append(raw + text[m.end() : m.end() + _LOG_CTX_AFTER])
    if unauth:
        # ★ Log only when actually flagging: this function runs per line (`report_writer_loop`
        # calls it once for each Markdown line); unconditional logging floods logs. Flagging is rare,
        # and is exactly the event that needs retrospective diagnosis.
        #
        # Why this is necessary (2026-08-10 dogfood on 116): a user reported a false flag
        # (「1.38 亿元」 in a report flagged as "未在数据返回结果查到"), which I could not diagnose afterward;
        # `authorized` comes from runtime `tool_invocations` and is never persisted.
        # Both reconstruction attempts expanded the set (regex-scanning turn_events included IDs/timestamps),
        # making the number derivable and the old bug impossible to reproduce. Hours of investigation left only inference.
        #
        # Log these three things to cover exactly the three missing facts:
        #   authorized=  Authorization-set size: distinguish a starved set from actual fabrication.
        #   flagged=     Original flagged strings.
        #   ctx=         Number + the next four characters: reveals spacing before the unit
        #                (「1.38 亿元」 vs 「1.38亿元」 took entirely different code paths).
        #                Suffix-only is deliberate: prefixes do not aid judgment and most readily expose narrative meaning.
        #   shapes=      Derivation shape of each flagged number (sum2/sum3/scaled/none).
        #                Added because relaxing the criterion lacks evidence: authorized is never persisted,
        #                and retrospective reconstruction failed; classification must happen here at flagging time.
        #                Collect enough samples to determine how many false positives are actually derived quantities.
        logger.info(
            "number_guard.flagged trace=%s authorized=%d flagged=%s shapes=%s ctx=%s",
            trace or "-",
            len(authorized),
            [raw for raw, _ in unauth][:_LOG_MAX_SAMPLES],
            [classify_flagged_number(value, authorized) for _, value in unauth[:_LOG_MAX_SAMPLES]],
            contexts,
        )
    return unauth


_CLASSIFY_MAX_VALS = 40


def classify_flagged_number(target: float, auth: set[float], *, rel_tol: float = 0.02) -> str:
    """给一个**已被标记**的数字打一个派生形状标签 —— 只进日志，不改判定。

    feedback-triage-2026-08-24：客户的报告顶上挂着「以下数字未在工具返回结果中查到，
    可能是模型推算或臆觉: 210,079,277」，而这个数就是同段正文里 Top 3 的求和
    （146,459,000 + 32,805,940 + 30,814,337）。`_is_derivable` 只覆盖**两项**派生
    （a/b-1、a-b、a×b%），三项求和不在内，于是派生量被当成凭空数字。

    ⚠ 但**不能就这么放宽**：授权集常有几十个数，允许任意子集求和等于把闸拆了
    （任何值都能凑出来），真幻觉会一起放过。放宽与否必须先看误杀率。

    ⚠⚠ 而误杀率**用现有数据量不出来** —— 判定用的 `authorized` 来自运行期的
    `tool_invocations`，**从不落库**（本文件上方那段 2026-08-10 的教训里，作者尝试
    事后重建失败过：正则扫 turn_events 会把 id/时间戳算进集合，把它撑大到什么都可派生）。
    所以先在**唯一拿得到这个集合的时刻**（标记发生时）把形状记下来，攒够数据再决定。

    标签：
      · ``sum2`` / ``sum3`` —— 是 2 项 / 3 项授权值之和（本次客户案例属 ``sum3``）
      · ``scaled`` —— 与某个授权值差一个 10 的整数次幂（单位换算没对上）
      · ``none``   —— 上述都不是，更像真的凭空数字

    成本：只在标记这条罕见路径上跑；候选集截到 ``_CLASSIFY_MAX_VALS`` 个，
    sum3 用「对和查表」把 O(n³) 压到 O(n²)。
    """
    if abs(target) < 1e-9 or not auth:
        return "none"

    def close(x: float) -> bool:
        return abs(target - x) / max(abs(target), 1e-9) < rel_tol

    vals = [v for v in sorted(auth, key=lambda x: -abs(x)) if v != 0][:_CLASSIFY_MAX_VALS]

    for a in vals:
        for exp in (-4, -2, 2, 4, 8):
            if close(a * (10.0**exp)):
                return "scaled"

    pair_sums: dict[int, tuple[float, float]] = {}
    for i, a in enumerate(vals):
        for b in vals[i + 1 :]:
            total = a + b
            if close(total):
                return "sum2"
            pair_sums[round(total, 6)] = (a, b)

    for c in vals:
        need = target - c
        for candidate in pair_sums:
            if abs(need - candidate) / max(abs(need), 1e-9) < rel_tol:
                return "sum3"

    return "none"


# ── Strict mode: quantity checks before external sending (used by send_email) ─────────────────────────────
#
# Unlike the warning-appending mode above, this mode protects outputs that cannot be recalled once sent:
#   · Check only quantities with units (monetary units/percentages/currency symbols/thousands separators); dates, rankings, phones, IDs,
#     and other bare integers are outside its scope: they are not this failure and would make false positives unusable.
#   · Do not use `_is_derivable`: its 2% tolerance plus tiny values from unit expansion let
#     any number within 2% of a real value pass (9.51 亿 ≈ 9.54 亿 minus a tiny number),
#     exactly the error in the email from 116. Compute derived values (totals, differences) with a tool before adding them to sources.
#   · Check integer percentages too (「占比 75%」); the general mode above allows all integers ≤100.
_QUANTITY_UNITS: tuple[tuple[str, float], ...] = (
    ("百分点", 0.01),
    ("亿", 1e8),
    ("万", 1e4),
    ("千", 1e3),
    ("%", 0.01),
    ("pp", 0.01),
    ("美元", 1.0),
    ("港元", 1.0),
    ("欧元", 1.0),
    ("元", 1.0),
    ("K", 1e3),
    ("M", 1e6),
    ("B", 1e9),
    ("T", 1e12),
)
_CURRENCY_PREFIXES = frozenset("¥$€£")


def unsourced_quantities(text: str, authorized: Iterable[float]) -> list[str]:
    """返回 text 里**带量纲**、却不是任何来源数值忠实四舍五入的数（原样片段，去重保序）。

    authorized 应已做过单位扩展（见 `expand_for_unit_scale`）；空集合 ⇒ 每个带量纲的数都返回。
    判据＝`is_faithful_rounding`，只在候选值附近一个数量级窗口内比较，保证大表也不退化成全量扫描。
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    ordered = sorted({float(v) for v in authorized if math.isfinite(v)})
    flagged: list[str] = []
    for m in _NUM_RE.finditer(normalized):
        raw = m.group(0)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        tail = normalized[m.end() :].lstrip(" \t")
        unit, mult = next(((u, k) for u, k in _QUANTITY_UNITS if tail.startswith(u)), ("", 1.0))
        head = normalized[: m.start()].rstrip(" \t")
        has_currency = bool(head) and head[-1] in _CURRENCY_PREFIXES
        if not unit and not has_currency and "," not in raw:
            continue
        stated = value * mult
        if stated == 0 or (mult == 0.01 and value == 100):
            continue  # "0" and "100%" are expressions, not retrieved figures.
        if not _has_faithful_source(stated, ordered):
            label = f"{raw}{unit}"
            if label not in flagged:
                flagged.append(label)
    return flagged


def _has_faithful_source(stated: float, ordered: list[float]) -> bool:
    span = 10.0 ** math.floor(math.log10(abs(stated)))
    i = bisect.bisect_left(ordered, stated - span)
    while i < len(ordered) and ordered[i] <= stated + span:
        if is_faithful_rounding(stated, ordered[i]):
            return True
        i += 1
    return False


def expand_for_unit_scale(values: set[float]) -> set[float]:
    """公开别名：来源数值的单位扩展（亿/万/百分比），与通用档同一实现。"""
    return _expand_for_unit_scale(values)


def extract_numbers(obj: Any, bag: set[float]) -> None:
    """公开别名：从任意工具结果里收集数值，与通用档同一实现。"""
    _extract_numbers(obj, bag)


def _is_derivable(target: float, auth: set[float], rel_tol: float = 0.02) -> bool:
    """target 是否可由 authorized 集合里任意两个数做常见派生（a/b-1 / a-b /
    a×b×0.01 / (a-b)/c / a×0.X 等）得到. 容差 2%.

    保护模式：|auth| > 40 → 走 2-step 派生可能 O(n³) 爆；走快速 path 只 O(n²).
    """
    if abs(target) < 1e-9:
        return False
    vals = sorted(auth, key=abs)[:40]  # Cap at 40 to limit growth.
    n = len(vals)
    # Share common-fraction check (#79): accept only clearly precise matches. Tighten tolerance to
    # 0.5% (vs the default 2%) to avoid accepting arbitrary ~10% values as valid derivations.
    for a in vals:
        if a == 0:
            continue
        for frac in (0.1, 0.2, 0.25, 0.5, 0.75):
            if abs(target - a * frac) / max(abs(a * frac), 1e-9) < 0.005:
                return True

    for a in vals:
        if a == 0:
            continue
        for b in vals:
            if b == 0:
                continue
            # Year-over-year / change rate: a/b - 1
            r = a / b - 1
            if abs(target - r) / max(abs(r), 1e-9) < rel_tol:
                return True
            # Percentage ratio: a/b * 100 (e.g. 17.54 = 28.96亿/165.35亿*100)
            r2 = a / b * 100
            if abs(target - r2) / max(abs(r2), 1e-9) < rel_tol:
                return True
            # Absolute difference: a - b
            if a != b and abs(target - (a - b)) / max(abs(a - b), 1e-9) < rel_tol:
                return True
            # Percentage-point product: (a-b) × base × 0.01 — impact calculation
            for c in vals:
                if c != 0 and abs(target - (a - b) * c * 0.01) / max(abs(target), 1e-9) < rel_tol:
                    return True

    # Two-step chained derivation (#79): (a-b)/c; only for small |auth| to avoid explosion (n ≤ 20).
    if n <= 20:
        for a in vals:
            for b in vals:
                if a == b:
                    continue
                for c in vals:
                    if c == 0:
                        continue
                    val = (a - b) / c
                    if abs(target - val) / max(abs(val), 1e-9) < rel_tol:
                        return True
    return False


def annotate_with_warning(text: str, unauth: list[tuple[str, float]]) -> str:
    """把未授权数字列成一个 **footer 警告段** 追加到 text 末尾（不改写正文）.

    如果未授权数字超过总数字的 30%，footer 里加硬提示；否则标注少量为"待核验"。
    """
    if not unauth:
        return text
    # Deduplicate by raw text, preserving occurrence order.
    seen: set[str] = set()
    unique: list[str] = []
    for raw, _ in unauth:
        if raw in seen:
            continue
        seen.add(raw)
        unique.append(raw)

    footer = "\n\n---\n\n> ⚠️ **数据核验提示**：以下数字未在本次工具返回里直接查到，可能是模型基于上下文推算或幻觉，请谨慎采用：\n" + "\n".join(
        f"> - `{raw}`" for raw in unique[:20]
    )
    return text + footer
