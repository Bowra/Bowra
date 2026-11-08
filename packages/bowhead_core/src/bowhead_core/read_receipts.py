"""Content-free facts for bounded reads; availability is supplied by the owner."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReadLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scope: Literal["parser", "model_preview", "transport", "model_context"]
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    actual: int | None = Field(default=None, ge=0)
    maximum: int | None = Field(default=None, ge=0)
    unit: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")


class ReadCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scope: Literal["file", "selection"] = "file"
    unit: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    returned: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)


class ReadRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    action: Literal["use_full_input", "narrow_selection", "adjust_configuration", "provide_supported_input", "none"] = "none"
    retry_same_input: bool = False


class PptxReadCoverage(ReadCoverage):
    unit: Literal["pages"] = "pages"
    content_kind: Literal["index", "text_and_tables", "document_content"]
    detail: Literal["index", "content", "objects"]
    requested_pages: list[int] | None = None
    returned_pages: list[int]
    complete_pages: list[int]
    total_pages: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_pages(self) -> Self:
        selected = self.requested_pages if self.requested_pages is not None else list(range(1, self.total_pages + 1))
        if (
            any(page < 1 or page > self.total_pages for page in selected)
            or len(set(selected)) != len(selected)
            or self.scope != ("file" if self.requested_pages is None else "selection")
            or self.returned_pages != selected[: len(self.returned_pages)]
            or self.complete_pages != self.returned_pages[: len(self.complete_pages)]
            or self.returned != len(self.returned_pages)
            or self.total != len(selected)
            or (self.content_kind == "index") != (self.detail == "index")
            or self.detail == "objects"
            and self.content_kind != "document_content"
        ):
            raise ValueError("PPTX coverage must preserve the actual representation and ordered page prefix")
        return self


class ReadNext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    tool: Literal["attachment_read", "document_read"]
    arguments: dict[str, object]

    @model_validator(mode="after")
    def validate_read_call(self) -> Self:
        from bowhead_core.pptx_reading import validate_attachment_read_arguments, validate_pptx_slides

        if self.tool == "attachment_read":
            if set(self.arguments) != {"artifact_id", "detail", "slides", "expected_content_hash"}:
                raise ValueError("An attachment continuation requires its exact source hash, detail and selected pages")
            validate_attachment_read_arguments(self.arguments)
        elif (
            set(self.arguments) != {"source", "detail", "slides"}
            or not isinstance(self.arguments["source"], str)
            or not self.arguments["source"]
            or "/" in self.arguments["source"]
            or "\\" in self.arguments["source"]
            or self.arguments["detail"] not in ("index", "content", "objects")
        ):
            raise ValueError("A private next read requires an admitted source alias, detail and selected pages")
        validate_pptx_slides(self.arguments.get("slides"))
        return self


class ReadContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scope: Literal["file", "selection"]
    available: bool
    next: ReadNext | None
    reason: Literal["selection_complete", "route_unavailable", "page_content_limit", "later_pages_available"] | None

    @model_validator(mode="after")
    def validate_available(self) -> Self:
        if self.available != (self.next is not None):
            raise ValueError("Available continuation must name an actual next read")
        return self


class ReadInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    view: Literal["complete", "partial", "none"]
    coverage: ReadCoverage | PptxReadCoverage
    limit: ReadLimit | None = None
    full_input_available: bool | None = None
    recovery: ReadRecovery = Field(default_factory=ReadRecovery)
    continuation: ReadContinuation | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_pptx_view(self) -> Self:
        if isinstance(self.coverage, PptxReadCoverage):
            expected = (
                "complete" if len(self.coverage.complete_pages) == self.coverage.total else "partial" if self.coverage.returned_pages else "none"
            )
            if self.view != expected:
                raise ValueError("PPTX view must agree with complete page coverage")
            if self.continuation is not None and self.continuation.next is not None:
                args = self.continuation.next.arguments
                if args["detail"] != self.coverage.detail or any(page > self.coverage.total_pages for page in args["slides"]):
                    raise ValueError("PPTX continuation must retain its detail and existing pages")
        elif self.continuation is not None:
            raise ValueError("Page continuation requires PPTX coverage")
        return self


def read_info(
    *,
    view: Literal["complete", "partial", "none"],
    unit: str,
    returned: int | None = None,
    total: int | None = None,
    scope: Literal["file", "selection"] = "file",
    limit: ReadLimit | None = None,
    full_input_available: bool | None = None,
    recovery: ReadRecovery | None = None,
) -> dict:
    """Build a bounded receipt without inferring full-source or Tool authority."""
    return ReadInfo(
        view=view,
        coverage=ReadCoverage(scope=scope, unit=unit, returned=returned, total=total),
        limit=limit,
        full_input_available=full_input_available,
        recovery=recovery or ReadRecovery(),
    ).model_dump(mode="json")
