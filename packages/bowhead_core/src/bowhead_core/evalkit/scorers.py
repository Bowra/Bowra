"""Scorers — 对 (case, output) 打分.

每个 scorer 负责一个维度；报告按维度 × case 矩阵汇总.

内建 7 种：
    - ExactMatchScorer   — 精确匹配（字符串规范化后）
    - ContainsScorer     — 期望片段全部包含（list）
    - JSONFieldScorer    — dict path 取值后比对
    - NumberToleranceScorer — 数值容差（相对/绝对）
    - NoFabricationScorer — 从 agent trace 抽数字 → 与 "允许数字白名单" 对比
    - RecallAtKScorer    — 检索排序:目标进 top-k 的召回比例
    - MRRScorer          — 检索排序:首个命中的 reciprocal rank

NoFabrication 是 Bowhead 特化的：agent 运行时 trace 提供"授权数字"集合
(bi.* 工具返回的数字)，output 里如果出现**其他**数字 → 视为编造，扣分.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from bowhead_core.evalkit.cases import EvalCase


@dataclass
class ScoreResult:
    name: str  # Scorer name
    passed: bool
    score: float  # 0..1; a passed result may be < 1 (partial pass).
    detail: str = ""  # Human-readable explanation (failure reason / match details).
    data: dict[str, Any] = field(default_factory=dict)


class Scorer(Protocol):
    """所有 scorer 实现这个接口.

    output 是 agent 返回的主输出；trace 是可选的 run 时副产物（见 runner）.
    """

    name: str

    def score(
        self,
        case: EvalCase,
        output: Any,
        trace: dict[str, Any] | None = None,
    ) -> ScoreResult: ...


# ─────────────────────────────────────────────────────────────
# Basic scorers
# ─────────────────────────────────────────────────────────────
@dataclass
class ExactMatchScorer:
    name: str = "exact_match"
    case_insensitive: bool = False
    strip: bool = True

    def score(self, case: EvalCase, output: Any, trace=None) -> ScoreResult:  # type: ignore[no-untyped-def]
        exp = case.expected
        if exp is None:
            return ScoreResult(self.name, False, 0.0, "no expected")
        a = str(output)
        b = str(exp)
        if self.strip:
            a = a.strip()
            b = b.strip()
        if self.case_insensitive:
            a = a.lower()
            b = b.lower()
        ok = a == b
        return ScoreResult(
            self.name,
            ok,
            1.0 if ok else 0.0,
            "" if ok else f"expected {b!r} but got {a!r}",
        )


@dataclass
class ContainsScorer:
    """期望列出的每个片段都必须包含；部分命中按命中比例计分."""

    name: str = "contains"
    case_insensitive: bool = False

    def score(self, case: EvalCase, output: Any, trace=None) -> ScoreResult:  # type: ignore[no-untyped-def]
        needles = case.expected
        if isinstance(needles, str):
            needles = [needles]
        if not needles:
            return ScoreResult(self.name, False, 0.0, "no needles")
        hay = str(output)
        if self.case_insensitive:
            hay = hay.lower()
        hits = 0
        missing: list[str] = []
        for n in needles:
            needle = str(n)
            if self.case_insensitive:
                needle = needle.lower()
            if needle in hay:
                hits += 1
            else:
                missing.append(str(n))
        ratio = hits / len(needles)
        return ScoreResult(
            self.name,
            hits == len(needles),
            ratio,
            "" if hits == len(needles) else f"missing: {missing}",
            {"hits": hits, "total": len(needles)},
        )


@dataclass
class JSONFieldScorer:
    """按 dot-path 从 output（期望是 dict）取值，与 case.expected[path] 相等判定.

    用于结构化输出（比如 JSON response、canvas section dict）.
    """

    path: str
    name: str | None = None

    def __post_init__(self) -> None:
        if self.name is None:
            self.name = f"json_field:{self.path}"

    def score(self, case: EvalCase, output: Any, trace=None) -> ScoreResult:  # type: ignore[no-untyped-def]
        expected_root = case.expected or {}
        want = _getpath(expected_root, self.path) if isinstance(expected_root, dict) else None
        got = _getpath(output, self.path) if isinstance(output, dict) else None
        ok = want == got and want is not None
        return ScoreResult(
            self.name or "json_field",
            ok,
            1.0 if ok else 0.0,
            "" if ok else f"path={self.path} expected={want!r} got={got!r}",
        )


def _getpath(d: Any, path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


@dataclass
class NumberToleranceScorer:
    """expected 和 output 都提取首个数字比对；相对误差 ≤ rel 或 绝对误差 ≤ abs 算 pass."""

    name: str = "number_tolerance"
    rel_tolerance: float = 0.01
    abs_tolerance: float = 0.0

    _NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

    def score(self, case: EvalCase, output: Any, trace=None) -> ScoreResult:  # type: ignore[no-untyped-def]
        exp = case.expected
        if exp is None:
            return ScoreResult(self.name, False, 0.0, "no expected")
        want = self._first(str(exp))
        got = self._first(str(output))
        if want is None:
            return ScoreResult(self.name, False, 0.0, "no number in expected")
        if got is None:
            return ScoreResult(self.name, False, 0.0, "no number in output")
        diff = abs(got - want)
        rel = diff / abs(want) if want != 0 else diff
        ok = diff <= self.abs_tolerance or rel <= self.rel_tolerance
        return ScoreResult(
            self.name,
            ok,
            1.0 if ok else max(0.0, 1.0 - rel),
            "" if ok else f"want={want} got={got} rel={rel:.3f}",
            {"want": want, "got": got, "rel_err": rel},
        )

    def _first(self, s: str) -> float | None:
        m = self._NUM_RE.search(s)
        if not m:
            return None
        try:
            return float(m.group(0))
        except ValueError:
            return None


@dataclass
class NoFabricationScorer:
    """检测 agent 输出里是否出现 **未授权** 的数字.

    trace 里放 `authorized_numbers`（通过 bi.* 工具拿到的数字集合）；output 里出现的
    任何数字都必须在授权白名单里（或是纯 ordinal/年份，可选忽略）.

    对 "agent 幻觉数字" 类 bug 是基本兜底（Bowhead 的生命线）.
    """

    name: str = "no_fabrication"
    ignore_integers_le: int = 100  # Ignore small integers (ordinals/integer percentages, e.g. "3 步").
    ignore_years: bool = True

    # Capture: optional sign + integer part allowing thousands separators + decimal part + optional unit (%/亿/万/千).
    # Match numbers with units first, then plain numbers (integer part allows `1,234,567`).
    _NUM_RE = re.compile(
        r"(?<![\d.,])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"  # With thousands separators
        r"|(?<![\d.,])-?\d+(?:\.\d+)?",  # Plain numbers
    )
    # Unit suffix: scale "29.3亿" to 2.93e9 using 亿 = 1e8, matching the raw value returned by the tool.
    _UNIT_MAP = {"%": 0.01, "亿": 1e8, "万": 1e4, "千": 1e3, "pp": 0.01, "百分点": 0.01}

    def _normalize(self, text: str) -> list[tuple[float, float]]:
        """从输出里抽 (raw, scaled) 对 —— raw 保留原数字（"29.3亿" → 29.3），
        scaled 乘单位放大（"29.3亿" → 2.93e9）. 匹配 trace 时两者任一满足即算已授权：
        trace 既可能是 `29.3`（"亿" 是单位信息）也可能是 `2930123456.7`（raw value）."""
        out: list[tuple[float, float]] = []
        for m in self._NUM_RE.finditer(text):
            raw = m.group(0).replace(",", "")
            try:
                v = float(raw)
            except ValueError:
                continue
            tail = text[m.end() : m.end() + 3]
            mult = 1.0
            for unit, u in self._UNIT_MAP.items():
                if tail.startswith(unit):
                    mult = u
                    break
            out.append((v, v * mult))
        return out

    def score(self, case: EvalCase, output: Any, trace=None) -> ScoreResult:  # type: ignore[no-untyped-def]
        auth: set[float] = set()
        if trace and isinstance(trace.get("authorized_numbers"), (list, set, tuple)):
            for v in trace["authorized_numbers"]:
                try:
                    auth.add(float(v))
                except (TypeError, ValueError):
                    continue
        nums = self._normalize(str(output))
        violations: list[float] = []
        checked: list[float] = []

        for raw, scaled in nums:
            if self._whitelisted(raw):
                continue
            checked.append(raw)
            # If raw or scaled matches any number in auth → allow it.
            if not (_close_to_any(raw, auth) or _close_to_any(scaled, auth)):
                violations.append(raw)
        if not checked:
            # No "significant" numbers in output → nothing to fabricate, so pass.
            return ScoreResult(self.name, True, 1.0, "no significant numbers")
        ratio = 1.0 - (len(violations) / len(checked))
        return ScoreResult(
            self.name,
            not violations,
            ratio,
            "" if not violations else f"unauthorized numbers: {violations}",
            {"checked": len(checked), "violations": violations},
        )

    def stated_value_matches(self, value: str, authorized: Iterable[float]) -> bool | None:
        """Whether every figure in one stated value has a source, with the same units and tolerance.

        Returns None when the value contains no figure to check.
        """
        auth = {float(item) for item in authorized}
        # Ordinals and years are skipped exactly as `score()` skips them.
        nums = [(raw, scaled) for raw, scaled in self._normalize(value) if not self._whitelisted(raw)]
        if not nums:
            return None
        return all(_close_to_any(raw, auth) or _close_to_any(scaled, auth) for raw, scaled in nums)

    def _whitelisted(self, v: float) -> bool:
        if v.is_integer():
            iv = int(v)
            if abs(iv) <= self.ignore_integers_le:
                return True
            if self.ignore_years and 1900 <= iv <= 2100:
                return True
        return False


def _close_to_any(value: float, authorized: set[float]) -> bool:
    return any(abs(value - item) / max(abs(item), 1e-9) < 0.05 for item in authorized)


# ─────────────────────────────────────────────────────────────
# Retrieval ranking scorers (kb-optimization evaluation gate MVP-2)
# output = an **ordered** list of retrieval hit keys (e.g. file_name deduplicated by rank);
# case.expected = target key list. The caller defines key semantics (file_name or chunk_id).
# ─────────────────────────────────────────────────────────────
@dataclass
class RecallAtKScorer:
    """recall@k：|expected ∩ dedup(output)[:k]| / |expected|。

    passed = 全部目标进 top-k；部分命中按比例计分（同 ContainsScorer 哲学）。
    output 自动去重保序——同一文件多 chunk 只算第一次出现的位次。
    """

    name: str = "recall_at_k"
    k: int = 5

    def score(self, case: EvalCase, output: Any, trace=None) -> ScoreResult:  # type: ignore[no-untyped-def]
        exp_raw = case.expected if isinstance(case.expected, (list, tuple, set)) else [case.expected]
        exp = {str(e) for e in exp_raw if e}
        if not exp:
            return ScoreResult(self.name, False, 0.0, "no expected")
        ranked = _dedup_ranked(output)
        top = ranked[: self.k]
        got = sum(1 for key in top if key in exp)
        ratio = got / len(exp)
        return ScoreResult(
            self.name,
            got == len(exp),
            ratio,
            "" if got == len(exp) else f"missing in top-{self.k}: {sorted(exp - set(top))}",
            {"k": self.k, "got": got, "total": len(exp), "top": top},
        )


@dataclass
class MRRScorer:
    """reciprocal rank：第一个命中目标的 1/rank；无命中 = 0。

    k=None 全列表找；k 给定时 rank > k 记 0（与 hit@k 口径一致）。
    跨 case 的 MRR = 报告层对本 scorer score 求均值（EvalReport 均分即是）。
    """

    name: str = "mrr"
    k: int | None = None

    def score(self, case: EvalCase, output: Any, trace=None) -> ScoreResult:  # type: ignore[no-untyped-def]
        exp_raw = case.expected if isinstance(case.expected, (list, tuple, set)) else [case.expected]
        exp = {str(e) for e in exp_raw if e}
        if not exp:
            return ScoreResult(self.name, False, 0.0, "no expected")
        ranked = _dedup_ranked(output)
        rank = next((i + 1 for i, key in enumerate(ranked) if key in exp), None)
        if rank is None or (self.k is not None and rank > self.k):
            k_note = f" in top-{self.k}" if self.k is not None else ""
            return ScoreResult(self.name, False, 0.0, f"no hit{k_note}", {"rank": rank})
        return ScoreResult(self.name, True, 1.0 / rank, "", {"rank": rank})


def _dedup_ranked(output: Any) -> list[str]:
    """有序去重：检索结果同一键多次出现只保留首个位次。非可迭代输入按单元素宽容。"""
    if output is None:
        return []
    if isinstance(output, str) or not hasattr(output, "__iter__"):
        output = [output]
    seen: list[str] = []
    for x in output:
        s = str(x)
        if s not in seen:
            seen.append(s)
    return seen
