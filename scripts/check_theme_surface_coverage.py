#!/usr/bin/env python3
"""Validate and generate the Theme Appearance surface coverage contract."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config/theme_surface_coverage.yaml"
TS_OUTPUT = ROOT / "packages/bowhead_frontend/lib/theme-surface-registry.generated.ts"
PY_OUTPUT = ROOT / "packages/bowhead_core/src/bowhead_core/theme_surface_registry_generated.py"
GLOBALS_CSS = ROOT / "packages/bowhead_frontend/app/globals.css"
APP_ROOTS = (
    ROOT / "packages/bowhead_frontend/app/(app)",
    ROOT / "packages/bowhead_frontend/app/(public)",
)
VALID_BATCHES = ("C0", "C1", "C2", "C3")
VALID_STATUSES = {"planned", "migrating", "verified", "excepted"}
VALID_DIMENSIONS = {"color", "brand", "typography", "type_scale", "density", "radius", "motion"}
CANONICAL_TOKEN_KEYS = (
    "background",
    "surface",
    "surface2",
    "surface3",
    "line",
    "line2",
    "ink",
    "ink2",
    "ink3",
    "ink4",
    "accent",
    "accentHover",
    "accentInk",
    "accentSoft",
    "accentTint",
    "focus",
    "positive",
    "positiveSoft",
    "warning",
    "warningSoft",
    "error",
    "errorSoft",
    "info",
    "infoSoft",
)
SOURCE_SUFFIXES = {".css", ".py", ".ts", ".tsx"}


def _load_manifest() -> dict[str, Any]:
    raw = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("theme surface manifest must be an object")
    return raw


def _route_for_page(page: Path, app_root: Path) -> str:
    parts: list[str] = []
    for part in page.parent.relative_to(app_root).parts:
        if part.startswith("(") and part.endswith(")"):
            continue
        if part.startswith("[") and part.endswith("]"):
            parts.append("*")
        else:
            parts.append(part)
    return "/" + "/".join(parts) if parts else "/"


def _route_candidates() -> dict[str, Path]:
    candidates: dict[str, Path] = {}
    for app_root in APP_ROOTS:
        for page in sorted(app_root.rglob("page.tsx")):
            route = _route_for_page(page, app_root)
            if route in candidates:
                raise ValueError(f"duplicate route candidate {route}: {candidates[route]} and {page}")
            candidates[route] = page.relative_to(ROOT)
    return candidates


def _matches(route: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return route == prefix or route.startswith(prefix + "/")
    return fnmatch.fnmatchcase(route, pattern)


def _source_files(source_root: Path) -> list[Path]:
    if source_root.is_file():
        return [source_root] if source_root.suffix in SOURCE_SUFFIXES else []
    return [path for path in source_root.rglob("*") if path.is_file() and path.suffix in SOURCE_SUFFIXES]


def _validate_evidence(surface_id: str, evidence: Any, *, required: bool) -> None:
    if not isinstance(evidence, list):
        raise ValueError(f"{surface_id}: evidence must be a list")
    if required and not evidence:
        raise ValueError(f"{surface_id}: verified/excepted surface requires evidence")
    for entry in evidence:
        if not isinstance(entry, str) or ":" not in entry:
            raise ValueError(f"{surface_id}: invalid evidence {entry!r}")
        kind, raw_path = entry.split(":", 1)
        if kind not in {"test", "e2e", "screenshot", "snapshot"}:
            raise ValueError(f"{surface_id}: unsupported evidence kind {kind!r}")
        if not (ROOT / raw_path).exists():
            raise ValueError(f"{surface_id}: evidence does not exist: {raw_path}")


def _validate_exception(surface_id: str, exception: Any, status: str) -> None:
    if status != "excepted":
        if exception is not None:
            raise ValueError(f"{surface_id}: only excepted surfaces may define exception")
        return
    if not isinstance(exception, dict) or set(exception) != {"reason", "owner", "replacement", "expires_on", "issue"}:
        raise ValueError(f"{surface_id}: exception requires reason/owner/replacement/expires_on/issue")
    if any(not exception[key] for key in ("reason", "owner", "replacement", "issue")):
        raise ValueError(f"{surface_id}: exception fields may not be empty")
    expires_on = date.fromisoformat(str(exception["expires_on"]))
    if expires_on < date.today():
        raise ValueError(f"{surface_id}: exception expired on {expires_on.isoformat()}")


def _validate_legacy_aliases(raw: dict[str, Any], canonical_names: set[str]) -> dict[str, str]:
    aliases = raw.get("legacy_aliases")
    if not isinstance(aliases, dict) or not aliases:
        raise ValueError("legacy_aliases must be a non-empty object")
    resolved: dict[str, str] = {}
    css = GLOBALS_CSS.read_text(encoding="utf-8")
    for alias, metadata in aliases.items():
        if not isinstance(alias, str) or not re.fullmatch(r"--[a-z0-9-]+", alias):
            raise ValueError(f"invalid legacy alias {alias!r}")
        if alias in canonical_names:
            raise ValueError(f"legacy alias {alias} is also registered as canonical")
        if not isinstance(metadata, dict) or set(metadata) != {"target", "consumers", "removal_batch"}:
            raise ValueError(f"legacy alias {alias} requires target/consumers/removal_batch")
        target = metadata.get("target")
        consumers = metadata.get("consumers")
        if target not in canonical_names:
            raise ValueError(f"legacy alias {alias} must point directly to a canonical CSS variable")
        if not isinstance(consumers, list) or not consumers or any(not isinstance(item, str) or not item for item in consumers):
            raise ValueError(f"legacy alias {alias} requires non-empty consumers")
        if metadata.get("removal_batch") != "GA":
            raise ValueError(f"legacy alias {alias} removal_batch must be GA")
        declarations = re.findall(
            rf"^\s*{re.escape(alias)}\s*:\s*var\((--[a-z0-9-]+)\)\s*;",
            css,
            re.MULTILINE,
        )
        if not declarations:
            raise ValueError(f"legacy alias {alias} has no one-hop declaration in globals.css")
        if any(value != target for value in declarations):
            raise ValueError(f"legacy alias {alias} diverges from registered target {target}")
        resolved[alias] = target
    return resolved


def _validate_manifest(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if raw.get("schema_version") != 1:
        raise ValueError("theme surface manifest schema_version must be 1")
    completed_through = raw.get("completed_through")
    if completed_through not in VALID_BATCHES:
        raise ValueError("completed_through must be C0, C1, C2, or C3")
    tokens = raw.get("canonical_tokens")
    if not isinstance(tokens, dict) or tuple(tokens) != CANONICAL_TOKEN_KEYS:
        raise ValueError("canonical_tokens must exactly match the frozen 24-token order")
    seen_css_names: set[str] = set()
    for token, css_names in tokens.items():
        if not isinstance(css_names, list) or not css_names:
            raise ValueError(f"canonical token {token} requires at least one CSS variable")
        for css_name in css_names:
            if not isinstance(css_name, str) or not re.fullmatch(r"--[a-z0-9-]+", css_name):
                raise ValueError(f"canonical token {token} has invalid CSS variable {css_name!r}")
            if css_name in seen_css_names:
                raise ValueError(f"CSS variable {css_name} is mapped more than once")
            seen_css_names.add(css_name)
    if tokens["accentInk"] != ["--c-accent-ink"]:
        raise ValueError("accentInk must map directly to --c-accent-ink")
    _validate_legacy_aliases(raw, seen_css_names)

    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        raise ValueError("theme surface manifest requires surfaces")
    ids: set[str] = set()
    patterns: set[str] = set()
    completed_index = VALID_BATCHES.index(completed_through)
    for surface in surfaces:
        if not isinstance(surface, dict):
            raise ValueError("surface entries must be objects")
        surface_id = surface.get("id")
        if not isinstance(surface_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", surface_id):
            raise ValueError(f"invalid surface id {surface_id!r}")
        if surface_id in ids:
            raise ValueError(f"duplicate surface id {surface_id}")
        ids.add(surface_id)
        batch = surface.get("batch")
        status = surface.get("status")
        if batch not in VALID_BATCHES or status not in VALID_STATUSES:
            raise ValueError(f"{surface_id}: invalid batch/status")
        if VALID_BATCHES.index(batch) <= completed_index and status not in {"verified", "excepted"}:
            raise ValueError(f"{surface_id}: completed batch {batch} must be verified or excepted")
        dimensions = surface.get("dimensions")
        if not isinstance(dimensions, list) or not dimensions or set(dimensions) - VALID_DIMENSIONS:
            raise ValueError(f"{surface_id}: invalid dimensions")
        owners = surface.get("owners")
        if not isinstance(owners, list) or not owners or any(not isinstance(owner, str) or not owner for owner in owners):
            raise ValueError(f"{surface_id}: owners must be non-empty strings")
        roots = surface.get("source_roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError(f"{surface_id}: source_roots must not be empty")
        for raw_root in roots:
            if not isinstance(raw_root, str) or not (ROOT / raw_root).exists():
                raise ValueError(f"{surface_id}: missing source root {raw_root!r}")
        routes = surface.get("routes")
        if not isinstance(routes, list):
            raise ValueError(f"{surface_id}: routes must be a list")
        for pattern in routes:
            if not isinstance(pattern, str) or not pattern.startswith("/"):
                raise ValueError(f"{surface_id}: invalid route pattern {pattern!r}")
            if pattern in patterns:
                raise ValueError(f"duplicate route pattern {pattern}")
            patterns.add(pattern)
        _validate_exception(surface_id, surface.get("exception"), status)
        _validate_evidence(surface_id, surface.get("evidence"), required=status in {"verified", "excepted"})
        if status == "verified":
            for guard_root in surface.get("guard_roots", []):
                path = ROOT / guard_root
                if not path.exists():
                    raise ValueError(f"{surface_id}: missing guard root {guard_root}")
                for source_file in _source_files(path):
                    text = source_file.read_text(encoding="utf-8")
                    for pattern in surface.get("forbidden_patterns", []):
                        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                            relative = source_file.relative_to(ROOT)
                            raise ValueError(f"{surface_id}: forbidden pattern {pattern!r} in {relative}")

    candidates = _route_candidates()
    for route, page in candidates.items():
        matches = [surface["id"] for surface in surfaces if any(_matches(route, pattern) for pattern in surface["routes"])]
        if len(matches) != 1:
            raise ValueError(f"route {route} ({page}) must match exactly one surface; got {matches}")
    if completed_through == "C3":
        unfinished = [surface["id"] for surface in surfaces if surface["status"] not in {"verified", "excepted"}]
        if unfinished:
            raise ValueError(f"C3 gate has unfinished surfaces: {unfinished}")
    return surfaces


def _render_typescript(raw: dict[str, Any], surfaces: list[dict[str, Any]]) -> str:
    ids = [surface["id"] for surface in surfaces]
    batches = {surface["id"]: surface["batch"] for surface in surfaces}
    dimensions = {surface["id"]: surface["dimensions"] for surface in surfaces}
    tokens = raw["canonical_tokens"]
    aliases = {key: value["target"] for key, value in raw["legacy_aliases"].items()}
    return (
        "// Generated by scripts/check_theme_surface_coverage.py. Do not edit.\n"
        'import type { ColorSchemeToken } from "./theme-config";\n\n'
        f"export const THEME_SURFACE_IDS = {json.dumps(ids, ensure_ascii=False, indent=2)} as const;\n"
        "export type ThemeSurfaceId = (typeof THEME_SURFACE_IDS)[number];\n\n"
        f'export const THEME_SURFACE_BATCH = {json.dumps(batches, ensure_ascii=False, indent=2)} as const satisfies Record<ThemeSurfaceId, "C0" | "C1" | "C2" | "C3">;\n\n'
        f"export const THEME_SURFACE_DIMENSIONS = {json.dumps(dimensions, ensure_ascii=False, indent=2)} as const satisfies Record<ThemeSurfaceId, readonly string[]>;\n\n"
        f"export const CANONICAL_THEME_CSS_TOKENS = {json.dumps(tokens, ensure_ascii=False, indent=2)} as const satisfies Record<ColorSchemeToken, readonly string[]>;\n\n"
        f"export const LEGACY_THEME_CSS_ALIASES = {json.dumps(aliases, ensure_ascii=False, indent=2)} as const;\n"
    )


def _render_python(raw: dict[str, Any], surfaces: list[dict[str, Any]]) -> str:
    lines = ['"""Generated by scripts/check_theme_surface_coverage.py. Do not edit."""', ""]
    lines.append("THEME_SURFACE_IDS = (")
    lines.extend(f'    "{surface["id"]}",' for surface in surfaces)
    lines.extend((")", "THEME_SURFACE_BATCH = {"))
    lines.extend(f'    "{surface["id"]}": "{surface["batch"]}",' for surface in surfaces)
    lines.extend(("}", "THEME_SURFACE_DIMENSIONS = {"))
    for surface in surfaces:
        values = ", ".join(f'"{value}"' for value in surface["dimensions"])
        lines.append(f'    "{surface["id"]}": ({values}),')
    lines.extend(("}", "CANONICAL_THEME_TOKEN_CSS = {"))
    for key, css_names in raw["canonical_tokens"].items():
        values = ", ".join(f'"{value}"' for value in css_names)
        if len(css_names) == 1:
            values += ","
        lines.append(f'    "{key}": ({values}),')
    lines.extend(("}", "LEGACY_THEME_CSS_ALIASES = {"))
    for alias, metadata in raw["legacy_aliases"].items():
        lines.append(f'    "{alias}": "{metadata["target"]}",')
    lines.extend(("}", ""))
    return "\n".join(lines)


def _sync(path: Path, expected: str, *, write: bool) -> None:
    if write:
        path.write_text(expected, encoding="utf-8")
        return
    actual = path.read_text(encoding="utf-8") if path.exists() else ""
    if actual != expected:
        raise ValueError(f"generated theme registry drift: run {Path(__file__).name} --write ({path.relative_to(ROOT)})")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        raw = _load_manifest()
        surfaces = _validate_manifest(raw)
        _sync(TS_OUTPUT, _render_typescript(raw, surfaces), write=args.write)
        _sync(PY_OUTPUT, _render_python(raw, surfaces), write=args.write)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.stderr.write(f"theme surface coverage error: {exc}\n")
        return 1
    verified = sum(surface["status"] == "verified" for surface in surfaces)
    excepted = sum(surface["status"] == "excepted" for surface in surfaces)
    migrating = sum(surface["status"] == "migrating" for surface in surfaces)
    sys.stdout.write(
        f"theme surfaces: registered={len(surfaces)} verified={verified} "
        f"migrating={migrating} excepted={excepted} routes={len(_route_candidates())}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
