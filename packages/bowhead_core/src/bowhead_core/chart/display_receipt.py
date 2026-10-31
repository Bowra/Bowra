"""Facts about saved native chart display, without interpreting model prose."""

from typing import Any


def chart_display_receipt(spec: dict[str, Any], *, has_root_plan: bool = False) -> dict[str, Any]:
    """Describe only renderer behavior that is certain from this chart spec."""
    columns = spec.get("columns")
    dimensions = (
        [
            {
                "key": column["key"],
                "type": column["type"],
                **(
                    {"date_pattern": column["date_pattern"]}
                    if isinstance(column.get("date_pattern"), str) and len(column["date_pattern"]) <= 80
                    else {}
                ),
            }
            for column in columns[:64]
            if isinstance(column, dict)
            and isinstance(column.get("key"), str)
            and len(column["key"]) <= 128
            and column.get("type") in {"date", "categorical", "geo"}
        ]
        if isinstance(columns, list)
        else []
    )
    receipt: dict[str, Any] = {"dimensions": dimensions}
    if spec.get("kind") in {"line", "area_stacked", "bar", "hbar", "stacked_bar", "dual_axis", "combo", "scatter"}:
        receipt["value_display"] = {
            "prepared_rows": "not_modified_by_axis_formatting",
            "axis_magnitude": "renderer_selected_may_change",
            "actual_axis_unit": "not_available_in_tool_receipt",
        }
    elif spec.get("kind") == "table":
        receipt["value_display"] = {
            "prepared_rows": "not_modified_by_table_formatting",
            "table_units": "renderer_may_convert_for_display",
            "actual_table_units": "not_available_in_tool_receipt",
        }
    # Analytical plans may replace the ordinary renderer with a numerical date
    # scale. Do not make an axis claim for them or for a composed chart root.
    if not has_root_plan and not spec.get("views") and not spec.get("presentation_plan") and spec.get("kind") == "line":
        axis: dict[str, Any] = {"scale": "category", "spacing": "equal", "elapsed_time_proportional": False}
        all_dimensions = (
            [column for column in columns if isinstance(column, dict) and column.get("type") in {"date", "categorical", "geo"}]
            if isinstance(columns, list)
            else []
        )
        if len(all_dimensions) == 1 and len(dimensions) == 1:
            axis["column"] = dimensions[0]["key"]
            axis["column_type"] = dimensions[0]["type"]
            if dimensions[0]["type"] == "date":
                axis["date_pattern"] = dimensions[0].get("date_pattern", "auto")
        receipt["horizontal_axis"] = axis
    return receipt
