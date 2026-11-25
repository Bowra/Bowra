"""Decimal and receipt boundaries independent of the provider."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bowhead_core import answer_cards as cards

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("price", "cost", "fixed", "units"), json.loads((Path(__file__).parent / "fixtures/break-even-cases.json").read_text()))
def test_break_even_units(price, cost, fixed, units):
    result = cards.break_even_units(dict(zip(cards.FIELDS, (price, cost, fixed), strict=True)))
    assert result == (None if units is None else int(units))


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        (0, "0"),
        (1e-6, "0.000001"),
        ("0002.010000", "2.01"),
        (1e-300, None),
        (1e30, None),
        ("0.0000009", None),
        ("9" * 16, None),
        ("", None),
        (None, None),
        (False, None),
        (-1, None),
        (float("nan"), None),
    ],
)
def test_bounded_optional_amounts(value, accepted):
    assert cards.amount(value) == accepted


@pytest.mark.asyncio
async def test_native_card_recovery_and_scope():
    ctx = SimpleNamespace(turn_id="t_math", execution_generation=1, answer_card_entry=True, artifact_presentation_entry=True, language_context=None)
    receipt = await cards.show_break_even(
        {"unit_price": 100, "unit_variable_cost": " ", "fixed_costs": False, "currency": "XYZ", "extra": "ignored"}, ctx
    )
    assert receipt["answer_card"]["initial"] == {"unit_price": "100"}
    assert receipt.ignored == ["fixed_costs"]
    assert "Fixed costs" in receipt["text"] and "Variable cost per unit" in receipt["text"]
    mixed = await cards.show_break_even({"unit_price": 100, "unit_variable_cost": 60, "fixed_costs": 12000, "currency": "CNY/USD"}, ctx)
    assert mixed["answer_card"]["initial"] == {}
    assert "same currency" in mixed["text"]
    ctx.user_text = "Selling price 100 USD, variable cost 60 EUR and fixed costs 12000 USD."
    mixed = await cards.show_break_even({"unit_price": 100, "unit_variable_cost": 60, "fixed_costs": 12000, "currency": "USD"}, ctx)
    assert mixed["answer_card"]["initial"] == {}

    def invocation(result, name=cards.TOOL):
        return SimpleNamespace(tool_name=name, result=result, error=None)

    assert cards.completed_card([invocation(dict(receipt)), invocation(receipt, "other")], ctx) is None
    assert cards.completed_card([invocation(receipt), invocation(receipt)], ctx) is None
    ctx.execution_generation = 2
    assert cards.completed_card([invocation(receipt)], ctx) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("card_entry", "presentation_entry"), [(False, True), (True, False)])
async def test_calculator_requires_an_eligible_chat_turn(card_entry, presentation_entry):
    ctx = SimpleNamespace(answer_card_entry=card_entry, artifact_presentation_entry=presentation_entry)
    result = await cards.show_break_even({}, ctx)
    assert result["ok"] is False
    assert "answer_card" not in result


@pytest.mark.parametrize(
    ("source", "currency", "mixed"),
    [
        ("Price $100, unit cost €60", "USD", True),
        ("Price ¥100, unit cost CNY 60", "CNY", False),
        ("Price $100, unit cost 60 CAD", "CAD", False),
    ],
)
def test_currency_symbols_preserve_ambiguous_units(source, currency, mixed):
    assert cards.mixed_currency(source, currency) is mixed
