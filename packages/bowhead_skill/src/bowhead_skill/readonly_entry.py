"""Versioned, opt-in Skill read-only declarations. Validation grants no access.

The package bytes (including this frontmatter) still need normal content review.
A declaration does not prove natural-language instructions have no side effects;
resource authorization, dependency closure and immutable materialization remain
prepare/dispatch responsibilities. Ordinary baseline references may project only
the manifest body; extensions require an explicit declaration. Neither parsing
path grants invocation or resource access.
"""

from __future__ import annotations

import json
import math
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

MAX_CONTRACT_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 16 * 1024
MAX_PARAMETER_BYTES = 16 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_ENTRY_BYTES = 1024 * 1024
MAX_SAFE_INTEGER = (1 << 53) - 1
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,512}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_TEXT_SUFFIXES = frozenset({".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml"})
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_FORMATS = FormatChecker(formats=["date"])


class ReadOnlyEntryError(ValueError):
    """Finite reason only: never embed a parameter, schema or private path."""


def _fail(reason: str) -> None:
    raise ReadOnlyEntryError(reason) from None


def _json_copy(value: object, *, max_bytes: int, max_depth: int = 16) -> Any:
    pending = [(value, 0)]
    count = 0
    string_bytes = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 8192:
            _fail("skill_readonly_json_limit")
        if type(item) in (dict, list):
            if depth >= max_depth or len(item) > 512:
                _fail("skill_readonly_json_limit")
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    _fail("skill_readonly_invalid_json")
                pending.extend((child, depth + 1) for child in (*item.keys(), *item.values()))
            else:
                pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            try:
                string_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                _fail("skill_readonly_invalid_json")
            if string_bytes > max_bytes:
                _fail("skill_readonly_json_limit")
        elif type(item) in (int, float):
            if (type(item) is float and not math.isfinite(item)) or abs(item) > MAX_SAFE_INTEGER:
                _fail("skill_readonly_invalid_json")
        elif item is not None and type(item) is not bool:
            _fail("skill_readonly_invalid_json")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > max_bytes:
        _fail("skill_readonly_json_limit")
    return json.loads(encoded)


def _path(raw: object) -> str:
    if type(raw) is not str or not raw or len(raw) > 256 or any(ord(char) < 32 for char in raw) or "\\" in raw or ":" in raw:
        _fail("skill_readonly_invalid_file")
    path = PurePosixPath(raw)
    if path.is_absolute() or path.as_posix() != raw or ".." in path.parts or any(part.casefold() == "scripts" for part in path.parts):
        _fail("skill_readonly_invalid_file")
    if path.suffix.casefold() not in _TEXT_SUFFIXES:
        _fail("skill_readonly_invalid_file")
    return raw


