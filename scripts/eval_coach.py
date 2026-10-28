#!/usr/bin/env python3
"""Run the isolated Coach task suite with an explicit model route or a self-test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class EvaluationPlugin:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.results: list[dict] = []

    def pytest_configure(self, config):
        config.coach_evaluation = self


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--router-config", type=Path, help="Explicit routing YAML with a real analyst provider; only synthetic fixture data is sent")
    mode.add_argument("--self-test", action="store_true", help="Exercise the same runner, services and scorers with controlled responses")
    parser.add_argument("--case", action="append", default=[], help="Run the named task; repeat for multiple tasks")
    parser.add_argument("--through-lead", action="store_true", help="Run the original Lead loop, Coach dispatch and final Chat composition")
    parser.add_argument("--output", type=Path, required=True, help="Local JSON evidence destination; use a private temporary directory")
    parser.add_argument("--case-timeout", type=int, default=90, choices=range(10, 301), metavar="10..300")
    parser.add_argument("--max-calls", type=int, default=12, choices=range(1, 33), metavar="1..32")
    args = parser.parse_args()
    suite = json.loads((ROOT / "eval/fixtures/coach/tasks-v1.json").read_text())
    known = {case["id"] for case in suite["cases"]}
    if len(args.case) != len(set(args.case)):
        parser.error("Duplicate Coach task ID")
    if set(args.case) - known:
        parser.error("Unknown Coach task ID")
    if args.output.exists():
        parser.error("Output already exists; choose a new evidence path")
    if args.router_config:
        args.router_config = args.router_config.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    plugin = EvaluationPlugin(args)
    tests = ROOT / "packages/bowhead_gateway/tests/test_coach_model_tasks.py"
    selection = [f"{tests}::test_coach_task[{case}]" for case in args.case] if args.case else [str(tests)]
    code = pytest.main(["-q", "--tb=short", "-o", f"timeout={args.case_timeout + 60}", *selection], plugins=[plugin])
    # Missing records (including setup failure) can never produce a passing suite.
    expected = len(args.case) if args.case else len(known)
    complete = len(plugin.results) == expected and {case["id"] for case in plugin.results} == (set(args.case) if args.case else known)
    report = {
        "schema_version": 1,
        "suite_id": suite["suite_id"],
        "mode": "self_test" if args.self_test else "real_model",
        "entry": "lead" if args.through_lead else "coach",
        "pytest_exit_code": int(code),
        "complete": complete,
        "cases": plugin.results,
        "missing_cases": sorted((set(args.case) if args.case else known) - {case["id"] for case in plugin.results}),
        "passed": complete and code == 0 and all(case["passed"] for case in plugin.results),
        "real_model_execution": not args.self_test and any(case.get("physical_attempts") for case in plugin.results),
        "real_model_acceptance": False,
        "semantic_review": "required",
        "deployment_acceptance": False,
    }
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return int(code) if code else (0 if report["passed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
