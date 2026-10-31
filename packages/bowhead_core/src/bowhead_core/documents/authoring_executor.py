"""Private byte-only client for the original durable document-run service.

No Office parsing or model-code execution occurs in Gateway. The original
private document service owns admission, selected-model calls and publication;
this client transports qualified worker bytes under its versioned context.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import anyio
import httpx2

from bowhead_core.documents.authoring_protocol import CONTRACT, PROFILE, control, digest, identity, read_file
from bowhead_core.documents.executor import DocumentExecutor, WorkerInput, WorkerResult
from bowhead_core.documents.package_io import OfficePackageError


@dataclass(frozen=True)
class ScriptCandidate:
    content: bytes
    run_id: str
    parent_sha256: str
    code_sha256: str
    image_id: str


class ScriptExecutionError(OfficePackageError):
    """A settled code failure; diagnostics are data for the original owner only."""

    def __init__(self, run_id: str, diagnostics: str) -> None:
        super().__init__("document_authoring_script_failed")
        self.run_id = run_id
        self.diagnostics = diagnostics[:8192]


def _raise_fixed_error(record, *, control_sha256, source_sha256):
    if isinstance(record, dict) and record.get("kind") == "authoring_error":
        code = record.get("error_code")
        if (
            record.get("profile") != PROFILE
            or record.get("control_sha256") != control_sha256
            or record.get("source_sha256") != source_sha256
            or not isinstance(code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{1,100}", code) is None
        ):
            raise OfficePackageError("document_authoring_output_invalid")
        raise OfficePackageError(code)


def read_preview(directory: Path, *, image_id: str, control_sha256: str, candidate: ScriptCandidate) -> WorkerResult:
    try:
        record = json.loads(read_file(directory / "result.json", 2 * 1024 * 1024))
        _raise_fixed_error(record, control_sha256=control_sha256, source_sha256=digest(candidate.content))
        if (
            record["profile"] != PROFILE
            or record["kind"] != "authoring_preview"
            or record["control_sha256"] != control_sha256
            or record["parent_sha256"] != candidate.parent_sha256
            or record["semantic_status"] != "unapproved"
        ):
            raise OfficePackageError("document_authoring_output_invalid")
        preview = record["preview"]
        source = read_file(directory / "candidate.pptx")
        pdf = read_file(directory / "candidate.pdf")
        if source != candidate.content or digest(source) != preview["source_sha256"]:
            raise OfficePackageError("source_hash_mismatch")
        if not pdf.startswith(b"%PDF-") or digest(pdf) != preview["pdf_sha256"] or preview["text_and_geometry"] != "passed":
            raise OfficePackageError("document_pdf_invalid")
        if type(preview["page_count"]) is not int or not 1 <= preview["page_count"] <= 64 or len(preview["pages"]) != preview["page_count"]:
            raise OfficePackageError("document_authoring_output_invalid")
        files = {"candidate.pptx": source, "candidate.pdf": pdf}
        total = 0
        for page in preview["pages"]:
            name = page["name"]
            if not isinstance(name, str) or re.fullmatch(r"page-[0-9]{1,3}\.png", name) is None or name in files:
                raise OfficePackageError("document_authoring_output_invalid")
            content = read_file(directory / name, 16 * 1024 * 1024)
            total += len(content)
            if total > 128 * 1024 * 1024 or not content.startswith(b"\x89PNG\r\n\x1a\n") or digest(content) != page["sha256"]:
                raise OfficePackageError("document_authoring_output_invalid")
            files[name] = content
        return WorkerResult(record, files, image_id)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise OfficePackageError("document_authoring_output_invalid") from error


class AuthoringExecutor(DocumentExecutor):
    async def read_material(self, *, run_id: str, item: WorkerInput, expected_image_id: str) -> dict:
        query = json.dumps({"filename": item.name}, sort_keys=True).encode()
        encoded = control("material", (item.content, query), parent_sha256=digest(item.content))
        directory, _ = await self._dispatch(
            run_id, (item, WorkerInput("query.json", "query", query)), encoded, expected_image_id=expected_image_id, profile=PROFILE
        )
        record = json.loads(await anyio.to_thread.run_sync(lambda: read_file(directory / "output" / "result.json", 2 * 1024 * 1024)))
        _raise_fixed_error(record, control_sha256=digest(encoded), source_sha256=digest(item.content))
        if (
            not isinstance(record, dict)
            or record.get("profile") != PROFILE
            or record.get("kind") != "authoring_material"
            or record.get("control_sha256") != digest(encoded)
            or record.get("source_sha256") != digest(item.content)
            or not isinstance(record.get("content_json"), str)
        ):
            raise OfficePackageError("document_authoring_output_invalid")
        return {"source_sha256": digest(item.content), "filename": item.name, "content_json": record["content_json"]}

    async def inspect_source(
        self, *, run_id: str, source: bytes, expected_image_id: str, slides: list[int] | None = None, detail: str = "index"
    ) -> dict:
        if not isinstance(expected_image_id, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", expected_image_id) is None:
            raise OfficePackageError("document_runtime_identity_required")
        default_first_page = slides is None and detail in {"content", "objects"}
        query = json.dumps({"slides": [1] if default_first_page else slides, "detail": detail}, sort_keys=True).encode()
        encoded = control("inspect", (source, query), parent_sha256=digest(source))
        directory, _ = await self._dispatch(
            run_id,
            (WorkerInput("source.pptx", "source", source), WorkerInput("query.json", "query", query)),
            encoded,
            expected_image_id=expected_image_id,
            profile=PROFILE,
        )
        record = json.loads(await anyio.to_thread.run_sync(lambda: read_file(directory / "output" / "result.json", 2 * 1024 * 1024)))
        _raise_fixed_error(record, control_sha256=digest(encoded), source_sha256=digest(source))
        if (
            not isinstance(record, dict)
            or record.get("profile") != PROFILE
            or record.get("kind") != "authoring_inspection"
            or record.get("control_sha256") != digest(encoded)
            or record.get("source_sha256") != digest(source)
            or not isinstance(record.get("inspection"), dict)
            or record["inspection"].get("source_sha256") != digest(source)
        ):
            raise OfficePackageError("document_authoring_output_invalid")
        facts = record["inspection"]
        if default_first_page:
            facts = {
                **facts,
                "selection": {
                    "mode": "default_first_page",
                    "remaining_pages": facts["slide_count"] - 1,
                    "next_slide": 2 if facts["slide_count"] > 1 else None,
                },
            }
        return facts

    async def capabilities(self) -> dict:
        async with httpx2.AsyncClient(headers={"X-Executor-Token": self.token}, timeout=20, follow_redirects=False, trust_env=False) as client:
            reply = await client.get(self.url + "/document-runs/capabilities/" + PROFILE)
            reply.raise_for_status()
            record = reply.json()
        if (
            not isinstance(record, dict)
            or record.get("profile") != PROFILE
            or record.get("contract") != CONTRACT
            or not isinstance(record.get("image_id"), str)
            or re.fullmatch(r"sha256:[a-f0-9]{64}", record["image_id"]) is None
        ):
            raise OfficePackageError("document_runtime_unavailable")
        return record

    async def execute_script(
        self, *, run_id: str, source: bytes, code: str, parent_sha256: str, expected_image_id: str, data: tuple[WorkerInput, ...] = ()
    ) -> ScriptCandidate:
        if not isinstance(expected_image_id, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", expected_image_id) is None:
            raise OfficePackageError("document_runtime_identity_required")
        script = code.encode("utf-8")
        encoded = control("execute", (source, script, *(item.content for item in data)), parent_sha256=parent_sha256)
        directory, execution = await self._dispatch(
            run_id,
            (WorkerInput("source.pptx", "source", source), WorkerInput("script.py", "code", script), *data),
            encoded,
            expected_image_id=expected_image_id,
            profile=PROFILE,
            return_failed=True,
        )
        if execution["status"] == "failed":
            diagnostic = execution.get("diagnostics")
            raise ScriptExecutionError(
                run_id, diagnostic if isinstance(diagnostic, str) else "The isolated script failed without retained diagnostics."
            )
        try:
            content = await anyio.to_thread.run_sync(lambda: read_file(directory / "output" / "candidate.pptx"))
        except OfficePackageError as error:
            raise ScriptExecutionError(
                run_id,
                "The completed script did not save a bounded regular candidate.pptx in OUTPUT_DIR. "
                "Use document_read for read-only inspection. Complete the requested edits before saving the final candidate; "
                "do not save an unchanged source merely to finish inspection.",
            ) from error
        # Model-written result.json/stdout is deliberately ignored. Only a
        # separate code-free render run can produce a trusted preview receipt.
        return ScriptCandidate(content, run_id, parent_sha256, digest(script), execution["image_id"])

    async def render_script_candidate(self, *, run_id: str, candidate: ScriptCandidate) -> WorkerResult:
        identity(candidate.run_id)
        identity(candidate.code_sha256)
        encoded = control("render", (candidate.content,), parent_sha256=candidate.parent_sha256)
        directory, execution = await self._dispatch(
            run_id, (WorkerInput("candidate.pptx", "candidate", candidate.content),), encoded, expected_image_id=candidate.image_id, profile=PROFILE
        )
        return await anyio.to_thread.run_sync(
            lambda: read_preview(directory / "output", image_id=execution["image_id"], control_sha256=digest(encoded), candidate=candidate)
        )
