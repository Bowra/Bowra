"""Finite source contracts for the original private authoring service."""

from bowhead_core.documents.package_io import OFFICE_MIMES, OfficePackageError

PPTX_PROFILE = "pptx-script-worker-v1"
WORD_PROFILE = "docx-script-worker-v1"
AUTHORING_PROFILES = frozenset({PPTX_PROFILE, WORD_PROFILE})
WORD_CONTRACT = "private-docx-authoring-v1"
WORD_METHOD_REVISION = "sha256:5aae7c26af920cb8ebc034a955cd69f3b5ece80c65e6e467b52061b61f8639b8"


def source_name(profile: str) -> str:
    if profile == WORD_PROFILE:
        return "candidate.docx"
    if profile in {PPTX_PROFILE, "ordinary-pptx-worker-v1"}:
        return "candidate.pptx"
    raise OfficePackageError("document_authoring_profile_invalid")


def source_mime(profile: str) -> str:
    source_name(profile)
    return OFFICE_MIMES[".docx" if profile == WORD_PROFILE else ".pptx"]


def word_descriptor(requested_outputs: tuple[str, ...]) -> dict:
    """Use only the server's frozen request, never a script output mode."""
    if requested_outputs not in {("source",), ("source", "pdf")}:
        raise OfficePackageError("document_output_selection")
    return {"format": "docx", "source_name": "candidate.docx", "pdf_name": "candidate.pdf", "published_outputs": list(requested_outputs)}


def draft_source_name(draft: dict) -> str:
    """Resolve the fixed name from the immutable server-validated draft."""
    profile = draft.get("profile", "ordinary-pptx-worker-v1")
    name = source_name(profile)
    if profile == WORD_PROFILE:
        descriptor = draft.get("descriptor")
        if not isinstance(descriptor, dict) or descriptor != word_descriptor(tuple(descriptor.get("published_outputs", ()))):
            raise OfficePackageError("document_draft_invalid")
    elif "descriptor" in draft:
        raise OfficePackageError("document_draft_invalid")
    return name
