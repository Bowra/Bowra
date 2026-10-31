"""Versioned byte contract for isolated script and independent render runs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from bowhead_core.documents.package_io import OfficePackageError

PROFILE = "pptx-script-worker-v1"
CONTRACT = "private-draft-authoring-v1"
MAX_CODE = 64 * 1024
MAX_FILE = 32 * 1024 * 1024


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def identity(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise OfficePackageError("document_authoring_identity_invalid")
    return value


def read_file(path: Path, limit: int = MAX_FILE) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > limit:
        raise OfficePackageError("document_authoring_file_invalid")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise OfficePackageError("document_authoring_file_invalid")
    return content


def control(operation: str, inputs: tuple[bytes, ...], *, parent_sha256: str) -> bytes:
    if operation not in {"inspect", "material", "execute", "render"} or not (
        (operation == "render" and len(inputs) == 1)
        or (operation in {"inspect", "material"} and len(inputs) == 2)
        or (operation == "execute" and 2 <= len(inputs) <= 9)
    ):
        raise OfficePackageError("document_authoring_control_invalid")
    identity(parent_sha256)
    if any(len(content) > MAX_FILE for content in inputs):
        raise OfficePackageError("document_inputs_too_large")
    if operation in {"inspect", "material", "execute"}:
        if digest(inputs[0]) != parent_sha256 or not 0 < len(inputs[1]) <= MAX_CODE:
            raise OfficePackageError("document_authoring_control_invalid")
        try:
            inputs[1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise OfficePackageError("document_authoring_code_invalid") from error
        if operation == "inspect":
            try:
                query = json.loads(inputs[1])
                if (
                    not isinstance(query, dict)
                    or set(query) != {"slides", "detail"}
                    or query["detail"] not in ("index", "content", "objects")
                    or (
                        query["slides"] is not None
                        and (not isinstance(query["slides"], list) or any(type(page) is not int or page < 1 for page in query["slides"]))
                    )
                ):
                    raise OfficePackageError("document_authoring_query_invalid")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise OfficePackageError("document_authoring_query_invalid") from error
        if operation == "material":
            try:
                query = json.loads(inputs[1])
                if not isinstance(query, dict) or set(query) != {"filename"} or not isinstance(query["filename"], str):
                    raise OfficePackageError("document_authoring_query_invalid")
            except json.JSONDecodeError as error:
                raise OfficePackageError("document_authoring_query_invalid") from error
    return json.dumps(
        {"profile": PROFILE, "operation": operation, "parent_sha256": parent_sha256, "input_sha256": [digest(content) for content in inputs]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def read_control(directory: Path, expected_sha256: str) -> tuple[dict, tuple[bytes, ...]]:
    raw = read_file(directory / "control.json", 256_000)
    if digest(raw) != identity(expected_sha256):
        raise OfficePackageError("document_authoring_control_changed")
    try:
        record = json.loads(raw)
        if set(record) != {"profile", "operation", "parent_sha256", "input_sha256"} or record["profile"] != PROFILE:
            raise OfficePackageError("document_authoring_control_invalid")
        if not isinstance(record["input_sha256"], list) or not 1 <= len(record["input_sha256"]) <= 9:
            raise OfficePackageError("document_authoring_control_invalid")
        expected_names = {"control.json", *(f"input-{index}" for index in range(len(record["input_sha256"])))}
        if {path.name for path in directory.iterdir()} != expected_names:
            raise OfficePackageError("document_authoring_input_changed")
        inputs = tuple(read_file(directory / f"input-{index}") for index in range(len(record["input_sha256"])))
        if [digest(content) for content in inputs] != record["input_sha256"]:
            raise OfficePackageError("document_authoring_input_changed")
        if control(record["operation"], inputs, parent_sha256=record["parent_sha256"]) != raw:
            raise OfficePackageError("document_authoring_control_invalid")
        return record, inputs
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise OfficePackageError("document_authoring_control_invalid") from error
