"""Facts about saved native chart display, without interpreting model prose."""

from typing import Any


def chart_display_receipt(spec: dict[str, Any], *, has_root_plan: bool = False) -> dict[str, Any]:
    """Describe only renderer behavior that is certain from this chart spec."""
    explicit = spec.get("explicit_presentation")
    if isinstance(explicit, dict):
        from bowhead_core.chart_style_presentation import explicit_plot_columns

        columns = explicit_plot_columns(spec).get("columns")
    else:
        columns = spec.get("columns")
    metadata = (
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
            and column.get("type") in {"numeric", "date", "categorical", "geo"}
        ]
        if isinstance(columns, list)
        else []
    )
    if isinstance(explicit, dict):
        receipt: dict[str, Any] = {
            "explicit_presentation": explicit,
            "selected_columns": [column["key"] for column in columns if isinstance(column, dict) and isinstance(column.get("key"), str)],
            "columns": metadata,
            "column_metadata_complete": len(metadata) == len(columns),
            "rows_available": spec.get("row_count"),
            "rows_inline": len(spec.get("rows") or []),
        }
        if explicit.get("mode") == "plot":
            axes = explicit.get("axes") or {}
            declared = ([axes["x"]] if axes.get("x_scale") == "category" and "x" in axes else []) + explicit.get("group_by", [])
            by_key = {column["key"]: column for column in metadata}
            receipt["dimensions"] = [by_key[key] for key in declared if key in by_key]
    else:
        receipt = {"dimensions": [column for column in metadata if column["type"] != "numeric"]}
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
    if isinstance(explicit, dict) and not any(
        column.get("unit") or column.get("percent_scale") or column.get("format") in {"currency", "percent"}
        for column in columns or []
        if isinstance(column, dict)
    ):
        display = receipt.get("value_display", {})
        for key in ("table_units", "actual_table_units", "actual_axis_unit"):
            display.pop(key, None)
    # Analytical plans may replace the ordinary renderer with a numerical date
    # scale. Do not make an axis claim for them or for a composed chart root.
    if not has_root_plan and not spec.get("views") and not spec.get("presentation_plan") and spec.get("kind") == "line":
        axis: dict[str, Any] = {"scale": "category", "spacing": "equal", "elapsed_time_proportional": False}
        all_dimensions = (
            [column for column in columns if isinstance(column, dict) and column.get("type") in {"date", "categorical", "geo"}]
            if isinstance(columns, list)
            else []
        )
        declared_x = explicit.get("axes", {}).get("x") if isinstance(explicit, dict) else None
        x_column = next((column for column in metadata if column["key"] == declared_x), None)
        if x_column is None and not explicit and len(all_dimensions) == 1:
            x_column = all_dimensions[0]
        if x_column is not None:
            axis["column"] = x_column["key"]
            axis["column_type"] = x_column["type"]
            if x_column["type"] == "date":
                axis["date_pattern"] = x_column.get("date_pattern", "auto")
        receipt["horizontal_axis"] = axis
    return receipt
