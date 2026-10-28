"""Fail CI when settings pages or source fields have no Configuration Agent coverage decision."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import fields
from pathlib import Path

from bowhead_core.agent_run_limits import AgentRunLimits
from bowhead_core.config_copilot import get_form
from bowhead_core.configuration_contracts import DOMAINS
from bowhead_core.execution_limits import ExecutionLimits
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from bowhead_gateway.configuration_team_models import ModelSettings, ProviderSettings
from bowhead_gateway.routes.admin_llm_config_v2 import V2ModelDeclaration, V2ProviderDraft
from bowhead_gateway.routes.team_settings import AgentRunLimitsBody, _run_limits_view
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]


def settings_routes(root: Path) -> set[str]:
    routes = set()
    for base in (root / "packages/bowhead_frontend/app/(app)", root / "enterprise/frontend/app/(app)"):
        for path in base.rglob("page.tsx"):
            relative = path.parent.relative_to(base).as_posix()
            if relative == "settings" or relative.startswith(("settings/", "space/settings", "team/settings")):
                routes.add("/" + relative + "/")
    dynamic = root / "packages/bowhead_frontend/app/(app)/settings/[tab]/page.tsx"
    if dynamic.exists():
        declaration = dynamic.read_text().split("const PERSONAL_SETTINGS_TABS =", 1)[1].split("];", 1)[0]
        routes.discard("/settings/[tab]/")
        routes.update(f"/settings/{tab}/" for tab in re.findall(r'"([a-z-]+)"', declaration))
    return routes


def check(root: Path = ROOT) -> list[str]:
    inventory = json.loads((root / "config/configuration-coverage.json").read_text())
    failures = []
    actual, declared = settings_routes(root), set(inventory["pages"])
    for path in sorted(actual - declared):
        failures.append(f"Unclassified settings page: {path}. Add an Agent adapter or an explicit manual/workflow reason.")
    for path in sorted(declared - actual):
        failures.append(f"Stale settings page classification: {path}.")
    for path, entry in inventory["pages"].items():
        if not entry.get("reason") or entry.get("status") not in {"agent", "partial", "manual", "workflow", "informational", "redirect"}:
            failures.append(f"Incomplete settings page classification: {path}.")
        for form in entry.get("forms", []):
            if form not in DOMAINS:
                failures.append(f"Unknown configuration form {form} on {path}.")
    register_gateway_forms(FastAPI())
    for form, domain in DOMAINS.items():
        reg = get_form(form)
        if reg is None or reg.read_accessor is None or reg.lite_applier is None or not reg.write_descriptor or not reg.guidance:
            failures.append(f"Incomplete configuration adapter: {form}.")
            continue
        if domain.page not in declared or form not in inventory["pages"][domain.page].get("forms", []):
            failures.append(f"Configuration form {form} has no matching page coverage entry.")
        for name, field in reg.schema.model_fields.items():
            if not field.description:
                failures.append(f"Missing configuration field description: {form}.{name}.")
    for name, source, projection in (("provider", V2ProviderDraft, ProviderSettings), ("model", V2ModelDeclaration, ModelSettings)):
        policy = inventory["source_fields"][name]
        if set(source.model_fields) != set(policy):
            failures.append(f"Unclassified or stale {name} fields: {sorted(set(source.model_fields) ^ set(policy))}.")
        for field, decision in policy.items():
            if decision == "agent" and field not in projection.model_fields:
                failures.append(f"Missing {name} projection: {field}.")
            elif decision != "agent" and field in projection.model_fields:
                failures.append(f"Excluded {name} field exposed to the Agent: {field}.")
    limit_fields = {field.name for field in fields(AgentRunLimits)}
    if limit_fields != set(AgentRunLimitsBody.model_fields) - {"expected_version"} or not limit_fields <= set(_run_limits_view(None)):
        failures.append("Execution-limit schema and original page API disagree; update both in the same change.")
    execution_fields = {field.name for field in fields(ExecutionLimits)}
    if execution_fields != set(inventory["source_fields"]["execution_limits"]) or execution_fields != set(
        get_form("team_execution_limits").schema.model_fields
    ):
        failures.append("Unclassified or stale sandbox/HTML execution-limit fields; update the source contract and coverage inventory together.")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.parse_args()
    failures = check()
    for failure in failures:
        sys.stdout.write(failure + "\n")
    if not failures:
        sys.stdout.write("Configuration coverage: every settings route and admitted source field is classified.\n")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
