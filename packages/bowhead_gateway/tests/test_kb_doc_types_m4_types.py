"""kb-matrix M4 FR-42: 路由层类型集与内核 SSOT 一致性 + options 收发。

- `_FieldIn.type` Literal 无法从常量派生 → 本 UT 锁 get_args == FIELD_TYPES（漂移即红）。
- `_fields_in_to_domain` 透传 options/dimension。
"""

from __future__ import annotations

from typing import get_args, get_type_hints

from bowhead_core.kb.doc_type_store import FIELD_TYPES
from bowhead_gateway.routes.kb_doc_types import _FieldIn, _fields_in_to_domain


def test_field_in_literal_matches_kernel_field_types() -> None:
    hints = get_type_hints(_FieldIn)
    literal_values = get_args(hints["type"])
    assert tuple(literal_values) == FIELD_TYPES, (
        "路由 _FieldIn.type Literal 与内核 doc_type_store.FIELD_TYPES 漂移——两者必须同批改（tech-design-m4 决策 2：一致性 UT 绑死类型集）"
    )


def test_fields_in_to_domain_passes_options_and_dimension() -> None:
    items = [
        _FieldIn(name="情感倾向", type="select", options=["正面", "负面"]),
        _FieldIn(name="金额", type="currency", dimension={"kind": "currency"}),
        _FieldIn(name="备注", type="string"),
    ]
    domain = _fields_in_to_domain(items)
    assert domain[0].options == ["正面", "负面"]
    assert domain[1].dimension == {"kind": "currency"}
    assert domain[2].options is None and domain[2].dimension is None
