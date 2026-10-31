"""Optional metadata on original authorized settings reads, independent of Agent admission."""

from __future__ import annotations

import logging
from typing import Any

from bowhead_core.config_copilot import get_form
from bowhead_core.configuration_field_guidance import field_contract, project_schema
from bowhead_core.language_context import german_ai_output_enabled

logger = logging.getLogger(__name__)


def page_field_contract(domain: str, view: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call only after the original page's access check; never read settings or grant writes."""
    try:
        registration = get_form(domain)
        source = registration.schema.model_json_schema()
        choices = None
        if domain == "user_language" and not german_ai_output_enabled():
            choices = {"output_language": ["auto", "follow_input", "en-US", "zh-CN", "fr-FR"]}
        schema = project_schema(source, set(registration.field_policy), choices)
        for key, bounds in (view or {}).get("ranges", {}).items():
            if key in schema["properties"]:
                prop = schema["properties"][key]
                for branch in [prop, *prop.get("anyOf", [])]:
                    if branch.get("type") == "integer":
                        branch.update(minimum=bounds["min"], maximum=bounds["max"])
        return {"field_contract": field_contract(domain, schema)}
    except Exception:
        # Metadata cannot break the original editor. Do not include configuration values.
        logger.exception("configuration_field_contract_unavailable domain=%s", domain)
        return {}


def with_field_contract(domain: str, view: dict[str, Any]) -> dict[str, Any]:
    return {**view, **page_field_contract(domain, view)}
