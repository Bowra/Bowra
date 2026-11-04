"""Explicit source duties and their retained task envelope."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

CONTRACT = "delegated-sources.v1"


def source_url_identity(value: str) -> str:
    """Normalize only the optional root slash, preserving significant URL bytes."""
    return value.strip().rstrip("/")


def required_source_urls(values: object) -> frozenset[str]:
    """Validate declared source inputs without inferring intent from prose."""
    if not isinstance(values, (list, tuple)) or len(values) > 16:
        raise ValueError("required_source_urls must be an array of at most 16 HTTP(S) URLs")
    sources = set()
    for value in values:
        if not isinstance(value, str) or len(value) > 4096 or any(char.isspace() for char in value):
            raise ValueError("Each required source must be an HTTP(S) URL")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Each required source must be an HTTP(S) URL")
        sources.add(source_url_identity(value))
    return frozenset(sources)


def task_envelope(task: str, sources: frozenset[str]) -> str:
    """Carry the declaration in the existing encrypted transcript on continuation."""
    if not sources:
        return task
    return json.dumps({"source_contract": CONTRACT, "task": task, "required_source_urls": sorted(sources)}, ensure_ascii=False)


def retained_sources(messages) -> frozenset[str]:
    """Read the server-created first task envelope from a retained transcript."""
    if not messages or messages[0].role != "user":
        return frozenset()
    try:
        value = json.loads(messages[0].content)
    except (ValueError, TypeError):
        return frozenset()
    if not isinstance(value, dict) or value.get("source_contract") != CONTRACT:
        return frozenset()
    return required_source_urls(value.get("required_source_urls"))
