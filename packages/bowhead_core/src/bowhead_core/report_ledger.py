"""report-consistency-gate v3 — 授权数字台账 核心（确定性，无 LLM）。

设计见 docs/sprints/report-consistency-gate/tech-design.md v3。

**职责分工（code-review v2 后重定）**：
- 数字「是否被授权」的**基数 + 匹配**复用 `number_verifier`（已 native-int/float-aware、
  combo-aware〔a/b、a-b…〕、单位归一、年份/小整数白名单）—— 不再自己重造一套有损的
  unit-class 匹配（旧版漏 bi_query 原生数字、误判 个百分点、且 ×100 magnitude 错被放过）。
- 本模块**唯一新增价值** = 对 analyst 声明的**派生量 formula 做确定性重算校验**
  （combo 只问"能否由某组合得出"，formula 校验问"声明的公式是否真重算出声明值"
  → 抓住 combo 抓不到的"声明了错值"，如户均 163 万 vs 公式重算 4.23 万 = C4）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

# Use number_verifier **raw** extraction (native int/float + string + nested), **never** its
# build_authorized_numbers: scale expansion ×{1e-8…1e8, including ×100} matches written numbers at arbitrary
# magnitude (re-review FN-1: ~44% fabricated 5-digit numbers passed dense tables). Raw only, own matching.
from bowhead_core.number_verifier import _extract_numbers, _flatten_invocations


def _tool_numbers(invocations: Iterable[Any]) -> set[float]:
    """从 tool invocations 抽**原始**数字(不 scale-expand)。native 数(bi_query rows)+
    string + nested 都覆盖(解 F2);不扩量级 → 严格匹配不被量级盲点击穿(FN-1)。"""
    bag: set[float] = set()
    try:
        invs = list(invocations or [])
    except TypeError:
        return bag
    for inv in _flatten_invocations(invs):
        if getattr(inv, "error", None) or getattr(inv, "result", None) is None:
            continue
        _extract_numbers(inv.result, bag)
    return bag


# ── Unit class (formula recompute scale + written-number unit normalization) ──
_UNIT_TABLE: dict[str, tuple[str, float]] = {
    "%": ("pct", 0.01),
    "％": ("pct", 0.01),
    "百分点": ("pp", 1.0),
    "个百分点": ("pp", 1.0),
    "pp": ("pp", 1.0),
    "‰": ("permille", 0.001),
    "亿元": ("cny", 1e8),
    "亿": ("cny", 1e8),
    "万元": ("cny", 1e4),
    "万": ("cny", 1e4),
    "千元": ("cny", 1e3),
    "千": ("cny", 1e3),
    "元": ("cny", 1.0),
    "户": ("count", 1.0),
    "人": ("count", 1.0),
    "个": ("count", 1.0),
    "家": ("count", 1.0),
    "件": ("count", 1.0),
    "次": ("count", 1.0),
    "倍": ("ratio", 1.0),
}


def unit_class(unit: str | None) -> tuple[str, float]:
    """声明单位 token → (class, scale)。未知/空 → ('none', 1.0)。复合(万元/户)取分子。"""
    u = (unit or "").strip()
    if not u:
        return ("none", 1.0)
    head = re.split(r"[/／]", u, maxsplit=1)[0].strip()
    hit = _UNIT_TABLE.get(head) or _UNIT_TABLE.get(u)
    if hit:
        return hit
    m = re.match(r"^(亿|万|千)([户人个家件次笔台项条只])$", head)  # 万笔/亿户 = count×10^k.
    if m:
        return ("count", {"亿": 1e8, "万": 1e4, "千": 1e3}[m.group(1)])
    return ("none", 1.0)


# ── Formula recomputation (AST allowlist, no eval, reject Pow, prevent ** exponent bomb) ──
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_FORMULA_LITERAL_RE = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _eval_node(node: ast.AST) -> float:  # noqa: PLR0911
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ValueError("non-numeric constant")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval_node(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        lft, rgt = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Add):
            return lft + rgt
        if isinstance(node.op, ast.Sub):
            return lft - rgt
        if isinstance(node.op, ast.Mult):
            return lft * rgt
        return lft / rgt  # Div
    raise ValueError(f"disallowed node {type(node).__name__}")  # Pow/Name/Call/… reject.


def _eval_formula(formula: str) -> float | None:
    """重算受限算术（+ - * / ( ) 与数字字面量）。**无 eval**：AST 解析后只走白名单节点，
    `**`(Pow) 被拒 → 杜绝 `9**9**9` 大数炸弹。非法/除零/越界 → None。"""
    f = str(formula or "").strip()
    for a, b in (("×", "*"), ("÷", "/"), ("（", "("), ("）", ")"), ("，", ",")):
        f = f.replace(a, b)
    f = f.replace(",", "")
    if not f or len(f) > 200 or not re.search(r"[+\-*/]", f):
        return None
    try:
        return float(_eval_node(ast.parse(f, mode="eval")))
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError, OverflowError, RecursionError):
        return None


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace(",", "").replace("，", ""))
    except ValueError:
        return None


# Operand magnitude tolerance: model uses display 3.57 for 3.57亿 or full 357000000; allow cny factors,
# **excluding ×100** (percentage factor lets 826 impersonate 8.26 = FN). Base already raw, unexpanded.
_OPERAND_SCALES = (1.0, 1e3, 1e4, 1e8, 1e-3, 1e-4, 1e-8)
_OPERAND_CONSTS = (1.0, 100.0, 1000.0, 10000.0, 1e8)


def _operands_authorized(formula: str, base: set[float]) -> bool:
    """formula 操作数防编造:跳过纯单位常数(1/100/1000/10000/1e8);其余「非常数」操作数里
    **至少一个必须锚定真实数据**(base ∪ 已声明 fact,容 cny 量级因子)。这挡住 `500*326`
    这种全编造小整数乘积(re-review FN-2b);对 `8438*0.5`/`x/12` 不误伤(常数操作数不强求对上)。"""
    base_abs = {abs(b) for b in base}
    saw_operand = False
    for lit in _FORMULA_LITERAL_RE.findall(formula or ""):
        v = _to_float(lit)
        if v is None:
            continue
        av_ = abs(v)
        if av_ in _OPERAND_CONSTS or av_ < 1e-6:
            continue  # Unit constants / near-zero no-op (x+0/x-0/x*1 not data anchors, re-review P2).
        saw_operand = True
        if any(abs(av_ - b * s) <= max(0.01 * b * s, 1e-6) for b in base_abs for s in _OPERAND_SCALES):
            return True  # Data anchor found.
    return not saw_operand  # Pure constants (no suspect operands) pass; operands without anchor fabricate.


def _recompute_matches(recompute: float, value: float, unit: str | None) -> bool:
    """recompute(formula) 是否 == 声明 value。**unit-aware scale**：
    仅 pct(%) 允许 ×1/×100/×0.01（模型 `3.57/8.26` 分数 与 `*100` 百分数 都出现）；
    pp/cny/count/ratio/none 只允许 ×1 —— 否则 100×/10000× magnitude 错(户均 423 vs 4.23万)会被放过。"""
    cls, _ = unit_class(unit)
    # Only pct(%) allows fraction↔percent ×100/×0.01; pp absolute delta has no ambiguity → ×1 only
    # (or 2.1pp claimed 210pp passes). cny/count/ratio/none also ×1 only (blocks 100× magnitude error).
    scales = (1.0, 0.01, 100.0) if cls == "pct" else (1.0,)
    return any(abs(recompute * s - value) <= max(0.02 * abs(value), 0.01) for s in scales)


def verify_formula(formula: str, value: float, unit: str | None, authorized: set[float]) -> tuple[bool, str]:
    """派生量校验：操作数 ∈ 授权集 且 recompute(formula) ≈ 声明 value(unit-aware)。
    返回 (ok, reason ∈ {ok, unparseable, operand_unverified, recompute_mismatch})。"""
    r = _eval_formula(formula)
    if r is None:
        return (False, "unparseable")
    if not _operands_authorized(formula, authorized):
        return (False, "operand_unverified")
    if not _recompute_matches(r, value, unit):
        return (False, f"recompute_mismatch(formula→{r:g} vs value {value:g})")
    return (True, "ok")


# ── Ledger assembly ────────────────────────────────────────────
@dataclass
class AuthorizedLedger:
    derived_values: set[float] = field(default_factory=set)  # Formula-verified derived values (raw+canonical).
    rejected: list[dict[str, Any]] = field(default_factory=list)  # Failed ground_facts.


def build_ledger(ground_facts: Iterable[Any], invocations: Iterable[Any]) -> AuthorizedLedger:
    """对 analyst 声明的 ground_facts 做 formula 校验。

    - 授权基数 = `_tool_numbers`(真 tool 输出的**原始**数, native+string+nested, 不 expand)。
    - operand 池 = 授权基数 ∪ 已声明 fact 值(支持派生引用派生)。
    - 派生量(带 formula): verify_formula → 通过进 derived_values, 失败进 rejected。
    - 给定基数(无 formula): 值须能对到授权基数, 否则 rejected(疑似凭空给定)。
    """
    try:
        gfs = list(ground_facts or [])
    except TypeError:
        gfs = []
    authorized = _tool_numbers(invocations)  # Raw tool numbers, no scale expansion.
    declared_abs = {abs(_gf_fields(gf)[2]) for gf in gfs}
    operand_pool = set(authorized) | declared_abs
    led = AuthorizedLedger()
    for gf in gfs:
        key, label, value, unit, formula, source = _gf_fields(gf)
        _cls, scale = unit_class(unit)
        if (formula or "").strip():
            ok, reason = verify_formula(formula, value, unit, operand_pool)
            if ok:
                led.derived_values.add(round(value, 6))
                led.derived_values.add(round(value * scale, 6))
            else:
                led.rejected.append({"key": key, "label": label, "value": value, "formula": formula, "reason": reason})
        elif not _value_authorized(value, authorized):
            led.rejected.append({"key": key, "label": label, "value": value, "reason": "given_value_not_in_tool_output", "source": source})
    return led


def _value_authorized(value: float, authorized: set[float], *, rel: float = 0.02) -> bool:
    return any(abs(value - a) <= max(rel * abs(a), 1e-6) for a in authorized)


# ── Reconciliation ─────────────────────────────────────────────
@dataclass
class ReconcileResult:
    rejected_facts: list[dict[str, Any]] = field(default_factory=list)  # Failed ground_facts.
    unauthorized: list[dict[str, Any]] = field(default_factory=list)  # Written unauthorized numbers.

    @property
    def clean(self) -> bool:
        return not self.rejected_facts and not self.unauthorized


# Written number + unit tokens (个百分点/百分点 before bare 个 to avoid cutting short).
# `(?<!\d)` lookahead: interdigit - (range 3-5万) is no negative sign; avoid fabricated -5万 (FP-4).
# Units include common off-table measures (套/吨/美元/平方米…); otherwise unit='' skips fabricated numbers (FN-3).
_NUM_UNIT_RE = re.compile(
    r"(?<!\d)(-?\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*"
    r"(亿元|万元|千元|个百分点|百分点|[亿万千][户人个家件次笔台项条只]|万吨|"
    r"亿|万|千|元|户|人|个|家|件|次|笔|台|套|吨|度|美元|平方米|㎡|倍|pp|‰|%|％)?",
)


def extract_numbers(text: str) -> list[tuple[float, str]]:
    """从写出文本抽 (value, unit_token)。仅供对账扫描；基数走 number_verifier。"""
    out: list[tuple[float, str]] = []
    for m in _NUM_UNIT_RE.finditer(text or ""):
        v = _to_float(m.group(1))
        if v is not None:
            out.append((v, m.group(2) or ""))
    return out


def _is_year_like(v: float) -> bool:
    """年份样整数（1900–2100）——非台账数字，C1 归 date-anchor 管，跳过。"""
    return float(v).is_integer() and 1900 <= v <= 2100


def _authorized(value: float, canon: float, base: set[float], derived: set[float], *, rel: float = 0.02) -> bool:
    """写出数字是否被授权：raw 值 或 canonical(value×单位scale) ≈ 某**原始** tool 数
    (base 不 scale-expand —— 否则量级盲点会放过 ~44% 编造数, re-review FN-1) 或某
    formula-verified 派生值。**严格匹配**(无小整数白名单/5%/combo),故 C3/C4 写错值能被抓。"""
    for c in (value, canon):
        if any(abs(c - b) <= max(rel * abs(b), 1e-6) for b in base):
            return True
        if any(abs(c - d) <= max(rel * abs(d), 0.01) for d in derived):
            return True
    return False


def reconcile_payload(payload: Any, invocations: Iterable[Any]) -> ReconcileResult:
    """intra-payload 自对账：payload 写出的每个带单位数字必须被授权。

    base = ``_tool_numbers``（真 tool 输出的**原始**数, native int/float + string + nested,
    解了 bi_query 原生数 base-empty 的 F2；**不** scale-expand 避免量级盲点 FN-1）;授权判定走本模块
    **严格匹配**(_authorized) + formula-verified 派生量。裸数字/年份跳过(宁严勿扰)。
    """
    base = _tool_numbers(invocations)  # Raw tool numbers, strict match, no scale expansion.
    gfs = payload.get("ground_facts") if isinstance(payload, dict) else getattr(payload, "ground_facts", None)
    led = build_ledger(gfs or [], invocations)
    unauth: list[dict[str, Any]] = []
    for field_name, text in _payload_text_fields(payload):
        for v, u in extract_numbers(text):
            if not u or _is_year_like(v):
                continue
            _, scale = unit_class(u)
            if _authorized(v, v * scale, base, led.derived_values):
                continue
            unauth.append({"field": field_name, "value": v, "unit": u, "raw": f"{v:g}{u}"})
    return ReconcileResult(rejected_facts=led.rejected, unauthorized=unauth)


def _payload_text_fields(payload: Any) -> list[tuple[str, str]]:
    def g(obj: Any, key: str, default: Any = "") -> Any:
        return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

    out: list[tuple[str, str]] = [("exec_summary", str(g(payload, "exec_summary") or ""))]
    for i, k in enumerate(g(payload, "kpis") or []):
        out.append((f"kpis[{i}].value", str(g(k, "value") or "")))
    for i, ins in enumerate(g(payload, "insights") or []):
        out.append((f"insights[{i}].thesis", str(g(ins, "thesis") or "")))
        out.append((f"insights[{i}].narrative", str(g(ins, "narrative") or "")))
    return out


def _gf_fields(gf: Any) -> tuple[str, str, float, str, str, str]:
    """GroundFact 或 dict → 字段元组。value 复用 analysis_payload 的 coercion(去千分位、排 bool)。"""
    from bowhead_core.analysis_payload import _groundfact_from_dict

    if isinstance(gf, dict):
        g = _groundfact_from_dict(gf)
        return (g.key, g.label, g.value, g.unit, g.formula, g.source)
    return (
        getattr(gf, "key", ""),
        getattr(gf, "label", ""),
        float(getattr(gf, "value", 0.0) or 0.0),
        getattr(gf, "unit", ""),
        getattr(gf, "formula", ""),
        getattr(gf, "source", ""),
    )
