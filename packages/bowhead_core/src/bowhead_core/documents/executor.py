"""Client for fixed document operations; never parses Office in the Gateway.

The server-owned run ID and exact control hash are the retry boundary. A lost
HTTP response can reconcile the same run, never dispatch another container.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import anyio
import httpx2

from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_plan import PROFILE as PLANNER_PROFILE
from bowhead_core.documents.pptx_plan import planning_system_prompt

PROFILE = "ordinary-pptx-worker-v1"
MAX_RESULT = 2 * 1024 * 1024


@dataclass(frozen=True)
class WorkerInput:
    name: str
    role: str
    content: bytes


@dataclass(frozen=True)
class WorkerResult:
    record: dict
    files: dict[str, bytes]
    image_id: str


def _read_output(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > limit:
        raise OfficePackageError("document_worker_output_invalid")
    with path.open("rb") as stream:
        result = stream.read(limit + 1)
    if len(result) > limit:
        raise OfficePackageError("document_worker_output_invalid")
    return result


def read_result(
    directory: Path, *, image_id: str, operation: str, context_digest: str | None, planner_profile: str = PLANNER_PROFILE
) -> WorkerResult:
    """Read only declared regular files and verify exact byte identities."""
    record = json.loads(_read_output(directory / "result.json", MAX_RESULT))
    if not isinstance(record, dict) or record.get("profile") != PROFILE:
        raise OfficePackageError("document_worker_output_invalid")
    kind = record.get("kind")
    files = {}
    if kind == "context" and operation == "inventory":
        context = record["context"]
        payload = context["payload"]
        if not isinstance(payload, str) or len(payload.encode()) > 64_000:
            raise OfficePackageError("document_worker_output_invalid")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise OfficePackageError("document_worker_output_invalid")
        if (
            decoded.get("profile") != planner_profile
            or hashlib.sha256((planning_system_prompt(planner_profile) + "\n" + payload).encode()).hexdigest() != context["digest"]
        ):
            raise OfficePackageError("planning_context_changed")
    elif kind == "draft" and operation == "apply_verify":
        manifest, preview = record["manifest"], record["preview"]
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if hashlib.sha256(canonical).hexdigest() != record["manifest_sha256"] or manifest["context_digest"] != context_digest:
            raise OfficePackageError("document_worker_output_invalid")
        if manifest["semantic_status"] != "unapproved" or preview["text_and_geometry"] != "passed":
            raise OfficePackageError("document_worker_output_invalid")
        source = _read_output(directory / "candidate.pptx", 32 * 1024 * 1024)
        pdf = _read_output(directory / "candidate.pdf", 32 * 1024 * 1024)
        source_sha = hashlib.sha256(source).hexdigest()
        if source_sha != manifest["edit_receipt"]["candidate_sha256"] or source_sha != preview["source_sha256"]:
            raise OfficePackageError("source_hash_mismatch")
        if not pdf.startswith(b"%PDF-") or hashlib.sha256(pdf).hexdigest() != preview["pdf_sha256"]:
            raise OfficePackageError("document_pdf_invalid")
        files = {"candidate.pptx": source, "candidate.pdf": pdf}
        if not 1 <= preview["page_count"] <= 64 or len(preview["pages"]) != preview["page_count"]:
            raise OfficePackageError("document_worker_output_invalid")
        total = 0
        for page in preview["pages"]:
            name = page["name"]
            if not isinstance(name, str) or re.fullmatch(r"page-[0-9]{1,3}\.png", name) is None or name in files:
                raise OfficePackageError("document_worker_output_invalid")
            content = _read_output(directory / name, 16 * 1024 * 1024)
            total += len(content)
            if total > 128 * 1024 * 1024 or not content.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(content).hexdigest() != page["sha256"]:
                raise OfficePackageError("document_worker_output_invalid")
            files[name] = content
    elif kind not in {"error", "question"} or (kind == "question" and operation != "apply_verify"):
        raise OfficePackageError("document_worker_output_invalid")
    return WorkerResult(record, files, image_id)


class DocumentExecutor:
    def __init__(self, *, root: Path, url: str, token: str) -> None:
        self.root, self.url, self.token = root.resolve(strict=True), url.rstrip("/"), token
        if not token or not url:
            raise OfficePackageError("document_runtime_unavailable")

    async def run(
        self,
        *,
        run_id: str,
        inputs: tuple[WorkerInput, ...],
        instruction: str,
        operation: str,
        response: str | None = None,
        context_digest: str | None = None,
        expected_image_id: str | None = None,
        planner_profile: str = PLANNER_PROFILE,
    ) -> WorkerResult:
        if re.fullmatch(r"[a-f0-9]{64}", run_id) is None or not 1 <= len(inputs) <= 8 or operation not in {"inventory", "apply_verify"}:
            raise OfficePackageError("document_worker_control_invalid")
        planning_system_prompt(planner_profile)
        control = {
            "profile": PROFILE,
            "operation": operation,
            "instruction": instruction,
            "inputs": [{"name": item.name, "role": item.role, "sha256": hashlib.sha256(item.content).hexdigest()} for item in inputs],
            "response": response,
            "context_digest": context_digest,
        }
        if planner_profile != PLANNER_PROFILE:
            control["planner_profile"] = planner_profile
        encoded = json.dumps(control, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 256_000 or sum(len(item.content) for item in inputs) > 128 * 1024 * 1024:
            raise OfficePackageError("document_inputs_too_large")
        directory = self.root / ("document-" + run_id)
        directory.mkdir(mode=0o700, exist_ok=True)
        for path in (directory, directory / "input", directory / "output"):
            if path.is_symlink():
                raise OfficePackageError("document_directory_invalid")
            path.mkdir(mode=0o700, exist_ok=True)
        # Create-only files. The durable runner has a single lease owner; a
        # crash-partial staging operation fails rather than replacing input.
        for name, content in [(f"input-{index}", item.content) for index, item in enumerate(inputs)] + [("control.json", encoded)]:
            if len(content) > 32 * 1024 * 1024:
                raise OfficePackageError("document_inputs_too_large")
            path = directory / "input" / name
            try:
                with path.open("xb") as stream:
                    stream.write(content)
            except FileExistsError:
                if _read_output(path, 32 * 1024 * 1024) != content:
                    raise OfficePackageError("document_worker_identity_mismatch") from None
        payload = {"run_id": run_id, "control_sha256": hashlib.sha256(encoded).hexdigest(), "expected_image_id": expected_image_id}
        async with httpx2.AsyncClient(headers={"X-Executor-Token": self.token}, timeout=20, follow_redirects=False, trust_env=False) as client:
            try:
                reply = await client.post(self.url + "/document-runs", json=payload)
            except httpx2.TransportError:
                # Reconcile only the exact durable run. Never repeat POST or
                # switch identities when the dispatch response is unknown.
                reply = await client.get(self.url + "/document-runs/" + run_id)
            reply.raise_for_status()
            record = reply.json()
            with anyio.fail_after(180):
                while record.get("status") in {"starting", "running", "unknown"}:
                    await anyio.sleep(1)
                    reply = await client.get(self.url + "/document-runs/" + run_id)
                    reply.raise_for_status()
                    record = reply.json()
            if record.get("run_id") != run_id or record.get("control_sha256") != payload["control_sha256"] or record.get("status") != "completed":
                raise OfficePackageError("document_worker_failed")
            if expected_image_id is not None and record.get("image_id") != expected_image_id:
                raise OfficePackageError("document_runtime_changed")
        try:
            return await anyio.to_thread.run_sync(
                lambda: read_result(
                    directory / "output",
                    image_id=record["image_id"],
                    operation=operation,
                    context_digest=context_digest,
                    planner_profile=planner_profile,
                )
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            if isinstance(exc, OfficePackageError):
                raise
            raise OfficePackageError("document_worker_output_invalid") from exc
