"""Chart subsystem —— ChartSpec 契约 + 推断引擎.

见 docs/native-charts-prd.md / native-charts-tech.md.

公共 API:
    - ChartSpec / ChartColumn / ChartKind
    - infer_chart_spec(rows, columns, hint, title, ...) -> ChartSpec
"""

from bowhead_core.chart.infer import infer_chart_spec
from bowhead_core.chart.types import (
    ChartColumn,
    ChartColumnFormat,
    ChartColumnRole,
    ChartColumnType,
    ChartKind,
    ChartSpec,
    spec_to_dict,
)

__all__ = [
    "ChartColumn",
    "ChartColumnFormat",
    "ChartColumnRole",
    "ChartColumnType",
    "ChartKind",
    "ChartSpec",
    "infer_chart_spec",
    "spec_to_dict",
]
