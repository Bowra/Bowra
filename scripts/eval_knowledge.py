#!/usr/bin/env python3
"""Run opt-in knowledge write acceptance through real models and isolated native HTTP services."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ("file_only", "negative", "personal", "space", "ambiguous", "team_denied", "team_admin", "injection", "remove_replay", "english", "helper")


class Evaluation:
    def __init__(self, args: argparse.Namespace, config: dict, version: int) -> None:
        self.args, self.model_config, self.source_version = args, config, version
        self.results: list[dict] = []

    def pytest_configure(self, config) -> None:
        config.knowledge_evaluation = self

    def pytest_collection_modifyitems(self, config, items) -> None:
        selected = set(self.args.case or CASES)
        retained = [item for item in items if item.callspec.params.get("case_id") in selected]
        config.hook.pytest_deselected(items=[item for item in items if item not in retained])
        items[:] = retained


def load_models(args: argparse.Namespace) -> tuple[dict, int]:
    # Credentials remain in process memory; evidence never serializes this configuration.
    if args.config:
        value = json.loads(args.config.read_text())
    else:
        code = (
            "import anyio,json,sys; from bowhead_core.user_store import UserStore; "
            'exec(\'async def main():\\n config,version=await UserStore("/app/.data/sqlite/bowhead.db").get_team_llm_config_with_version(sys.argv[1])\\n print(json.dumps({"config":config,"version":version}))\'); anyio.run(main)'
        )
        command = shlex.join(["docker", "exec", args.container, "python", "-c", code, args.source_team])
        ssh = shutil.which("ssh")
        if ssh is None:
            raise RuntimeError("SSH is unavailable; supply a private model configuration file instead")
        result = subprocess.run(
            [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.ssh_host, command], capture_output=True, text=True, timeout=40
        )
        if result.returncode:
            raise RuntimeError(f"Cannot read the selected model configuration (SSH exit {result.returncode})")
        value = json.loads(result.stdout)
    config = value["config"]
    if not config.get("providers") or any(p.get("type") in {"mock", "refusal"} for p in config["providers"].values()):
        raise ValueError("A real configured model provider is required")
    return config, int(value.get("version", 0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, help="Private JSON containing config and version; never committed")
    source.add_argument("--ssh-host", help="Explicit existing model-configuration host; read-only, no deployment")
    parser.add_argument("--container", default="bowhead-app")
    parser.add_argument("--source-team", help="Exact Team providing the model configuration")
    parser.add_argument("--case", action="append", choices=CASES, default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-timeout", type=int, default=180, choices=range(30, 301))
    parser.add_argument("--max-calls", type=int, default=16, choices=range(1, 33))
    args = parser.parse_args()
    if args.ssh_host and not args.source_team:
        parser.error("--source-team is required with --ssh-host")
    if args.output.exists():
        parser.error("Choose a new evidence path")
    if len(args.case) != len(set(args.case)):
        parser.error("Duplicate cases are not allowed")
    config, version = load_models(args)
    plugin = Evaluation(args, config, version)
    selected = args.case or list(CASES)
    tests = ROOT / "packages/bowhead_core/tests/test_knowledge_model_tasks.py"
    code = pytest.main(["-q", "--tb=short", "-o", f"timeout={args.case_timeout + 60}", f"{tests}::test_knowledge_model_task"], plugins=[plugin])
    complete = {row["id"] for row in plugin.results} == set(selected)
    report = {
        "schema_version": 1,
        "source_config_version": version,
        "mode": "real_model",
        "entry": "native_http_lead",
        "passed": code == 0 and complete and all(row["passed"] for row in plugin.results),
        "complete": complete,
        "cases": plugin.results,
        "missing": sorted(set(selected) - {row["id"] for row in plugin.results}),
        "semantic_review": "required",
        "deployment_acceptance": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return int(code) or int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
