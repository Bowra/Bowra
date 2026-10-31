"""Export canonical English field copy and check explicit UI/translation review coverage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bowhead_core.config_copilot import get_form
from bowhead_core.configuration_contracts import DOMAINS
from bowhead_core.configuration_field_guidance import content_revision, contract_fields, field_contract, field_shape_revision
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = Path("packages/bowhead_frontend")
MANIFEST = FRONTEND / "edition/configuration-field-translations.json"
BINDINGS = Path("config/configuration-field-bindings.json")


def export_fields() -> tuple[dict, dict]:
    register_gateway_forms(FastAPI())
    fields, english = {}, {}
    for domain in DOMAINS:
        registration = get_form(domain)
        assert registration is not None
        for field_id, node in contract_fields(field_contract(domain, registration.schema.model_json_schema())).items():
            text = node.get("description", "")
            key = "d_" + content_revision(text)
            english[key] = text
            fields[field_id] = {"key": key, "revision": node["x-bowhead-field"]["text_revision"]}
    return dict(sorted(fields.items())), dict(sorted(english.items()))


def write_english(root: Path = ROOT) -> None:
    """Never approve translations or invent control bindings during generation."""
    fields, english = export_fields()
    path = root / MANIFEST
    manifest = json.loads(path.read_text()) if path.exists() else {"reviewed": {}}
    manifest["fields"] = fields
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    path = root / FRONTEND / "edition/shared-messages/en-US.json"
    messages = json.loads(path.read_text())
    messages["configuration_fields"] = english
    path.write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n")


def check(root: Path = ROOT) -> list[str]:
    try:
        fields, english = export_fields()
    except ValueError as exc:
        return [str(exc)]
    failures = []
    schemas = {
        field_id: node
        for domain in DOMAINS
        for field_id, node in contract_fields(field_contract(domain, get_form(domain).schema.model_json_schema())).items()
    }
    for path in (MANIFEST, BINDINGS):
        if not (root / path).exists():
            failures.append(f"Missing field guidance inventory: {path}")
    if failures:
        return failures
    manifest = json.loads((root / MANIFEST).read_text())
    bindings = json.loads((root / BINDINGS).read_text())
    if manifest.get("fields") != fields:
        failures.append(
            "Stale field contract export. Run uv run python scripts/check_configuration_field_guidance.py --write-english; review affected bindings and translations."
        )
    for field_id in sorted(set(fields) | set(bindings)):
        binding = bindings.get(field_id, {})
        if field_id not in fields:
            failures.append(f"Stale field binding: {field_id}")
        elif not binding.get("reason") or binding.get("kind") not in {
            "control",
            "container",
            "preserved",
            "derived",
            "open_content",
            "compatibility",
        }:
            failures.append(f"Unreviewed field binding: {field_id}")
        elif binding.get("schema_revision") != field_shape_revision(schemas[field_id], dynamic_bounds=field_id.startswith("team_execution_limits.")):
            failures.append(
                f"Field shape changed: {field_id}. Review the original control, constraints and boundary tests; update its binding revision."
            )
        elif binding["kind"] == "control":
            source = root / FRONTEND / binding.get("file", "")
            marker = binding.get("marker", "")
            if not marker or not source.is_file() or marker not in source.read_text():
                failures.append(f"Missing field help binding: {field_id}; check its original control and marker.")
    for locale in ("en-US", "zh-CN", "de-DE", "fr-FR"):
        catalog = json.loads((root / FRONTEND / f"edition/shared-messages/{locale}.json").read_text()).get("configuration_fields", {})
        if locale == "en-US" and catalog != english:
            failures.append("Generated English field descriptions are stale.")
        messages = json.loads((root / FRONTEND / f"edition/shared-messages/{locale}.json").read_text())
        units = messages.get("configuration_guidance", {}).get("units", {})
        for field_id, node in schemas.items():
            unit = node.get("x-bowhead-field", {}).get("unit")
            if unit and not units.get(unit):
                failures.append(f"Missing localized field unit: {locale} {field_id} ({unit}).")
        for field_id, entry in fields.items():
            text = catalog.get(entry["key"])
            if not isinstance(text, str) or not text.strip() or text.startswith("Existing non-secret"):
                failures.append(f"Missing authored field meaning: {locale} {field_id}")
            if locale != "en-US" and manifest.get("reviewed", {}).get(locale, {}).get(field_id) != entry["revision"]:
                failures.append(f"Translation review is stale: {locale} {field_id}")
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-english", action="store_true")
    args = parser.parse_args()
    if args.write_english:
        write_english()
    else:
        errors = check()
        sys.stdout.write(("\n".join(errors) if errors else "Configuration field guidance coverage is complete.") + "\n")
        raise SystemExit(bool(errors))
