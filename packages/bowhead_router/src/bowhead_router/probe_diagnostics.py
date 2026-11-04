"""Bounded, credential-screened diagnostics for empty capability probe responses."""

from __future__ import annotations

import json
import re
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import quote, quote_plus

from bowhead_router.errors import redact_upstream_credentials

_RESPONSE_LIMIT = 64 * 1024
_SECRET_KEY = re.compile(
    r"authorization|cookie|(?:^|[_-])jwt(?:$|[_-])|password|passwd|secret|credential|signature|private.?key|api.?key|auth.?key|(?:access|refresh|id|auth|session)?[_-]?token$",
    re.I,
)
_REQUEST_FIELDS = (
    "model",
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "thinking",
    "thinking_budget",
    "reasoning",
    "reasoning_effort",
    "stream",
)
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "audio_tokens",
    "text_tokens",
    "accepted_prediction_tokens",
    "rejected_prediction_tokens",
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
)
_USAGE_DETAILS = ("input_tokens_details", "output_tokens_details", "prompt_tokens_details", "completion_tokens_details", "cache_creation")


def _usage_counts(value: Any) -> dict[str, Any]:
    """Project numeric usage only; arbitrary provider metadata stays in the bounded excerpt."""

    def counts(fields: dict[str, Any]) -> dict[str, int]:
        return {key: fields[key] for key in _USAGE_FIELDS if type(fields.get(key)) is int and 0 <= fields[key] <= 2**63 - 1}

    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = counts(value)
    for key in _USAGE_DETAILS:
        if isinstance(value.get(key), dict) and (details := counts(value[key])):
            result[key] = details
    return result


def _response_summary(frames: list[Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"model": None, "stop_reason": None, "usage": None, "content_types": []}
    content_types: set[str] = set()
    usage: dict[str, Any] = {}

    def content(value: Any) -> None:
        if isinstance(value, list):
            for block in value:
                content(block)
        elif isinstance(value, dict):
            kind = value.get("type")
            if isinstance(kind, str) and kind in {"text", "output_text", "thinking", "reasoning", "tool_use", "function_call"}:
                if kind not in {"text", "output_text"} or str(value.get("text") or "").strip():
                    content_types.add(kind)
            if kind is None or kind == "message":
                if isinstance(value.get("content"), (str, list)):
                    content(value["content"])
                if isinstance(value.get("reasoning_content"), str) and value["reasoning_content"].strip():
                    content_types.add("reasoning")
                if isinstance(value.get("tool_calls"), list) and value["tool_calls"]:
                    content_types.add("tool_use")
        elif isinstance(value, str) and value.strip():
            content_types.add("text")

    for frame in frames:
        if not isinstance(frame, dict):
            continue
        source = frame
        if str(frame.get("type", "")).startswith("response.") and isinstance(frame.get("response"), dict):
            source = frame["response"]
        if isinstance(source.get("model"), str):
            summary["model"] = source["model"][:256]
        for key, count in _usage_counts(source.get("usage")).items():
            if isinstance(count, dict):
                usage.setdefault(key, {}).update(count)
            else:
                usage[key] = count
        for key in ("stop_reason", "finish_reason"):
            if isinstance(source.get(key), str):
                summary["stop_reason"] = source[key][:128]
        incomplete = source.get("incomplete_details")
        if isinstance(incomplete, dict) and isinstance(incomplete.get("reason"), str):
            summary["stop_reason"] = incomplete["reason"][:128]
        choices = source.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                if isinstance(choice.get("finish_reason"), str):
                    summary["stop_reason"] = choice["finish_reason"][:128]
                content(choice.get("delta") or choice.get("message"))
        elif isinstance(source.get("output"), list):
            content(source["output"])
        else:
            content(source.get("content"))
    summary["usage"] = usage or None
    summary["content_types"] = sorted(content_types)
    return summary


def empty_completion_diagnostics(*, body: dict[str, Any], adapter_type: str, raw: bytes, secret_values: tuple[str, ...]) -> dict[str, Any] | None:
    """Screen all JSON/SSE frames before truncation; diagnostics never change the probe outcome."""
    try:
        text = raw.decode("utf-8")
        lines: list[str] | None = None
        data_lines: dict[int, int] = {}
        frames: list[Any] = []
        if text.lstrip().startswith(("data:", "event:", "id:", "retry:", ":")):
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if line.startswith("data:") and line[5:].strip() not in {"", "[DONE]"}:
                    data_lines[index] = len(frames)
                    frames.append(json.loads(line[5:].strip()))
        else:
            frames.append(json.loads(text))
        if not frames:
            return None

        secrets = {value for value in secret_values if value}

        def collect(value: Any, sensitive: bool = False) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    collect(item, sensitive or bool(_SECRET_KEY.search(key)))
            elif isinstance(value, list):
                for item in value:
                    collect(item, sensitive)
            elif sensitive:
                if isinstance(value, str) and value:
                    secrets.add(value)
                elif isinstance(value, (int, float)):
                    secrets.update((str(value), json.dumps(value)))

        collect(body)
        for frame in frames:
            collect(frame)
        for value in tuple(secrets):
            _, separator, credential = value.partition(" ")
            if separator and credential:
                secrets.add(credential)
            if "=" in value:
                cookie: SimpleCookie = SimpleCookie()
                try:
                    cookie.load(value)
                    secrets.update(item.value for item in cookie.values() if item.value)
                except CookieError:
                    continue
        originals = tuple(secrets)
        secrets.update(quote(value, safe="") for value in originals)
        secrets.update(quote_plus(value) for value in originals)
        ordered_secrets = sorted(secrets, key=len, reverse=True)

        def screen(value: Any) -> Any:
            if isinstance(value, dict):
                return {screen(key): "[REDACTED]" if _SECRET_KEY.search(key) else screen(item) for key, item in value.items()}
            if isinstance(value, list):
                return [screen(item) for item in value]
            if isinstance(value, str):
                for secret in ordered_secrets:
                    value = value.replace(secret, "[REDACTED]")
                return redact_upstream_credentials(value)
            if value is None or isinstance(value, (int, float)):
                if str(value) in secrets or json.dumps(value) in secrets:
                    return "[REDACTED]"
            return value

        frames = [screen(frame) for frame in frames]
        if lines is None:
            response = json.dumps(frames[0], ensure_ascii=False, indent=2)
        else:
            response = "\n".join(
                "data: " + json.dumps(frames[data_lines[index]], ensure_ascii=False) if index in data_lines else screen(line)
                for index, line in enumerate(lines)
            )
        encoded = response.encode("utf-8")
        return {
            "request": screen({"protocol": adapter_type, **{key: body[key] for key in _REQUEST_FIELDS if key in body}}),
            "response_summary": _response_summary(frames),
            "response_body": encoded[:_RESPONSE_LIMIT].decode("utf-8", errors="ignore"),
            "response_truncated": len(encoded) > _RESPONSE_LIMIT,
        }
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return None
