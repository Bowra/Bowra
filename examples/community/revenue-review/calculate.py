"""Reproduce the synthetic revenue example using only the Python standard library."""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


def calculate() -> dict[str, object]:
    totals = defaultdict(lambda: defaultdict(Decimal))
    segments = defaultdict(dict)
    with Path(__file__).with_name("revenue.csv").open(newline="") as source:
        for row in csv.DictReader(source):
            period = row["period"]
            for field in ("orders", "gross_revenue_usd", "refunds_usd", "variable_cost_usd"):
                totals[period][field] += Decimal(row[field])
            net = Decimal(row["gross_revenue_usd"]) - Decimal(row["refunds_usd"])
            segments[f"{row['region']} / {row['channel']}"][period] = net

    for values in totals.values():
        values["net_revenue_usd"] = values["gross_revenue_usd"] - values["refunds_usd"]
        values["contribution_usd"] = values["net_revenue_usd"] - values["variable_cost_usd"]
        values["contribution_margin"] = values["contribution_usd"] / values["net_revenue_usd"]
        values["refund_rate"] = values["refunds_usd"] / values["gross_revenue_usd"]
        values["net_revenue_per_order_usd"] = values["net_revenue_usd"] / values["orders"]

    first, second = totals["2026-Q1"], totals["2026-Q2"]
    return {
        "synthetic": True,
        "periods": dict(totals),
        "net_revenue_growth": (second["net_revenue_usd"] - first["net_revenue_usd"]) / first["net_revenue_usd"],
        "contribution_margin_change_pp": (second["contribution_margin"] - first["contribution_margin"]) * 100,
        "segment_net_revenue_change_usd": {name: values["2026-Q2"] - values["2026-Q1"] for name, values in segments.items()},
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(calculate(), default=str, indent=2) + "\n")
