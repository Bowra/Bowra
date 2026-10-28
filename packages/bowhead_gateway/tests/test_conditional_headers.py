"""A compressing CDN weakens the entity-tags we issue; routes must still see the strong form.

Regression cover for the customer outage recorded in
``docs/sprints/brand-appearance-repair-2026-09-09/spike.md`` §2: behind EdgeOne the
personal appearance panel became unusable for every member because the browser
echoed ``W/"personal-v3:<sha>"`` into ``If-Match`` and every comparison is exact.
"""

from __future__ import annotations

import pytest
from bowhead_gateway.conditional_headers import (
    ConditionalHeaderCanonicalizationMiddleware,
    canonicalize_entity_tag_list,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

STRONG = '"personal-v3:' + "a" * 64 + '"'
WEAK = f"W/{STRONG}"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (WEAK, STRONG),
        (STRONG, STRONG),
        ("*", "*"),
        (f"{WEAK}, {STRONG}", f"{STRONG}, {STRONG}"),
        ('W/"a", W/"b"', '"a", "b"'),
        # Not a weak entity-tag: normalize, never repair or invent syntax.
        ("W/not-a-tag", "W/not-a-tag"),
        ("", ""),
    ],
)
def test_canonicalize_strips_only_the_weak_prefix(raw: str, expected: str) -> None:
    assert canonicalize_entity_tag_list(raw) == expected


def _echo_app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, str | None]:
        return {
            "if_match": request.headers.get("If-Match"),
            "if_none_match": request.headers.get("If-None-Match"),
            "unrelated": request.headers.get("X-Keep-Me"),
        }

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(ConditionalHeaderCanonicalizationMiddleware(_echo_app()))


@pytest.mark.unit
def test_route_sees_the_strong_form_for_both_conditional_headers(client: TestClient) -> None:
    response = client.get(
        "/echo",
        headers={"If-Match": WEAK, "If-None-Match": WEAK, "X-Keep-Me": "untouched"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["if_match"] == STRONG
    assert body["if_none_match"] == STRONG
    assert body["unrelated"] == "untouched"


@pytest.mark.unit
def test_absent_conditional_headers_are_not_invented(client: TestClient) -> None:
    body = client.get("/echo").json()

    assert body["if_match"] is None
    assert body["if_none_match"] is None


@pytest.mark.unit
def test_already_strong_and_wildcard_values_pass_through(client: TestClient) -> None:
    body = client.get("/echo", headers={"If-Match": "*", "If-None-Match": STRONG}).json()

    assert body["if_match"] == "*"
    assert body["if_none_match"] == STRONG


@pytest.mark.unit
async def test_downstream_scope_writes_stay_visible_to_outer_middleware() -> None:
    """The scope object must be mutated in place, never copied.

    Starlette's router writes ``scope["route"]`` while matching and
    ``PublicEntryContextMiddleware`` rewrites ``scope["path"]``. Handing a copy
    downstream strands those writes: ``RequestIdContextMiddleware`` reads
    ``scope["route"]`` after the call and would log every request as unmatched.
    """
    outer_scope: dict = {
        "type": "http",
        "headers": [(b"if-match", WEAK.encode())],
    }

    async def inner(scope, receive, send) -> None:  # noqa: ANN001 - ASGI signature
        assert dict(scope["headers"])[b"if-match"].decode() == STRONG
        scope["route"] = "/echo"

    await ConditionalHeaderCanonicalizationMiddleware(inner)(outer_scope, None, None)

    assert outer_scope["route"] == "/echo"


@pytest.mark.unit
async def test_non_http_scopes_are_forwarded_untouched() -> None:
    seen: list[dict] = []

    async def inner(scope, receive, send) -> None:  # noqa: ANN001 - ASGI signature
        seen.append(scope)

    middleware = ConditionalHeaderCanonicalizationMiddleware(inner)
    scope = {"type": "lifespan"}
    await middleware(scope, None, None)

    assert seen == [scope]
    assert seen[0] is scope


def test_entity_tags_match_accepts_a_tag_the_middleware_already_weakened_away() -> None:
    from bowhead_gateway.conditional_headers import entity_tags_match

    issued = 'W/"sad_36205813ddd9:1"'
    # Middleware strips request W/, leaving strong form; still must equal its own issued weak form.
    assert entity_tags_match('"sad_36205813ddd9:1"', issued)
    assert entity_tags_match(issued, issued)
    assert entity_tags_match('"sad_36205813ddd9:1"', '"sad_36205813ddd9:1"')
    assert not entity_tags_match('"sad_36205813ddd9:2"', issued)
    assert not entity_tags_match(None, issued)
