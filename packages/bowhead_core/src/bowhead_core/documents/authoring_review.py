"""Observed source/candidate differences, never a semantic approval receipt."""

from __future__ import annotations

from bowhead_core.documents.authoring_protocol import PROFILE, digest
from bowhead_core.documents.executor import WorkerResult
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.store import _json


def _objects(page):
    result = {}

    def visit(items, path=()):
        for item in items:
            shape_id = item.get("shape_id")
            if type(shape_id) is not int or shape_id <= 0:
                raise ValueError("Object identity is unavailable")
            address = (*path, shape_id)
            if address in result:
                raise ValueError("Object identity is ambiguous")
            result[address] = {key: value for key, value in item.items() if key != "children"}
            visit(item.get("children", []), address)

    visit(page.get("objects", []))
    return result


def _pages(pages):
    result = {}
    for page in pages:
        part = page.get("part_name")
        if not isinstance(part, str) or not part or part in result:
            raise ValueError("Page identity is unavailable or ambiguous")
        result[part] = _objects(page)
    return result


def _text(item):
    if item is None:
        return ""
    text = "\n".join(paragraph["text"] for paragraph in item.get("paragraphs", []))
    if item.get("kind") == "table":
        text = "\n".join(" | ".join(paragraph["text"] for paragraph in cell["paragraphs"]) for cell in item["cells"])
    if item.get("kind") == "chart":
        text = _json({key: item[key] for key in ("title", "categories", "series", "text_fonts") if key in item})
    return text or _json(item)


def review_result(result: WorkerResult, *, source: dict, candidate: dict, execution_run_id: str, code_sha256: str) -> WorkerResult:
    if (
        source.get("detail") != "objects"
        or candidate.get("detail") != "objects"
        or candidate.get("source_sha256") != digest(result.files["candidate.pptx"])
        or result.record.get("parent_sha256") != source.get("source_sha256")
        or len(source["slides"]) != source["slide_count"]
        or len(candidate["slides"]) != candidate["slide_count"]
    ):
        raise OfficePackageError("document_authoring_output_invalid")
    changes = []
    comparison = "observed_object_facts"
    try:
        before, after = _pages(source["slides"]), _pages(candidate["slides"])
        if not after.keys() <= before.keys():
            raise ValueError("Candidate pages have no retained source identity")
        for page in candidate["slides"]:
            old, new = before[page["part_name"]], after[page["part_name"]]
            for key in sorted(old.keys() | new.keys()):
                if old.get(key) != new.get(key):
                    changes.append(
                        {
                            "slide": page["slide"],
                            "shape_name": (new.get(key) or old[key]).get("name", ""),
                            "before": _text(old.get(key)),
                            "after": _text(new.get(key)),
                            "evidence": [],
                        }
                    )
    except ValueError:
        comparison = "unavailable"
        changes = []
    manifest = {
        "schema_version": 2,
        "comparison": comparison,
        "changes": changes,
        "retained_unsupported_text": [],
        "source_sha256": source["source_sha256"],
        "candidate_sha256": candidate["source_sha256"],
        "source_page_count": source["slide_count"],
        "candidate_page_count": candidate["slide_count"],
        "limitations": list(
            dict.fromkeys(
                [
                    *source.get("limitations", []),
                    *candidate.get("limitations", []),
                    "Object comparison does not prove semantic relevance, page correspondence after composition or exhaustive package/style equality.",
                ]
            )
        ),
    }
    return WorkerResult(
        {
            **result.record,
            "profile": PROFILE,
            "kind": "authoring_draft",
            "manifest": manifest,
            "requires_full_review": True,
            "execution_run_id": execution_run_id,
            "code_sha256": code_sha256,
        },
        result.files,
        result.image_id,
    )
