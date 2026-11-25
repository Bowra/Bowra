"""The native break-even answer card shared by ordinary Lead and its finalizer."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from bowhead_router.types import ToolSchema

from bowhead_core.chart.numeric_display import currency_identity
from bowhead_core.report_chrome_text import chrome_lang
from bowhead_core.tool_outcome import invocation_result
from bowhead_core.tool_registry import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_core.tool_registry import ToolExecutionContext

TOOL = "show_break_even"
FIELDS = ("unit_price", "unit_variable_cost", "fixed_costs")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# Restrict prose evidence to explicit currency codes next to amounts or unambiguous currency names.
_AMOUNT_CODE = re.compile(r"(?<![A-Za-z])([A-Z]{3})\s*(?=[0-9])|(?<=[0-9])\s*([A-Z]{3})(?![A-Za-z])")
_CURRENCY_NAMES = {"美元": "USD", "人民币": "CNY", "欧元": "EUR", "英镑": "GBP", "日元": "JPY", "港元": "HKD"}
_SYMBOL = re.compile(r"([$€£¥￥])\s*(?=[0-9])|(?<=[0-9])\s*([$€£¥￥])")
_CODE_SYMBOLS = {"EUR": "€", "GBP": "£", "CNY": "¥", "JPY": "¥", "USD": "$", "CAD": "$", "AUD": "$", "HKD": "$"}
_COPY = {
    "en-US": (
        ("Unit price", "Variable cost per unit", "Fixed costs"),
        "Break-even quantity (units): {units}.",
        "At this price each unit contributes zero or less. Positive fixed costs cannot be recovered.",
        "Enter {fields} to calculate the break-even quantity.",
        "Re-enter {fields} as a nonnegative decimal with at most 15 digits and 6 decimal places.",
        "Enter all three amounts in the same currency.",
    ),
    "zh-CN": (
        ("销售单价", "单位变动成本", "固定成本"),
        "盈亏平衡销量为 {units} 件。",
        "按此单价，每件产品的边际贡献小于或等于零，无法收回正的固定成本。",
        "请填写{fields}以计算盈亏平衡销量。",
        "请重新填写{fields}，使用最多 15 位数字、最多 6 位小数的非负金额。",
        "请把三项金额统一为同一币种后填写。",
    ),
    "de-DE": (
        ("Stückpreis", "Variable Stückkosten", "Fixkosten"),
        "Absatzmenge an der Gewinnschwelle (Stück): {units}.",
        "Der Deckungsbeitrag pro Stück ist null oder negativ. Die positiven Fixkosten werden nicht gedeckt.",
        "Für die Berechnung bitte folgende Werte eingeben: {fields}.",
        "Bitte {fields} erneut als nichtnegative Zahl mit bis zu 15 Ziffern und 6 Nachkommastellen eingeben.",
        "Bitte alle drei Beträge in derselben Währung eingeben.",
    ),
    "fr-FR": (
        ("Prix unitaire", "Coût variable unitaire", "Coûts fixes"),
        "Seuil de rentabilité en unités : {units}.",
        "La marge unitaire est nulle ou négative. Les coûts fixes positifs ne peuvent pas être couverts.",
        "Pour calculer le seuil, renseignez les champs suivants : {fields}.",
        "Corrigez les champs suivants avec un montant positif ou nul de 15 chiffres maximum, dont 6 décimales : {fields}.",
        "Saisissez les trois montants dans la même devise.",
    ),
}


def amount(value: Any) -> str | None:
    """Return the canonical amount or None for an unusable or absent field."""
    if isinstance(value, str):
        value = value.strip()
        if len(value) > 64 or not _DECIMAL.fullmatch(value):
            return None
    elif type(value) in (int, float):
        if not 0 <= value < 10**15:
            return None
        number = Decimal(str(value))
        if number and number.adjusted() < -6:
            return None
        value = format(number, "f").lstrip("-")
    else:
        return None
    whole, _, fraction = value.partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    if len(whole) + len(fraction) > 15 or len(fraction) > 6:
        return None
    return whole + "." + fraction if fraction else whole


def break_even_units(initial: dict[str, str]) -> int | None:
    """Compute with integer millionths. None means no finite threshold."""
    price, cost, fixed = (int(Decimal(initial[key]) * 1_000_000) for key in FIELDS)
    if fixed == 0:
        return 0
    margin = price - cost
    return (fixed + margin - 1) // margin if margin > 0 else None


class BreakEvenReceipt(dict):
    """Only the native handler mints a receipt. Its private scope survives result capping."""

    def __init__(self, ctx: ToolExecutionContext, initial: dict[str, str], ignored: list[str], text: str):
        super().__init__(text=text, answer_card={"kind": "break_even", "version": 1, "initial": initial})
        self.turn_id = ctx.turn_id
        self.generation = ctx.execution_generation
        self.ignored = ignored


def native_receipt(inv: Any, ctx: ToolExecutionContext) -> BreakEvenReceipt | None:
    if getattr(inv, "tool_name", None) != TOOL or inv.error is not None:
        return None
    result = invocation_result(inv)
    if type(result) is BreakEvenReceipt and result.turn_id == ctx.turn_id and result.generation == ctx.execution_generation:
        return result
    return None


def completed_card(invocations: Sequence[Any], ctx: ToolExecutionContext) -> dict | None:
    receipts = [receipt for inv in invocations if (receipt := native_receipt(inv, ctx)) is not None]
    return receipts[0]["answer_card"] if len(receipts) == 1 else None


def mixed_currency(request: str, currency_text: str) -> bool:
    """Ambiguous symbols can agree with a stated code; incompatible identities need new assumptions."""
    codes = {code for word in re.findall(r"\b[A-Za-z]{3}\b", currency_text) if (code := currency_identity(word))}
    for match in _AMOUNT_CODE.finditer(request):
        if code := currency_identity(match[1] or match[2]):
            codes.add(code)
    codes.update(code for name, code in _CURRENCY_NAMES.items() if name in request)
    if len(codes) > 1:
        return True
    symbols = {(match[1] or match[2]).replace("￥", "¥") for match in _SYMBOL.finditer(request)}
    symbols.update(_CODE_SYMBOLS[code] for code in codes if code in _CODE_SYMBOLS)
    return len(symbols) > 1


async def show_break_even(args: dict[str, Any], ctx: ToolExecutionContext) -> dict:
    if not (ctx.answer_card_entry and ctx.artifact_presentation_entry):
        return {"ok": False, "error": "The calculator is unavailable for this turn. Continue with the ordinary answer."}
    language = chrome_lang(ctx.language_context)
    labels, finite, unreachable, missing_prompt, invalid_prompt, currency_prompt = _COPY[language]
    initial = {}
    ignored = []
    for key in FIELDS:
        raw = args.get(key)
        normalized = amount(raw)
        if normalized is not None:
            initial[key] = normalized
        elif raw is not None and not (isinstance(raw, str) and not raw.strip()):
            ignored.append(key)
    currency_arg = args.get("currency")
    currency_text = currency_arg[:80] if isinstance(currency_arg, str) else ""
    currency = currency_identity(currency_text)
    request = getattr(ctx, "user_text", "") or getattr(ctx, "_psi_turn_user_text", "") or ""
    if mixed_currency(request, currency_text):
        return BreakEvenReceipt(ctx, {}, list(FIELDS), currency_prompt)
    if currency:
        initial["currency"] = currency
    sep = "、" if language == "zh-CN" else ", "
    assumptions = sep.join(
        f"{label} {initial[key]}{(' ' + currency) if currency else ''}" for key, label in zip(FIELDS, labels, strict=True) if key in initial
    )
    lines = [assumptions] if assumptions else []
    missing = [label for key, label in zip(FIELDS, labels, strict=True) if key not in initial and key not in ignored]
    if missing:
        lines.append(missing_prompt.format(fields=sep.join(missing)))
    if ignored:
        lines.append(invalid_prompt.format(fields=sep.join(labels[FIELDS.index(key)] for key in ignored)))
    if not missing and not ignored:
        units = break_even_units(initial)
        lines.append(unreachable if units is None else finite.format(units=units))
    return BreakEvenReceipt(ctx, initial, ignored, "\n\n".join(lines))


def break_even_spec() -> ToolSpec:
    return ToolSpec(
        schema=ToolSchema(
            name=TOOL,
            description=(
                "Show an editable break-even calculator for a standalone scenario. Copy only user-supplied amounts. "
                "Omit unknowns. If currencies differ, ask for one currency before using this tool."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "unit_price": {"type": "string", "description": "Selling price per unit, as a decimal."},
                    "unit_variable_cost": {"type": "string", "description": "Variable cost per unit, as a decimal."},
                    "fixed_costs": {"type": "string", "description": "Total fixed costs, as a decimal."},
                    "currency": {"type": "string", "description": "Currency code when stated, such as CNY."},
                },
                "additionalProperties": False,
            },
        ),
        handler=show_break_even,
        allowed_for_roles=frozenset({"lead"}),
    )
