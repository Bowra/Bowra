"""Finite projection for FastAPI/Pydantic request-validation errors."""

from __future__ import annotations

import re
import types
from collections.abc import Mapping
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

from bowhead_core import metrics

VALIDATION_ISSUE_PARAMS: Mapping[str, frozenset[str]] = types.MappingProxyType(
    {
        "field_required": frozenset(),
        "invalid_format": frozenset(),
        "value_too_short": frozenset({"min_length"}),
        "value_too_large": frozenset({"max_length"}),
        "value_out_of_range": frozenset(),
        "invalid_value": frozenset(),
    }
)
VALIDATION_ISSUE_CODES = frozenset(VALIDATION_ISSUE_PARAMS)

_ISSUE_TYPE_MAP = {
    "missing": "field_required",
    "string_too_short": "value_too_short",
    "bytes_too_short": "value_too_short",
    "too_short": "value_too_short",
    "string_too_long": "value_too_large",
    "bytes_too_long": "value_too_large",
    "too_long": "value_too_large",
    "greater_than": "value_out_of_range",
    "greater_than_equal": "value_out_of_range",
    "less_than": "value_out_of_range",
    "less_than_equal": "value_out_of_range",
    "multiple_of": "value_out_of_range",
    "finite_number": "value_out_of_range",
}
_FORMAT_TYPE_SUFFIXES = ("_parsing", "_type")
_FORMAT_TYPES = frozenset(
    {
        "enum",
        "extra_forbidden",
        "frozen_field",
        "frozen_instance",
        "is_instance_of",
        "is_subclass_of",
        "json_invalid",
        "literal_error",
        "mapping_type",
        "model_attributes_type",
        "model_type",
        "string_pattern_mismatch",
        "url_parsing",
        "url_scheme",
        "uuid_version",
    }
)
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_SENSITIVE_SEGMENTS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "refresh_token",
        "secret",
        "token",
    }
)
_MAX_ISSUES = 20
_MAX_PATH_LENGTH = 128
_MAX_NUMERIC_PARAM = 1_000_000


def _segment_is_sensitive(segment: str) -> bool:
    normalized = segment.lower().replace("-", "_")
    return normalized in _SENSITIVE_SEGMENTS or any(normalized.endswith(f"_{suffix}") for suffix in ("password", "secret", "token", "credential"))


def _annotation_variants(annotation: Any) -> tuple[Any, ...]:
    origin = get_origin(annotation)
    if origin is None:
        return (annotation,)
    if origin in (types.UnionType, Union):
        return tuple(item for item in get_args(annotation) if item is not type(None))
    if origin is Annotated:
        args = get_args(annotation)
        return _annotation_variants(args[0]) if args else ()
    return (annotation,)


def _collect_annotation_paths(
    annotation: Any,
    *,
    prefix: tuple[str, ...],
    paths: set[str],
    depth: int,
) -> None:
    if depth > 6:
        return
    for variant in _annotation_variants(annotation):
        origin = get_origin(variant)
        if origin in (list, tuple, set, frozenset):
            args = get_args(variant)
            if args:
                _collect_annotation_paths(
                    args[0],
                    prefix=prefix,
                    paths=paths,
                    depth=depth + 1,
                )
            continue
        if origin is dict:
            if prefix:
                paths.add(".".join(prefix))
            continue
        if isinstance(variant, type) and issubclass(variant, BaseModel):
            for name, model_field in variant.model_fields.items():
                alias = str(model_field.alias or name)
                if not _PATH_SEGMENT_RE.fullmatch(alias):
                    continue
                child = (*prefix, alias)
                path = ".".join(child)
                if len(path) <= _MAX_PATH_LENGTH:
                    paths.add(path)
                    _collect_annotation_paths(
                        model_field.annotation,
                        prefix=child,
                        paths=paths,
                        depth=depth + 1,
                    )
            continue
        if prefix:
            paths.add(".".join(prefix))


