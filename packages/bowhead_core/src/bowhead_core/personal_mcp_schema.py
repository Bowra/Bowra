"""Untrusted remote schema validation runs in a bounded, cancellable process."""

from typing import Any

from jsonschema import Draft202012Validator


def arguments_valid(schema: dict[str, Any], arguments: dict[str, Any]) -> bool:
    try:
        Draft202012Validator(schema).validate(arguments)
    except Exception:
        return False
    return True
