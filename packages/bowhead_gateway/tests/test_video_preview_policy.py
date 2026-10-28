from __future__ import annotations

import pytest
from bowhead_core.artifact_store import mime_to_family
from bowhead_gateway.media_preview import (
    INLINE_MEDIA_MIMES,
    is_previewable_video,
)
from bowhead_gateway.routes.files import resolve_effective_upload_mime


@pytest.mark.unit
@pytest.mark.parametrize(
    ("declared", "filename", "expected"),
    [
        ("video/mp4", "result.mp4", "video/mp4"),
        ("application/octet-stream", "result.mp4", "video/mp4"),
        ("video/webm", "result.webm", "video/webm"),
        ("video/ogg", "result.ogv", "video/ogg"),
        ("video/ogg", "result.ogg", "video/ogg"),
        ("video/quicktime", "result.mov", None),
        ("video/mp4", "result.html", None),
    ],
)
def test_video_upload_mime_policy(
    declared: str,
    filename: str,
    expected: str | None,
) -> None:
    assert resolve_effective_upload_mime(declared, filename) == expected


@pytest.mark.unit
def test_video_preview_policy_is_exact_and_has_a_distinct_family() -> None:
    assert is_previewable_video("video/mp4")
    assert is_previewable_video("VIDEO/WEBM")
    assert not is_previewable_video("video/quicktime")
    assert not is_previewable_video("image/svg+xml")
    assert "video/ogg" in INLINE_MEDIA_MIMES
    assert mime_to_family("video/mp4") == "video"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("declared", "filename", "expected"),
    [
        ("text/html", "dashboard.html", "text/html"),
        ("text/html", "dashboard.htm", "text/html"),
        ("application/xhtml+xml", "dashboard.xhtml", "application/xhtml+xml"),
        ("text/plain", "dashboard.html", None),
        ("application/octet-stream", "dashboard.xhtml", "application/xhtml+xml"),
        ("text/html", "dashboard.txt", None),
        ("application/xhtml+xml", "dashboard.html", None),
        ("application/javascript", "dashboard.html", None),
    ],
)
def test_html_upload_mime_policy(
    declared: str,
    filename: str,
    expected: str | None,
) -> None:
    assert resolve_effective_upload_mime(declared, filename) == expected


@pytest.mark.unit
def test_html_download_never_uses_the_main_origin_inline_allowlist() -> None:
    assert "text/html" not in INLINE_MEDIA_MIMES
    assert "application/xhtml+xml" not in INLINE_MEDIA_MIMES
