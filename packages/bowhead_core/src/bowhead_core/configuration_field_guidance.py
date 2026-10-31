"""Value-free field contracts shared by original settings pages and Agent reads."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

ANNOTATION = "x-bowhead-field"


def content_revision(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:20]


def project_schema(source: dict[str, Any], fields: set[str], choices: dict[str, list[Any]] | None = None) -> dict[str, Any]:
    """Select already-authorized fields and only their reachable schema definitions."""
    properties = {name: copy.deepcopy(prop) for name, prop in source.get("properties", {}).items() if name in fields}
    for name, values in (choices or {}).items():
        if name not in properties:
            continue
        for branch in [properties[name], *properties[name].get("anyOf", [])]:
            if "enum" in branch:
                branch["enum"] = [value for value in branch["enum"] if value is None or value in values]
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    required = [name for name in source.get("required", []) if name in properties]
    if required:
        schema["required"] = required
    definitions = source.get("$defs", {})
    selected: dict[str, Any] = {}

    def references(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in selected and name in definitions:
                    selected[name] = copy.deepcopy(definitions[name])
                    references(selected[name])
            for value in node.values():
                references(value)
        elif isinstance(node, list):
            for value in node:
                references(value)

    references(properties)
    if selected:
        schema["$defs"] = selected
    return schema


def field_contract(domain: str, source: dict[str, Any]) -> dict[str, Any]:
    """Expand finite references and attach stable, locale-independent field identities.

    Recursive open-content definitions retain their references. This function never
    reads configuration values or discovers permissions; callers supply a safe schema.
    """
    validate_schema_metadata(source)
    definitions = source.get("$defs", {})

    def expand(node: Any, path: tuple[str, ...], trail: frozenset[str] = frozenset(), *, field: bool = False) -> Any:
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        node = copy.deepcopy(node)
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and ref not in trail:
            definition = definitions.get(ref.removeprefix("#/$defs/"))
            if definition is not None:
                node = {**definition, **{key: value for key, value in node.items() if key != "$ref"}}
                trail = trail | {ref}
        if "properties" in node:
            node["properties"] = {key: expand(value, (*path, key), trail, field=True) for key, value in node["properties"].items()}
        for keyword in ("items", "additionalProperties"):
            if isinstance(node.get(keyword), dict):
                node[keyword] = expand(node[keyword], (*path, "*"), trail)
        for keyword in ("anyOf", "oneOf", "allOf"):
            if keyword in node:
                node[keyword] = [expand(branch, path, trail) for branch in node[keyword]]
        if field:
            metadata = node.get(ANNOTATION, {})
            text = {key: node[key] for key in ("title", "description", "examples") if key in node}
            text.update({key: metadata[key] for key in ("unit", "effect", "empty_behavior", "null_behavior") if key in metadata})
            node[ANNOTATION] = {**metadata, "id": ".".join((domain, *path)), "text_revision": content_revision(text)}
        return node

    schema = expand(source, ())
    # Keep only definitions still referenced by recursive content after expansion.
    schema = project_schema(schema, set(schema.get("properties", {})))
    return {"contract_version": 1, "domain": domain, "schema": schema, "contract_revision": content_revision(schema)}


def contract_fields(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Enumerate authored fields, including repeated nested templates, without values."""
    result: dict[str, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            metadata = node.get(ANNOTATION)
            if isinstance(metadata, dict) and "id" in metadata:
                result[metadata["id"]] = node
            for key, value in node.items():
                if key not in {ANNOTATION, "$defs"}:
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(contract["schema"])
    return result


def field_shape_revision(node: dict[str, Any], *, dynamic_bounds: bool = False) -> str:
    """Review static shape changes separately from translatable meaning and deployment bounds."""

    def shape(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: shape(item)
                for key, item in value.items()
                if key not in {ANNOTATION, "description", "title", "examples", "default"} and not (dynamic_bounds and key in {"minimum", "maximum"})
            }
        if isinstance(value, list):
            return [shape(item) for item in value]
        return value

    return content_revision(shape(node))


def validate_schema_metadata(source: dict[str, Any]) -> None:
    """Reject executable/unknown annotations and credential-bearing synthetic examples."""
    from bowhead_core.team_llm_config import SECRET_KEYS

    allowed = {"id", "text_revision", "unit", "effect", "empty_behavior", "null_behavior", "decimal_minimum", "decimal_maximum", "decimal_places"}

    def example(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if any(
                str(key).lower() in SECRET_KEYS | {"password", "access_token", "refresh_token", "api_key_ref", "auth", "headers", "default_headers"}
                for key in value
            ):
                raise ValueError(f"Credential-bearing field example at {path}; remove it from canonical metadata.")
            for item in value.values():
                example(item, path)
        elif isinstance(value, list):
            for item in value:
                example(item, path)

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            metadata = value.get(ANNOTATION)
            if metadata is not None and (not isinstance(metadata, dict) or set(metadata) - allowed):
                raise ValueError(f"Unsupported field annotation at {path}; use the reviewed annotation contract.")
            if "examples" in value:
                example(value["examples"], path)
            for key, item in value.items():
                visit(item, f"{path}.{key}")
        elif isinstance(value, list):
            for item in value:
                visit(item, path)

    visit(source, "schema")