def _contains_model(annotation: Any) -> bool:
    for variant in _annotation_variants(annotation):
        origin = get_origin(variant)
        if origin in (list, tuple, set, frozenset):
            args = get_args(variant)
            if args and _contains_model(args[0]):
                return True
        if isinstance(variant, type) and issubclass(variant, BaseModel):
            return True
    return False


def _allowed_paths(request: Any) -> set[str]:
    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return set()
    paths: set[str] = set()
    body_params = tuple(getattr(dependant, "body_params", ()))
    for source, params in (
        ("body", body_params),
        ("query", tuple(getattr(dependant, "query_params", ()))),
    ):
        for param in params:
            field_info = getattr(param, "field_info", None)
            annotation = getattr(field_info, "annotation", None)
            alias = str(getattr(param, "alias", "") or getattr(param, "name", ""))
            flatten_body_model = (
                source == "body" and len(body_params) == 1 and _contains_model(annotation) and not bool(getattr(field_info, "embed", False))
            )
            prefix: tuple[str, ...] = ()
            if not flatten_body_model and _PATH_SEGMENT_RE.fullmatch(alias):
                prefix = (alias,)
            _collect_annotation_paths(
                annotation,
                prefix=prefix,
                paths=paths,
                depth=0,
            )
    return paths


def _safe_path(location: Any, allowed_paths: set[str]) -> str | None:
    if not isinstance(location, (list, tuple)):
        return None
    raw_segments = list(location)
    if raw_segments and raw_segments[0] in {"body", "query"}:
        raw_segments = raw_segments[1:]
    segments = [part for value in raw_segments if isinstance(value, str) and _PATH_SEGMENT_RE.fullmatch(part := value)]
    for length in range(len(segments), 0, -1):
        candidate = ".".join(segments[:length])
        if candidate not in allowed_paths:
            continue
        if len(candidate) > _MAX_PATH_LENGTH:
            return None
        if any(_segment_is_sensitive(segment) for segment in candidate.split(".")):
            return None
        return candidate
    return None


def _issue_code(error_type: Any) -> tuple[str, bool]:
    if not isinstance(error_type, str):
        return "invalid_value", False
    mapped = _ISSUE_TYPE_MAP.get(error_type)
    if mapped is not None:
        return mapped, True
    if error_type in _FORMAT_TYPES or error_type.endswith(_FORMAT_TYPE_SUFFIXES):
        return "invalid_format", True
    return "invalid_value", False


def _safe_params(code: str, context: Any) -> dict[str, int]:
    if not isinstance(context, dict):
        return {}
    param_name = {
        "value_too_short": "min_length",
        "value_too_large": "max_length",
    }.get(code)
    if param_name is None:
        return {}
    value = context.get(param_name)
    if isinstance(value, bool) or not isinstance(value, int):
        return {}
    if value < 0 or value > _MAX_NUMERIC_PARAM:
        return {}
    return {param_name: value}


def project_request_validation_errors(
    request: Any,
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a bounded issue list without Pydantic prose, input, or context."""

    allowed_paths = _allowed_paths(request)
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str, tuple[tuple[str, int], ...]]] = set()
    for error in errors:
        code, known_type = _issue_code(error.get("type"))
        path = _safe_path(error.get("loc"), allowed_paths)
        params = _safe_params(code, error.get("ctx"))
        if path is None:
            code = "invalid_value"
            params = {}
        metrics.inc(
            "bowhead_request_validation_issue_total",
            labels={"mapping": "known" if known_type else "unknown"},
            help="Request-validation issues projected through the finite mapper.",
        )
        key = (path, code, tuple(sorted(params.items())))
        if key in seen:
            continue
        seen.add(key)
        issue: dict[str, Any] = {
            "code": code,
            "params": params,
        }
        if path is not None:
            issue["path"] = path
        projected.append(issue)
        if len(projected) >= _MAX_ISSUES:
            break
    return projected or [{"code": "invalid_value", "params": {}}]


__all__ = [
    "VALIDATION_ISSUE_CODES",
    "VALIDATION_ISSUE_PARAMS",
    "project_request_validation_errors",
]
