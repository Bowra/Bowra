"""Bowhead EvalKit CLI — 跑数据集并输出 JSON 报告.

用法：
    uv run python -m bowhead_core.evalkit.cli run \\
        --dataset datasets/echo_demo.jsonl \\
        --out reports/echo_demo.json \\
        --agent bowhead_core.evalkit.agents:echo_agent

``--agent`` 指 ``module:callable``，callable 要是
``async (EvalCase) -> (output, trace | None)``。
默认 ``bowhead_core.evalkit.agents:echo_agent`` —— 会把 case.input 回显为 output
（用于自测 CLI 管道本身）。

scorer 通过 ``--scorers exact,contains,no_fabrication`` 逗号分隔选，
对应 `scorers.py` 里的 dataclass default 实例；更复杂配置应在代码里直接用
``EvalRunner`` 组装。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path

from bowhead_core.evalkit.cases import load_cases_jsonl
from bowhead_core.evalkit.runner import EvalRunner
from bowhead_core.evalkit.scorers import (
    ContainsScorer,
    ExactMatchScorer,
    NoFabricationScorer,
    NumberToleranceScorer,
    Scorer,
)

_SCORER_ALIASES: dict[str, type] = {
    "exact": ExactMatchScorer,
    "contains": ContainsScorer,
    "number": NumberToleranceScorer,
    "no_fabrication": NoFabricationScorer,
}


def _build_scorers(names: list[str]) -> list[Scorer]:
    out: list[Scorer] = []
    for n in names:
        n = n.strip()
        if not n:
            continue
        if n not in _SCORER_ALIASES:
            raise SystemExit(f"unknown scorer alias: {n}. Valid: {list(_SCORER_ALIASES)}")
        out.append(_SCORER_ALIASES[n]())
    return out


def _load_agent(spec: str):  # type: ignore[no-untyped-def]
    mod_name, _, callable_name = spec.partition(":")
    if not mod_name or not callable_name:
        raise SystemExit(f"agent spec must be module:callable, got {spec!r}")
    mod = importlib.import_module(mod_name)
    if not hasattr(mod, callable_name):
        raise SystemExit(f"module {mod_name} has no attribute {callable_name}")
    return getattr(mod, callable_name)


async def _cmd_run(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset).resolve()
    if not dataset_path.exists():
        return 2
    cases = load_cases_jsonl(dataset_path)
    scorers = _build_scorers(args.scorers.split(","))
    agent = _load_agent(args.agent)
    runner = EvalRunner(
        scorers=scorers,
        concurrency=args.concurrency,
        per_case_timeout_sec=args.timeout,
        dataset_name=dataset_path.name,
    )
    report = await runner.run(cases, agent)
    if args.out:
        out_path = Path(args.out).resolve()
        report.write_json(out_path)
    return 0 if report.pass_rate() >= args.min_pass_rate else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bowhead-eval")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run a dataset through an agent")
    run.add_argument("--dataset", required=True, help="JSONL dataset path")
    run.add_argument("--agent", default="bowhead_core.evalkit.agents:echo_agent", help="module:callable")
    run.add_argument("--scorers", default="exact", help="comma-separated: exact,contains,number,no_fabrication")
    run.add_argument("--out", default=None, help="write JSON report to path")
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--min-pass-rate", type=float, default=0.0, help="CLI exit 1 if pass_rate below this")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
