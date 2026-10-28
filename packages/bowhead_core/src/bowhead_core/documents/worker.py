"""Fixed document-worker operations. No model calls, credentials or user code."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from bowhead_core.documents.evidence import prepare_evidence
from bowhead_core.documents.package_io import MAX_OFFICE_BYTES, OFFICE_MIMES, OfficePackageError, inspect_office_package
from bowhead_core.documents.pptx_draft import compile_pptx_draft
from bowhead_core.documents.pptx_plan import PROFILE as PLANNER_PROFILE
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, planning_system_prompt, prepare_planning_context, review_planning_response
from bowhead_core.documents.pptx_text import inventory_pptx_text
from bowhead_core.documents.render import render_candidate

WORKER_PROFILE = "ordinary-pptx-worker-v1"


def _read(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise OfficePackageError("document_worker_input_invalid")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise OfficePackageError("document_worker_input_invalid")
    return content


def execute(input_dir: Path, output_dir: Path, control_sha256: str) -> dict[str, object]:
    raw = _read(input_dir / "control.json", 256_000)
    if hashlib.sha256(raw).hexdigest() != control_sha256:
        raise OfficePackageError("document_worker_identity_mismatch")
    control = json.loads(raw)
    required = {"profile", "operation", "instruction", "inputs", "response", "context_digest"}
    if not isinstance(control, dict) or set(control) not in (required, required | {"planner_profile"}):
        raise OfficePackageError("document_worker_control_invalid")
    if control["profile"] != WORKER_PROFILE or control["operation"] not in {"inventory", "apply_verify"}:
        raise OfficePackageError("document_worker_control_invalid")
    planner = control.get("planner_profile", PLANNER_PROFILE)
    planning_system_prompt(planner)
    inputs = control["inputs"]
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 8:
        raise OfficePackageError("document_worker_control_invalid")
    source, data = None, []
    for index, item in enumerate(inputs):
        if not isinstance(item, dict) or set(item) != {"name", "role", "sha256"} or item["role"] not in {"template", "data", "reference"}:
            raise OfficePackageError("document_worker_control_invalid")
        if not isinstance(item["sha256"], str) or re.fullmatch("[a-f0-9]{64}", item["sha256"]) is None:
            raise OfficePackageError("document_worker_control_invalid")
        content = _read(input_dir / f"input-{index}", MAX_OFFICE_BYTES)
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise OfficePackageError("document_frozen_hash_mismatch")
        if item["role"] == "template":
            if source is not None:
                raise OfficePackageError("document_worker_control_invalid")
            source = content
        else:
            data.append(prepare_evidence(item["name"], content))
    if source is None or not isinstance(control["instruction"], str):
        raise OfficePackageError("document_worker_control_invalid")
    if not data:
        data.append(JsonEvidenceSource("User request", json.dumps({"request": control["instruction"]}, ensure_ascii=False).encode()))
    inventory = inventory_pptx_text(source)
    package = inspect_office_package(source, filename="source.pptx", declared_mime=OFFICE_MIMES[".pptx"])
    if inventory.unsupported_objects or package.external_relationship_count or package.opaque_embedding_count:
        raise OfficePackageError("draft_template_unsupported")
    if not 1 <= inventory.slide_count <= 64:
        raise OfficePackageError("document_slide_limit")
    context = prepare_planning_context(source, control["instruction"], tuple(data), profile=planner)
    if control["operation"] == "inventory":
        if control["response"] is not None or control["context_digest"] is not None:
            raise OfficePackageError("document_worker_control_invalid")
        return {"profile": WORKER_PROFILE, "kind": "context", "context": asdict(context)}
    if not isinstance(control["response"], str) or context.digest != control["context_digest"]:
        raise OfficePackageError("planning_context_changed")
    review = review_planning_response(
        source, control["instruction"], tuple(data), control["response"], expected_context_digest=context.digest, profile=planner
    )
    if review.action == "ask":
        return {"profile": WORKER_PROFILE, "kind": "question", "review": asdict(review)}
    draft = compile_pptx_draft(
        source, control["instruction"], tuple(data), control["response"], expected_context_digest=context.digest, profile=planner
    )
    candidate = output_dir / "candidate.pptx"
    with candidate.open("xb") as stream:
        stream.write(draft.candidate)
    preview = render_candidate(candidate, output_dir)
    return {
        "profile": WORKER_PROFILE,
        "kind": "draft",
        "manifest": asdict(draft.manifest),
        "manifest_sha256": draft.manifest.digest,
        "preview": preview,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--control-sha256", required=True)
    args = parser.parse_args()
    try:
        result = execute(Path("/input"), Path("/output"), args.control_sha256)
    except OfficePackageError as exc:
        result = {"profile": WORKER_PROFILE, "kind": "error", "error_code": exc.code}
    except Exception:  # noqa: BLE001 — content-free boundary; partial outputs remain unpublished
        result = {"profile": WORKER_PROFILE, "kind": "error", "error_code": "document_worker_failed"}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 2 * 1024 * 1024:
        encoded = json.dumps({"profile": WORKER_PROFILE, "kind": "error", "error_code": "document_worker_output_limit"}).encode()
    with Path("/output/result.json").open("xb") as stream:
        stream.write(encoded)
    return 0
