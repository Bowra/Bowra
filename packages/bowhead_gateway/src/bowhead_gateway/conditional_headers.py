"""Canonicalize inbound conditional request headers before any route reads them.

Why this exists
---------------
A compressing intermediary (CDN) MUST weaken an entity-tag it forwards
(RFC 9110 §8.8.1): the origin's ``"personal-v3:<sha256>"`` reaches the browser as
``W/"personal-v3:<sha256>"``. The client then faithfully echoes that weakened form
back in ``If-Match`` / ``If-None-Match``, and every exact-string comparison we do
server-side stops matching. Observed on a customer deployment behind EdgeOne:
the personal appearance panel became unusable for every member, and all
conditional GETs turned into full transfers.

Semantics we deliberately adopt
-------------------------------
* ``If-None-Match`` is specified to use the **weak** comparison function
  (RFC 9110 §13.1.2), so stripping ``W/`` is simply the correct behaviour —
  our previous exact-string comparison was non-conforming regardless of any CDN.
* ``If-Match`` is specified to use the **strong** comparison function
  (RFC 9110 §13.1.1), under which a weak validator never matches. We knowingly
  deviate: every entity-tag this service issues is a digest of the resource
  content (``personal-v3:<sha256>``, ``brand-draft:<team>:<version>:<revision>``,
  …). The ``W/`` prefix here is added in transit and carries no semantic
  difference for us, so accepting it still detects every real concurrent change.
  **Do not "fix" this back to strong comparison** — that reintroduces the outage.

Scope
-----
Normalizing here, rather than at each comparison site, is deliberate: the
conditional headers are read both by hand
(``request.headers.get("If-Match")``) and through FastAPI header injection
(``Header(..., alias="If-Match")``), across gateway routes that keep growing.
One boundary means new routes inherit the behaviour instead of re-deriving it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

#: Headers whose values are lists of entity-tags we canonicalize.
CONDITIONAL_HEADERS: tuple[bytes, ...] = (b"if-match", b"if-none-match")

_WEAK_PREFIX = "W/"


def canonicalize_entity_tag_list(value: str) -> str:
    """Strip the weak-validator prefix from every entity-tag in a header value.

    ``*`` and anything that does not look like ``W/"…"`` is passed through
    untouched — this function normalizes, it never repairs or invents syntax.
    """

    parts = value.split(",")
    canonical: list[str] = []
    changed = False
    for part in parts:
        stripped = part.strip()
        if stripped.startswith(_WEAK_PREFIX) and stripped[len(_WEAK_PREFIX) :].startswith('"'):
            canonical.append(stripped[len(_WEAK_PREFIX) :])
            changed = True
        else:
            canonical.append(stripped)
    if not changed:
        return value
    return ", ".join(canonical)


def entity_tags_match(if_match: str | None, current: str) -> bool:
    """Whether an ``If-Match`` value names ``current`` once both sides are canonical.

    The middleware below has already stripped ``W/`` from the header, so a route that
    still *issues* ``W/"…"`` and compares by exact string can never match: 116 drafts
    (clarify / regenerate / create), team Agent status and edits, and the v1 chart-style
    policy all returned 409 on every write. Compare through this helper instead of ``!=``.
    """
    if if_match is None:
        return False
    return canonicalize_entity_tag_list(if_match.strip()) == canonicalize_entity_tag_list(current)


class ConditionalHeaderCanonicalizationMiddleware:
    """Rewrite ``If-Match`` / ``If-None-Match`` in the ASGI scope, once per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw_headers = scope.get("headers")
        if not raw_headers:
            await self.app(scope, receive, send)
            return

        rewritten: list[tuple[bytes, bytes]] = []
        changed = False
        for name, raw_value in raw_headers:
            if name.lower() not in CONDITIONAL_HEADERS:
                rewritten.append((name, raw_value))
                continue
            try:
                decoded = raw_value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - ASGI servers decode as latin-1
                rewritten.append((name, raw_value))
                continue
            canonical = canonicalize_entity_tag_list(decoded)
            if canonical == decoded:
                rewritten.append((name, raw_value))
                continue
            rewritten.append((name, canonical.encode("latin-1")))
            changed = True

        # Mutate in place; do NOT hand a copied scope downstream. Starlette's
        # router writes ``scope["route"]`` during matching and
        # PublicEntryContextMiddleware rewrites ``scope["path"]`` / ``scope["state"]``.
        # A copy would strand those writes where outer middleware cannot see them —
        # RequestIdContextMiddleware reads ``scope["route"]`` after the call and
        # would log every request as an unmatched route.
        if changed:
            scope["headers"] = rewritten
        await self.app(scope, receive, send)
