#!/usr/bin/env python3
"""Validate chart-style manifests and generate the Python/TypeScript registries."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_DIRECTORY = ROOT / "config/chart_style"
REGISTRY_MANIFEST = REGISTRY_DIRECTORY / "registry.json"
PRESENTATION_POLICY = REGISTRY_DIRECTORY / "presentation_policy.json"
PRESET_SCHEMA = REGISTRY_DIRECTORY / "preset.schema.json"
PRESET_DIRECTORY = REGISTRY_DIRECTORY / "presets"
TS_OUTPUT = ROOT / "packages/bowhead_frontend/lib/chart/chart-style-registry.generated.ts"
PY_OUTPUT = ROOT / "packages/bowhead_core/src/bowhead_core/chart_style_registry_generated.py"


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _load_and_validate() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    registry = _load_object(REGISTRY_MANIFEST, label="chart-style registry")
    if set(registry) != {"schemaVersion", "activePresetIds"} or registry.get("schemaVersion") != 1:
        raise ValueError("chart-style registry must contain schemaVersion=1 and activePresetIds")
    active_ids = registry.get("activePresetIds")
    if (
        not isinstance(active_ids, list)
        or not active_ids
        or any(not isinstance(preset_id, str) for preset_id in active_ids)
        or len(active_ids) != len(set(active_ids))
    ):
        raise ValueError("activePresetIds must be a non-empty list of unique strings")

    schema = _load_object(PRESET_SCHEMA, label="chart-style preset schema")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    packages: list[dict[str, Any]] = []
    raw_checksums: dict[str, str] = {}
    for preset_id in active_ids:
        path = PRESET_DIRECTORY / f"{preset_id}.json"
        package = _load_object(path, label=f"chart-style preset {preset_id}")
        errors = sorted(validator.iter_errors(package), key=lambda error: tuple(str(item) for item in error.path))
        if errors:
            locations = "; ".join(f"{'.'.join(str(item) for item in error.path) or '<root>'}: {error.message}" for error in errors)
            raise ValueError(f"{preset_id}: {locations}")
        if package.get("id") != preset_id:
            raise ValueError(f"{preset_id}: package id must match filename and registry id")
        if package.get("status") != "active":
            raise ValueError(f"{preset_id}: active registry entry must have status=active")
        packages.append(package)
        raw_checksums[preset_id] = _sha256(path.read_bytes())

    registered_files = {f"{preset_id}.json" for preset_id in active_ids}
    actual_files = {path.name for path in PRESET_DIRECTORY.glob("*.json")}
    if actual_files != registered_files:
        raise ValueError(
            "preset directory must exactly match activePresetIds: "
            f"missing={sorted(registered_files - actual_files)}, unregistered={sorted(actual_files - registered_files)}"
        )
    presentation_policy = _load_object(PRESENTATION_POLICY, label="chart-style presentation policy")
    if (
        presentation_policy.get("schemaVersion") != 1
        or presentation_policy.get("presentationPolicyVersion") != "chart-presentation/1"
        or presentation_policy.get("renderShapeVersion") != "chart-shape/1"
    ):
        raise ValueError("chart-style presentation policy versions are invalid")
    if set(presentation_policy.get("familiesByKind", {})) != {
        "kpi",
        "line",
        "bar",
        "hbar",
        "pie",
        "stacked_bar",
        "dual_axis",
        "combo",
        "scatter",
        "heatmap",
        "waterfall",
        "sankey",
        "area_stacked",
        "geo",
        "table",
    }:
        raise ValueError("chart-style presentation policy must map every ChartKind")
    if set(presentation_policy.get("reasonCodes", {})) != {
        "staticTooltip",
        "missingExactData",
        "gridStructure",
        "lineWidthMinimum",
        "pointDensity",
        "barRadiusLimited",
        "nonColorRequired",
        "paletteOverflow",
    }:
        raise ValueError("chart-style presentation policy reasonCodes are incomplete")
    if set(presentation_policy.get("line", {}).get("widthPx", {})) != {"thin", "standard", "strong"}:
        raise ValueError("chart-style presentation line width mapping is incomplete")
    if set(presentation_policy.get("bar", {}).get("radiusPx", {})) != {"square", "soft", "round"}:
        raise ValueError("chart-style presentation bar radius mapping is incomplete")
    if not presentation_policy.get("nonColor", {}).get("lineDashes") or not presentation_policy.get("nonColor", {}).get("pointStyles"):
        raise ValueError("chart-style presentation non-color recipes must not be empty")
    return registry, schema, packages, raw_checksums, presentation_policy


def _registry_checksum(registry: dict[str, Any], schema: dict[str, Any], packages: list[dict[str, Any]]) -> str:
    return _sha256(
        _canonical_bytes(
            {
                "generatorContractVersion": 1,
                "registry": registry,
                "presetSchema": schema,
                "presets": packages,
            }
        )
    )


def _render_typescript(
    packages: list[dict[str, Any]],
    raw_checksums: dict[str, str],
    content_checksums: dict[str, str],
    registry_checksum: str,
    presentation_policy: dict[str, Any],
    presentation_policy_checksum: str,
) -> str:
    package_json = json.dumps(packages, ensure_ascii=False, indent=2)
    raw_checksum_json = json.dumps(raw_checksums, ensure_ascii=False, indent=2)
    content_checksum_json = json.dumps(content_checksums, ensure_ascii=False, indent=2)
    presentation_policy_json = json.dumps(presentation_policy, ensure_ascii=False, indent=2)
    return f'''// Generated by scripts/check_chart_style_registry.py. Do not edit.
import type {{ ChartStylePresetId }} from "../theme-config";

export interface ChartStylePresetPackage {{
  schemaVersion: 1;
  presetRevision: string;
  id: ChartStylePresetId;
  labelKey: string;
  descriptionKey: string;
  status: "active";
  extends: "product-base";
  fallbackPresetId: "product-base";
  capabilities: {{
    coveredFamilies: string[];
    requiredRendererFeatures: string[];
    verifiedAccessibilityClaims: string[];
  }};
  tokens: {{
    light: ChartStyleColorTokens;
    dark: ChartStyleColorTokens;
    geometry: ChartStyleGeometryTokens;
  }};
}}

export interface ChartStyleColorTokens {{
  palette: string[];
  sequential: {{ from: string; to: string }};
  diverging: {{ neg: string; mid: string; pos: string }};
}}

export interface ChartStyleGeometryTokens {{
  lineWidth: number;
  pointRadius: number;
  barRadius: number;
  gridDensity: "quiet" | "standard" | "strong";
}}

const packages = {package_json} as unknown as ChartStylePresetPackage[];

export const CHART_STYLE_PRESETS = Object.freeze(
  Object.fromEntries(packages.map((preset) => [preset.id, Object.freeze(preset)])) as Record<
    ChartStylePresetId,
    Readonly<ChartStylePresetPackage>
  >,
);

export const CHART_STYLE_PRESET_IDS = packages.map((preset) => preset.id) as ChartStylePresetId[];

export const CHART_STYLE_PRESET_CHECKSUMS = Object.freeze(
  {raw_checksum_json} satisfies Record<ChartStylePresetId, string>,
);

export const CHART_STYLE_PRESET_CONTENT_SHA256 = Object.freeze(
  {content_checksum_json} satisfies Record<ChartStylePresetId, string>,
);

export const BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM = "{registry_checksum}";

export const CHART_STYLE_PRESENTATION_POLICY = {presentation_policy_json} as const;
export const CHART_STYLE_PRESENTATION_POLICY_CHECKSUM = "{presentation_policy_checksum}";

export function chartStylePreset(id: ChartStylePresetId): Readonly<ChartStylePresetPackage> {{
  return CHART_STYLE_PRESETS[id] ?? CHART_STYLE_PRESETS["clean-blue"];
}}
'''


def _render_python(
    packages: list[dict[str, Any]],
    raw_checksums: dict[str, str],
    content_checksums: dict[str, str],
    registry_checksum: str,
    presentation_policy: dict[str, Any],
    presentation_policy_checksum: str,
) -> str:
    preset_ids = tuple(package["id"] for package in packages)
    releases = {package["id"]: (package["presetRevision"], raw_checksums[package["id"]]) for package in packages}
    package_json = json.dumps(
        {package["id"]: package for package in packages},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    presentation_policy_json = json.dumps(presentation_policy, ensure_ascii=False, separators=(",", ":"))
    lines = [
        '"""Generated by scripts/check_chart_style_registry.py. Do not edit."""',
        "",
        "from __future__ import annotations",
        "",
        "import json",
        "from typing import Any, Final",
        "",
        f'_PRESET_JSON = r"""{package_json}"""',
        "CHART_STYLE_PRESETS: Final[dict[str, dict[str, Any]]] = json.loads(_PRESET_JSON)",
        "del _PRESET_JSON",
        "",
        "ACTIVE_CHART_STYLE_PRESET_IDS: Final = (",
    ]
    lines.extend(f'    "{preset_id}",' for preset_id in preset_ids)
    lines.extend((")", "PRESET_RELEASES: Final[dict[str, tuple[str, str]]] = {"))
    for preset_id, (revision, checksum) in releases.items():
        lines.extend(
            (
                f'    "{preset_id}": (',
                f'        "{revision}",',
                f'        "{checksum}",',
                "    ),",
            )
        )
    lines.extend(("}", "PRESET_CONTENT_SHA256: Final[dict[str, str]] = {"))
    lines.extend(f'    "{preset_id}": "{checksum}",' for preset_id, checksum in content_checksums.items())
    lines.extend(
        (
            "}",
            f'BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM: Final = "{registry_checksum}"',
            "",
            f'_PRESENTATION_POLICY_JSON = r"""{presentation_policy_json}"""',
            "CHART_STYLE_PRESENTATION_POLICY: Final[dict[str, Any]] = json.loads(_PRESENTATION_POLICY_JSON)",
            "del _PRESENTATION_POLICY_JSON",
            f'CHART_STYLE_PRESENTATION_POLICY_CHECKSUM: Final = "{presentation_policy_checksum}"',
            "",
        )
    )
    return "\n".join(lines)


def _sync(path: Path, expected: str, *, write: bool) -> None:
    if write:
        path.write_text(expected, encoding="utf-8")
        return
    actual = path.read_text(encoding="utf-8") if path.exists() else ""
    if actual != expected:
        raise ValueError(f"generated chart-style registry drift: run {Path(__file__).name} --write ({path.relative_to(ROOT)})")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        registry, schema, packages, raw_checksums, presentation_policy = _load_and_validate()
        content_checksums = {package["id"]: _sha256(_canonical_bytes(package)) for package in packages}
        registry_checksum = _registry_checksum(registry, schema, packages)
        presentation_policy_checksum = _sha256(_canonical_bytes(presentation_policy))
        _sync(
            TS_OUTPUT,
            _render_typescript(
                packages,
                raw_checksums,
                content_checksums,
                registry_checksum,
                presentation_policy,
                presentation_policy_checksum,
            ),
            write=args.write,
        )
        _sync(
            PY_OUTPUT,
            _render_python(
                packages,
                raw_checksums,
                content_checksums,
                registry_checksum,
                presentation_policy,
                presentation_policy_checksum,
            ),
            write=args.write,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"chart-style registry error: {exc}\n")
        return 1
    sys.stdout.write(f"chart-style registry: active={len(packages)} checksum={registry_checksum}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
