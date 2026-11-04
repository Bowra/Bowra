"""Ambient sign-in cookies must not authorize requests from artifact code."""

import pytest
from bowhead_gateway.cookie_request_security import CookieRequestSecurityMiddleware, cookie_request_is_same_origin
from bowhead_gateway.public_entry_context import PublicAccessContext, reset_public_access_context, set_public_access_context
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.mark.parametrize("cookie", ["bowhead_jwt", "bowhead_refresh"])
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "null"},
        {"Origin": "https://attacker.example"},
        {"Origin": "https://testserver:invalid"},
        {"Origin": "http://testserver:0"},
        {"Origin": ""},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"},
        {"Origin": "null", "Referer": "http://testserver/", "Sec-Fetch-Site": "same-origin"},
        {"Origin": "https://attacker.example", "Referer": "http://testserver/"},
        {"Origin": "null", "X-Bowhead-Public-Base": "http://testserver", "X-HTTP-Method-Override": "GET"},
        {},
    ],
    ids=[
        "opaque",
        "foreign",
        "malformed",
        "zero-port",
        "empty",
        "cross-site",
        "sibling-site",
        "opaque-with-matching-fallbacks",
        "foreign-with-matching-referer",
        "opaque-with-hints",
        "missing-evidence",
    ],
)
def test_cookie_mutation_is_rejected_before_the_handler(cookie, headers):
    app = FastAPI()
    app.add_middleware(CookieRequestSecurityMiddleware)
    writes = []

    @app.post("/write")
    async def write():
        writes.append(True)
        return {"ok": True}

    client = TestClient(app)
    client.cookies.set(cookie, "synthetic-credential")
    response = client.post(
        "/write", content="simple-form-compatible-body", headers={**headers, "Authorization": "Bearer cannot-bypass-cookie-priority"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "cross_origin_cookie_request_rejected"
    assert writes == []


@pytest.mark.parametrize("headers", [{"Origin": "http://testserver"}, {"Referer": "http://testserver/bowhead/"}, {"Sec-Fetch-Site": "same-origin"}])
def test_same_origin_cookie_mutations_remain_available(headers):
    app = FastAPI()
    app.add_middleware(CookieRequestSecurityMiddleware)

    @app.post("/write")
    async def write():
        return {"ok": True}

    client = TestClient(app)
    client.cookies.set("bowhead_jwt", "synthetic-credential")
    assert client.post("/write", headers=headers).status_code == 200


def test_bearer_integrations_and_get_callbacks_do_not_require_cookie_origin_evidence():
    app = FastAPI()
    app.add_middleware(CookieRequestSecurityMiddleware)

    @app.api_route("/probe", methods=["POST", "GET"])
    async def probe():
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/probe", headers={"Authorization": "Bearer synthetic", "Origin": "null"}).status_code == 200
    client.cookies.set("bowhead_jwt", "synthetic-credential")
    assert client.get("/probe", headers={"Origin": "https://identity-provider.example"}).status_code == 200


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
def test_cookie_origin_uses_request_transport_instead_of_team_link_entry(prefix):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/write",
            "headers": [(b"host", b"internal:8000"), (b"origin", b"https://browser.example:10010")],
        }
    )
    token = set_public_access_context(
        PublicAccessContext(
            base_url="https://default.example/default",
            origin="https://default.example",
            base_path="/default",
            app_root="https://default.example/default/",
            match_source="team_default",
            request_base_url=f"https://browser.example:10010{prefix}",
            request_match_source="browser_hint",
        )
    )
    try:
        assert cookie_request_is_same_origin(request)
    finally:
        reset_public_access_context(token)


@pytest.mark.parametrize("cookie", ["bowhead_jwt", "bowhead_refresh"])
@pytest.mark.parametrize("origin", ["null", "https://attacker.example", None])
def test_cookie_websocket_is_rejected_before_the_handler(cookie, origin):
    from fastapi import WebSocket
    from starlette.websockets import WebSocketDisconnect

    app = FastAPI()
    app.add_middleware(CookieRequestSecurityMiddleware)
    connections = []

    @app.websocket("/socket")
    async def socket(websocket: WebSocket):
        connections.append(True)
        await websocket.accept()
        await websocket.close()

    client = TestClient(app)
    client.cookies.set(cookie, "synthetic-credential")
    with pytest.raises(WebSocketDisconnect) as denied, client.websocket_connect("/socket", headers={"Origin": origin} if origin else {}):
        pytest.fail("An untrusted WebSocket reached the handler")
    assert denied.value.code == 1008
    assert connections == []


def test_same_origin_cookie_websocket_remains_available():
    from fastapi import WebSocket

    app = FastAPI()
    app.add_middleware(CookieRequestSecurityMiddleware)

    @app.websocket("/socket")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    client = TestClient(app)
    client.cookies.set("bowhead_jwt", "synthetic-credential")
    with client.websocket_connect("/socket", headers={"Origin": "http://testserver"}) as connection:
        assert connection.receive_json() == {"ok": True}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=synthetic"])
def test_simple_request_encodings_do_not_bypass_cookie_mutation_guard(method, content_type):
    app = FastAPI()
    app.add_middleware(CookieRequestSecurityMiddleware)
    writes = []

    @app.api_route("/write", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def write():
        writes.append(True)
        return {"ok": True}

    client = TestClient(app)
    client.cookies.set("bowhead_jwt", "synthetic")
    response = client.request(method, "/write", content="synthetic", headers={"Origin": "null", "Content-Type": content_type})
    assert response.status_code == 403
    assert writes == []
    assert client.request(method, "/write", headers={"Origin": "http://testserver"}).status_code == 200
    assert writes == [True]


@pytest.mark.parametrize("origin", ["null", "https://attacker.example"])
def test_cors_never_grants_credentialed_reads_or_custom_header_preflights(origin):
    from bowhead_gateway.http_request_context import cors_middleware_options
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI()
    app.add_middleware(CookieRequestSecurityMiddleware)
    app.add_middleware(CORSMiddleware, **cors_middleware_options())

    @app.get("/private")
    async def private(request: Request):
        assert request.cookies.get("bowhead_jwt") == "synthetic"
        return {"value": "synthetic-private"}

    client = TestClient(app)
    client.cookies.set("bowhead_jwt", "synthetic")
    # CORS prevents browser access to the response, not execution of safe GETs.
    read = client.get("/private", headers={"Origin": origin, "Range": "bytes=0-9"})
    assert read.status_code == 200
    assert read.headers.get("Access-Control-Allow-Credentials") != "true"
    preflight = client.options(
        "/private",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Bowhead-Public-Base,X-Forwarded-Host,Authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("Access-Control-Allow-Credentials") != "true"