def _schema(raw: object) -> dict[str, Any]:
    """Bounded JSON Schema 2020-12 subset shared with the forthcoming form reader.

    No refs, patterns, composition keywords or custom formats: validation cannot fetch
    documents, execute expressions or silently ignore an unsupported constraint.
    Nullable scalar/object/array types are explicit type + null unions.
    """
    schema = _json_copy(raw, max_bytes=MAX_SCHEMA_BYTES)
    if type(schema) is not dict or schema.get("type") != "object":
        _fail("skill_readonly_invalid_schema")
    pending = [(schema, 0)]
    nodes = 0
    checked: list[dict[str, Any]] = []
    while pending:
        node, depth = pending.pop()
        nodes += 1
        if type(node) is not dict or nodes > 128 or depth > 6:
            _fail("skill_readonly_invalid_schema")
        raw_type = node.get("type")
        if type(raw_type) is list:
            if len(raw_type) != 2 or "null" not in raw_type or any(type(item) is not str for item in raw_type):
                _fail("skill_readonly_invalid_schema")
            kind = next((item for item in raw_type if item != "null"), None)
        else:
            kind = raw_type
        if type(kind) is not str or kind not in _TYPES:
            _fail("skill_readonly_invalid_schema")
        allowed = {"type", "title", "description", "enum", "default"}
        if kind == "object":
            allowed |= {"properties", "required", "additionalProperties"}
            properties = node.get("properties")
            if node.get("additionalProperties") is not False or type(properties) is not dict or len(properties) > 64:
                _fail("skill_readonly_invalid_schema")
            if any(not key or len(key) > 128 or any(ord(char) < 32 for char in key) for key in properties):
                _fail("skill_readonly_invalid_schema")
            required = node.get("required", [])
            if type(required) is not list or any(type(key) is not str or key not in properties for key in required):
                _fail("skill_readonly_invalid_schema")
            pending.extend((child, depth + 1) for child in properties.values())
        elif kind == "array":
            allowed |= {"items", "minItems", "maxItems"}
            pending.append((node.get("items"), depth + 1))
        elif kind == "string":
            allowed |= {"minLength", "maxLength", "format"}
            if "format" in node and node["format"] != "date":
                _fail("skill_readonly_invalid_schema")
        elif kind in {"integer", "number"}:
            allowed |= {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
        if set(node) - allowed:
            _fail("skill_readonly_invalid_schema")
        for key, maximum in (("title", 256), ("description", 2048)):
            if key in node and (type(node[key]) is not str or len(node[key]) > maximum):
                _fail("skill_readonly_invalid_schema")
        if "enum" in node and (type(node["enum"]) is not list or not 1 <= len(node["enum"]) <= 64):
            _fail("skill_readonly_invalid_schema")
        checked.append(node)
    # Complete the whitelist walk before validating ANY defaults: an unsupported
    # ref hidden below a root default must never reach jsonschema's resolver.
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        _fail("skill_readonly_invalid_schema")
    for node in checked:
        try:
            validator = Draft202012Validator(node, format_checker=_FORMATS)
            if "default" in node:
                validator.validate(node["default"])
            for value in node.get("enum", []):
                validator.validate(value)
        except (SchemaError, ValidationError):
            _fail("skill_readonly_invalid_schema")
    return schema


def validate_parameter_schema(raw: object) -> dict[str, Any]:
    """Return a detached schema accepted by the shared Composer form reader."""
    return _schema(raw)


@dataclass(frozen=True, slots=True, init=False)
class ReadOnlyEntry:
    """Canonical JSON prevents nested defaults/dependencies changing after validation."""

    _canonical_json: str

    def __init__(self, raw: object) -> None:
        data = _parse_data(raw)
        object.__setattr__(self, "_canonical_json", json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_json)

    @property
    def entry_file(self) -> str:
        return self.to_dict()["entry_file"]

    @property
    def required_files(self) -> tuple[str, ...]:
        return tuple(self.to_dict()["required_files"])

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return self.to_dict()["parameter_schema"]

    def validate_parameters(self, parameters: object) -> dict[str, Any]:
        values = _json_copy(parameters, max_bytes=MAX_PARAMETER_BYTES, max_depth=8)
        try:
            Draft202012Validator(self.parameter_schema, format_checker=_FORMATS).validate(values)
        except ValidationError:
            _fail("skill_readonly_invalid_parameters")
        return values


def _parse_data(raw: object) -> dict[str, Any]:
    data = _json_copy(raw, max_bytes=MAX_CONTRACT_BYTES)
    keys = {
        "schema_version",
        "entry_file",
        "required_files",
        "parameter_schema",
        "required_skill_refs",
        "requires_script",
        "requires_new_tools",
        "requires_new_connections",
    }
    if type(data) is not dict or set(data) != keys or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        _fail("skill_readonly_invalid_declaration")
    if any(data[key] is not False for key in ("requires_script", "requires_new_tools", "requires_new_connections")):
        _fail("skill_readonly_invalid_declaration")
    _path(data["entry_file"])
    files = data["required_files"]
    if type(files) is not list or len(files) > 32:
        _fail("skill_readonly_invalid_file")
    paths = [_path(item) for item in [data["entry_file"], *files]]
    if len({path.casefold() for path in paths}) != len(paths):
        _fail("skill_readonly_invalid_file")
    refs = data["required_skill_refs"]
    if type(refs) is not list or len(refs) > 16:
        _fail("skill_readonly_invalid_dependency")
    identities: set[tuple[str, str]] = set()
    for ref in refs:
        if type(ref) is not dict or set(ref) != {"kind", "source_kind", "id", "version"} or ref["kind"] != "skill":
            _fail("skill_readonly_invalid_dependency")
        if ref["source_kind"] not in ("builtin", "team", "user") or type(ref["id"]) is not str or not _ID.fullmatch(ref["id"]):
            _fail("skill_readonly_invalid_dependency")
        if type(ref["version"]) is not str or not _HASH.fullmatch(ref["version"]):
            _fail("skill_readonly_invalid_dependency")
        identity = (ref["source_kind"], ref["id"])
        if identity in identities:
            _fail("skill_readonly_invalid_dependency")
        identities.add(identity)
    data["parameter_schema"] = _schema(data["parameter_schema"])
    return data


def parse_read_only_entry(raw: object) -> ReadOnlyEntry:
    return ReadOnlyEntry(raw)


def skill_reference_read_entry(frontmatter: dict) -> ReadOnlyEntry:
    """Read ordinary SKILL.md text, or honor an explicitly declared entry.

    The default projects only the manifest body with no parameters or captured
    dependencies. It is not an assertion about the package's executable effects
    and never grants invocation, Tools, connections, or receiver access.
    """
    if "read_only_entry" in frontmatter:
        return ReadOnlyEntry(frontmatter["read_only_entry"])
    return ReadOnlyEntry(
        {
            "schema_version": 1,
            "entry_file": "SKILL.md",
            "required_files": [],
            "parameter_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "required_skill_refs": [],
            "requires_script": False,
            "requires_new_tools": False,
            "requires_new_connections": False,
        }
    )


def validate_read_only_entry_files(entry: ReadOnlyEntry, skill_dir: Path, *, allow_missing: bool = False) -> None:
    """Validate the current tree, never an execution handle.

    Authoring alone allows missing files so the existing import inventory can mark
    the saved package pending. Admission must use the strict default.
    """
    total = 0
    try:
        root = skill_dir.resolve(strict=True)
        for relative in (entry.entry_file, *entry.required_files):
            path = root
            for part in PurePosixPath(relative).parts:
                path /= part
                if path.is_symlink():
                    _fail("skill_readonly_invalid_file")
            try:
                metadata = path.stat()
            except FileNotFoundError:
                if allow_missing:
                    continue
                _fail("skill_readonly_invalid_file")
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
                _fail("skill_readonly_invalid_file")
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            total += len(content)
            if len(content) > MAX_FILE_BYTES or total > MAX_ENTRY_BYTES or b"\x00" in content:
                _fail("skill_readonly_invalid_file")
            text = content.decode("utf-8")
            if relative == entry.entry_file and not text.strip():
                _fail("skill_readonly_invalid_file")
    except (OSError, UnicodeError):
        _fail("skill_readonly_invalid_file")
