"""Regression: Content-Disposition must be a safe HTTP header for arbitrary
filenames (non-ASCII / control chars), used by KB + canvas download routes.

HTTP headers are latin-1; a raw UTF-8 filename crashes the ASGI header encode
→ 500, and interior CR/LF would split the header. content_disposition emits an
ASCII-sanitized fallback + RFC 5987 filename* so the value is always safe.
"""

from __future__ import annotations

import pytest
from bowhead_gateway._http import content_disposition


@pytest.mark.parametrize(
    "filename",
    [
        "资产评估报告书.pdf",
        "2020年度审计报告.pdf",
        "序时账-上海熊熊2022年.XLS",
        "plain_ascii.pdf",
        'has"quote.pdf',
        "spaces and (parens).docx",
        "tab\tand\x00nul.pdf",
        "carriage\r\nreturn.pdf",
    ],
)
def test_content_disposition_is_header_safe(filename: str) -> None:
    cd = content_disposition("attachment", filename)
    # MUST be encodable as a latin-1 HTTP header (the actual prod crash)...
    cd.encode("latin-1")
    # ...and MUST NOT contain raw CR/LF (header injection / split).
    assert "\r" not in cd and "\n" not in cd
    assert cd.startswith("attachment; filename=")
    assert "filename*=UTF-8''" in cd


def test_content_disposition_strips_crlf_from_fallback() -> None:
    # CRLF is valid ASCII so a naive encode('ascii','ignore') would keep it →
    # header split. The fallback must drop control chars entirely.
    cd = content_disposition("attachment", "evil\r\nSet-Cookie: x.pdf")
    start = cd.index('filename="') + len('filename="')
    end = cd.index('"', start)
    fallback = cd[start:end]
    assert "\r" not in fallback and "\n" not in fallback
    assert "Set-Cookie" in fallback  # text kept, just the CRLF removed


def test_content_disposition_ascii_fallback_nonempty() -> None:
    cd = content_disposition("attachment", "审计报告")
    start = cd.index('filename="') + len('filename="')
    end = cd.index('"', start)
    assert cd[start:end] == "download"  # nothing ASCII survived → safe default


def test_content_disposition_preserves_ascii_extension() -> None:
    cd = content_disposition("inline", "资产评估.pdf")
    assert 'filename=".pdf"' in cd  # extension survives ascii filter
    assert cd.startswith("inline; ")
