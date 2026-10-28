"""Load the requested language's simple-lookup prompt templates."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from types import MappingProxyType

import yaml

_TEMPLATE_DIR = Path(__file__).parent / "simple-lookup"
_SUPPORTED_LOCALES = frozenset({"en-US", "zh-CN", "de-DE", "fr-FR"})
_SLOT = re.compile(r"\[\[lookup:([a-z_]+)\]\]")
_LINE_SLOT = re.compile(r"^[ \t]*\[\[lookup:([a-z_]+)\]\][ \t]*$", re.MULTILINE)
_SOURCE_BINDING = re.compile(r"^<!-- bowhead-lookup-templates:([0-9a-f]{64}) -->\n?", re.MULTILINE)
_REQUIRED_KEYS = frozenset(
    {
        "requested_metrics",
        "requested_artifacts",
        "analysis_depth",
        "lookup_workflow",
        "headline_delivery",
        "detail_delivery",
        "execution_sequence",
        "stop_condition",
        "factual_answer",
        "artifact_exception",
        "lead_scope",
        "lead_example",
    }
)


@cache
def load_lookup_templates(output_language: str = "en-US") -> Mapping[str, str]:
    """Read one complete locale file; unsupported locales use English."""
    locale = output_language if output_language in _SUPPORTED_LOCALES else "en-US"
    path = _TEMPLATE_DIR / f"{locale}.yaml"
    templates = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(templates, dict) or set(templates) != _REQUIRED_KEYS:
        raise ValueError(f"Incomplete simple-lookup prompt template: {path.name}")
    if any(not isinstance(value, str) or not value.strip() for value in templates.values()):
        raise ValueError(f"Empty simple-lookup prompt rule: {path.name}")
    if any(_SLOT.search(value) for value in templates.values()):
        raise ValueError(f"Nested simple-lookup prompt rule: {path.name}")
    return MappingProxyType(templates)


def lookup_templates_fingerprint() -> str:
    """Bind every locale's rule bytes into the existing Agent prompt identity."""
    content = {locale: dict(load_lookup_templates(locale)) for locale in sorted(_SUPPORTED_LOCALES)}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bind_lookup_template_source(template: str) -> str:
    """Freeze locale dependencies in the prompt without adding persisted fields."""
    if not _SLOT.search(template):
        return template
    unknown = set(_SLOT.findall(template)) - _REQUIRED_KEYS
    if unknown:
        raise ValueError(f"Unknown simple-lookup prompt rules: {', '.join(sorted(unknown))}")
    fingerprint = lookup_templates_fingerprint()
    bindings = _SOURCE_BINDING.findall(template)
    if bindings:
        if bindings != [fingerprint]:
            raise ValueError("Simple-lookup prompt templates changed; reload the Agent definition")
        return template
    return template + f"\n\n<!-- bowhead-lookup-templates:{fingerprint} -->"


def has_bound_lookup_template_source(template: str) -> bool:
    """Recognize compiled owned templates independently of added preludes."""
    return bool(_SOURCE_BINDING.search(template))


def render_lookup_template(template: str, output_language: str = "en-US") -> str:
    """Fill named slots from the locale file before adding user content."""
    if not _SLOT.search(template):
        return template
    binding = _SOURCE_BINDING.search(template)
    if binding and binding.group(1) != lookup_templates_fingerprint():
        raise ValueError("Simple-lookup prompt templates changed; reload the Agent definition")
    suffix = ""
    if binding:
        # The source binding seals the owned scaffold. Manifest append blocks
        # and team-authored instructions after it retain their original bytes.
        suffix = template[binding.end() :]
        template = template[: binding.start()]
    rules = load_lookup_templates(output_language)
    # Locale resources own list markers and indentation. Formatter-added
    # indentation around a standalone slot must not create a nested list.
    template = _LINE_SLOT.sub(lambda match: rules[match.group(1)], template)
    return _SLOT.sub(lambda match: rules[match.group(1)], template) + suffix
