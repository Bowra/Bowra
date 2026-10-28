"""Small shared HTTP header helpers.

Kept dependency-free so any route module can import it without cycles.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["content_disposition"]


def content_disposition(disposition: str, filename: str) -> str:
    """Build a Content-Disposition header value safe for arbitrary filenames.

    HTTP header values are latin-1, so a raw UTF-8 (e.g. Chinese) filename
    crashes the ASGI header encode (UnicodeEncodeError → 500), and interior
    CR/LF would split the header. Per RFC 6266 / RFC 5987 we emit:

      <disposition>; filename="<ascii-safe fallback>"; filename*=UTF-8''<pct>

    The ``filename`` token keeps only printable ASCII (drops control chars and
    quotes) for legacy clients; modern clients use the percent-encoded
    ``filename*``. ``disposition`` is a trusted literal ("inline"/"attachment").
    """
    ascii_fallback = "".join(c for c in (filename or "") if 0x20 <= ord(c) < 0x7F and c != '"').strip()
    if not ascii_fallback or ascii_fallback in (".", ".."):
        ascii_fallback = "download"
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename or '', safe='')}"
