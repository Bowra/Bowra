"""Page selection and source-bound receipts for the existing PPTX readers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from bowhead_core.read_receipts import ReadLimit

MAX_ATTACHMENT_PPTX_PAGES = 30


class AttachmentReadQueryError(ValueError):
    """A supported reader received an invalid selection, not invalid source bytes."""

    def __init__(self, message: str, *, limit: ReadLimit | None = None) -> None:
        super().__init__(message)
        self.limit: ReadLimit | None = limit


def validate_pptx_slides(slides: object, *, total: int | None = None, maximum: int | None = None) -> None:
    if not isinstance(slides, list) or not slides or any(type(page) is not int or page < 1 for page in slides):
        raise AttachmentReadQueryError("slides must contain unique positive one-based integer page numbers.")
    if len(set(slides)) != len(slides):
        raise AttachmentReadQueryError("slides must not contain duplicate page numbers.")
    if maximum is not None and len(slides) > maximum:
        from bowhead_core.read_receipts import ReadLimit

        raise AttachmentReadQueryError(
            f"This read accepts at most {maximum} selected pages; {len(slides)} were requested.",
            limit=ReadLimit(scope="model_preview", kind="selected_pages", actual=len(slides), maximum=maximum, unit="pages"),
        )
    if total is not None and any(page > total for page in slides):
        raise AttachmentReadQueryError(f"Selected page numbers must exist in this {total}-page presentation.")


def validate_attachment_read_arguments(arguments: object) -> None:
    """Validate without changing the exact arguments retained in native receipts."""
    if not isinstance(arguments, dict) or set(arguments) - {"artifact_id", "slides", "detail", "expected_content_hash"}:
        raise AttachmentReadQueryError("Use artifact_id and only the supported PPTX slides, detail and expected_content_hash arguments.")
    if not isinstance(arguments.get("artifact_id"), str) or not arguments["artifact_id"].strip():
        raise AttachmentReadQueryError("artifact_id must identify a confirmed attachment in the current task.")
    if "slides" in arguments:
        validate_pptx_slides(arguments["slides"], maximum=MAX_ATTACHMENT_PPTX_PAGES)
    if "detail" in arguments and arguments["detail"] not in ("index", "content"):
        raise AttachmentReadQueryError("detail must be index or content.")
    if "expected_content_hash" in arguments and (
        not isinstance(arguments["expected_content_hash"], str) or re.fullmatch(r"sha256:[a-f0-9]{64}", arguments["expected_content_hash"]) is None
    ):
        raise AttachmentReadQueryError("expected_content_hash must be the exact sha256 hash returned by the previous read.")


def validate_attachment_read_result(arguments: dict, result: object) -> None:
    """Keep legacy reads valid while binding selected reads to their saved coverage."""
    validate_attachment_read_arguments(arguments)
    if (
        not isinstance(result, dict)
        or result.get("ok") is not True
        or result.get("artifact_id") != arguments["artifact_id"]
        or not isinstance(result.get("content_hash"), str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", result["content_hash"]) is None
        or arguments.get("expected_content_hash", result["content_hash"]) != result["content_hash"]
    ):
        raise AttachmentReadQueryError("The retained attachment read does not match its source.")
    if set(arguments) == {"artifact_id"}:
        return
    from bowhead_core.read_receipts import PptxReadCoverage, ReadInfo

    info = ReadInfo.model_validate(result.get("read_info"))
    coverage = info.coverage
    if (
        not isinstance(coverage, PptxReadCoverage)
        or coverage.detail != arguments.get("detail", "content")
        or coverage.requested_pages != arguments.get("slides")
        or coverage.content_kind != ("index" if coverage.detail == "index" else "text_and_tables")
    ):
        raise AttachmentReadQueryError("The retained attachment read does not match its page selection.")


def pptx_read_info(
    *,
    total_pages: int,
    requested_pages: list[int] | None,
    returned_pages: list[int],
    complete_pages: list[int],
    detail: Literal["index", "content", "objects"],
    content_kind: Literal["index", "text_and_tables", "document_content"],
    limit: ReadLimit | None = None,
    full_input_available: bool | None = None,
) -> dict:
    from bowhead_core.read_receipts import PptxReadCoverage, ReadInfo, ReadRecovery

    total = total_pages if requested_pages is None else len(requested_pages)
    return ReadInfo(
        view="complete" if len(complete_pages) == total else "partial" if returned_pages else "none",
        coverage=PptxReadCoverage(
            scope="file" if requested_pages is None else "selection",
            unit="pages",
            returned=len(returned_pages),
            total=total,
            content_kind=content_kind,
            detail=detail,
            requested_pages=requested_pages,
            returned_pages=returned_pages,
            complete_pages=complete_pages,
            total_pages=total_pages,
        ),
        limit=limit,
        full_input_available=full_input_available,
        recovery=ReadRecovery(action="narrow_selection" if limit is not None else "none"),
    ).model_dump(mode="json")


def bind_pptx_continuation(info: dict, *, tool: Literal["attachment_read", "document_read"], arguments: dict, maximum: int) -> dict:
    """Describe one available call; this neither dispatches it nor grants its source."""
    from bowhead_core.read_receipts import PptxReadCoverage, ReadContinuation, ReadInfo, ReadNext, ReadRecovery

    receipt = ReadInfo.model_validate(info)
    coverage = receipt.coverage
    if not isinstance(coverage, PptxReadCoverage):
        raise ValueError("PPTX continuation requires page coverage")
    selected = coverage.requested_pages if coverage.requested_pages is not None else list(range(1, coverage.total_pages + 1))
    unfinished = [page for page in selected if page not in coverage.complete_pages]
    cut = [page for page in coverage.returned_pages if page not in coverage.complete_pages]
    next_pages = None
    scope = coverage.scope
    reason = "selection_complete"
    if cut:
        if len(selected) == 1 or not coverage.complete_pages:
            reason = "page_content_limit"
        else:
            next_pages = [cut[0]]
    elif unfinished:
        next_pages = unfinished[:maximum]
    elif selected and selected == list(range(selected[0], selected[-1] + 1)) and selected[-1] < coverage.total_pages:
        scope, reason = "file", "later_pages_available"
        next_pages = list(range(selected[-1] + 1, min(coverage.total_pages, selected[-1] + maximum) + 1))
    continuation = ReadContinuation(
        scope=scope,
        available=next_pages is not None,
        next=ReadNext(tool=tool, arguments={**arguments, "detail": coverage.detail, "slides": next_pages}) if next_pages is not None else None,
        reason=reason if next_pages is None or reason == "later_pages_available" else None,
    )
    updates = {"continuation": continuation}
    if reason == "page_content_limit":
        updates["recovery"] = ReadRecovery(action="use_full_input" if receipt.full_input_available else "none")
    return receipt.model_copy(update=updates).model_dump(mode="json")


def inspect_pptx_read_info(facts: dict, *, source: str, slides: list[int] | None, detail: Literal["index", "content", "objects"]) -> dict:
    """Bind verified native inspection pages without narrowing the original query."""
    from bowhead_core.documents.package_io import OfficePackageError

    count = facts.get("slide_count")
    if type(count) is not int or not 1 <= count <= 64 or not isinstance(facts.get("slides"), list) or facts.get("detail") != detail:
        raise OfficePackageError("document_authoring_output_invalid")
    expected = slides if slides is not None else list(range(1, count + 1)) if detail == "index" else [1]
    returned = [page.get("slide") if isinstance(page, dict) else None for page in facts["slides"]]
    if returned != expected:
        raise OfficePackageError("document_authoring_output_invalid")
    info = pptx_read_info(
        total_pages=count,
        requested_pages=slides,
        returned_pages=returned,
        complete_pages=returned,
        detail=detail,
        content_kind="index" if detail == "index" else "document_content",
        full_input_available=True,
    )
    return bind_pptx_continuation(info, tool="document_read", arguments={"source": source}, maximum=max(1, len(returned)))
