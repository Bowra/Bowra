"""HTTP adapter for the shared deployment sharing policy."""

from bowhead_core.share_service import ContentSharingDisabledError, require_content_sharing
from fastapi import HTTPException


def require_sharing_expansion(kind: str) -> None:
    """Call after management authorization and before any mutation."""
    try:
        require_content_sharing(kind)
    except ContentSharingDisabledError as error:
        raise HTTPException(403, {"error": {"code": error.code}}) from error
