"""Phase 1.2 receiver route UT — gateway-side.

Mounts a minimal FastAPI app with the receiver router and a fake
``LocalBackend``-shaped ``app.state.artifact_backend``. Sends raw-body POST
with Authorization header to drive every error path + success.

Scope per tech-design §6.3:
- JWT verification (aud / aid / expiry / signature)
- Path-traversal guard via aid regex + Path.resolve+is_relative_to
- Chunk-counter overflow → 413
- Empty body → 400
- Atomic ``.partial`` → ``os.replace`` on success
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import jwt
import pytest
from bowhead_gateway.routes import files_local_receiver
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "phase12-receiver-test-secret"


@dataclass
class _FakeBackend:
    """Mimics LocalBackend shape that receiver route reads (name + root_dir)."""

    name: str
    root_dir: Path


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(files_local_receiver.router)
    a.state.local_presign_secret = SECRET
    a.state.artifact_backend = _FakeBackend(name="local", root_dir=tmp_path)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _mint(aid: str, filename: str, mime: str, size_max: int, *, secret: str = SECRET, aud: str = "local-presign", exp_delta: int = 600) -> str:
    payload = {
        "aud": aud,
        "aid": aid,
        "filename": filename,
        "mime": mime,
        "size_max": size_max,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_delta,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


# ── happy path ───────────────────────────────────────────────────


def test_receiver_success_writes_target_and_returns_204(client: TestClient, app: FastAPI) -> None:
    aid = "art_abcdef012345"
    token = _mint(aid, "report.pdf", "application/pdf", size_max=10_000)
    body = b"%PDF-1.4 hello world"
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/pdf"},
        content=body,
    )
    assert r.status_code == 204, r.text
    # File written + partial cleaned up
    root: Path = app.state.artifact_backend.root_dir
    target = root / f"{aid}_report.pdf"
    assert target.is_file()
    assert target.read_bytes() == body
    assert not target.with_suffix(target.suffix + ".partial").exists()


# ── aid shape ────────────────────────────────────────────────────


def test_receiver_bad_aid_shape_403(client: TestClient) -> None:
    # No prefix
    r = client.post(
        "/api/v1/files/local-receiver/notanaid",
        headers={"Authorization": "Bearer x"},
        content=b"x",
    )
    assert r.status_code == 403


def test_receiver_aid_path_traversal_blocked(client: TestClient) -> None:
    # Path-traversal injection blocked by aid regex
    r = client.post(
        "/api/v1/files/local-receiver/..%2Fetc%2Fpasswd",
        headers={"Authorization": "Bearer x"},
        content=b"x",
    )
    assert r.status_code in (403, 404)  # either aid-regex fail or unmatched route


# ── auth ─────────────────────────────────────────────────────────


def test_receiver_missing_authorization_401(client: TestClient) -> None:
    aid = "art_abcdef012345"
    r = client.post(f"/api/v1/files/local-receiver/{aid}", content=b"x")
    assert r.status_code == 401


def test_receiver_bad_token_401(client: TestClient) -> None:
    aid = "art_abcdef012345"
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": "Bearer not-a-jwt"},
        content=b"x",
    )
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "file_upload_authorization_failed"
    assert "not-a-jwt" not in r.text


def test_receiver_wrong_audience_401(client: TestClient) -> None:
    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "text/plain", size_max=100, aud="OTHER")
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"x",
    )
    assert r.status_code == 401


def test_receiver_expired_token_401_with_www_authenticate(client: TestClient) -> None:
    """Per RFC 6750 + tech-design §5.5: expired token is 401 (re-presign + retry)
    with WWW-Authenticate hint; NOT 410 (gone forever). Phase 1.2 review P1-A-2.
    """
    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "text/plain", size_max=100, exp_delta=-60)
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"x",
    )
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert "expired" in r.headers["WWW-Authenticate"].lower()


def test_receiver_aid_mismatch_403(client: TestClient) -> None:
    """JWT for aid X used against route /local-receiver/Y → 403."""
    token = _mint("art_aaaaaaaaaaaa", "x.bin", "text/plain", size_max=100)
    r = client.post(
        "/api/v1/files/local-receiver/art_bbbbbbbbbbbb",
        headers={"Authorization": f"Bearer {token}"},
        content=b"x",
    )
    assert r.status_code == 403


def test_receiver_wrong_secret_401(client: TestClient) -> None:
    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "text/plain", size_max=100, secret="WRONG-SECRET")
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"x",
    )
    assert r.status_code == 401


# ── size limits ──────────────────────────────────────────────────


def test_receiver_body_exceeds_size_max_413(client: TestClient, app: FastAPI) -> None:
    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "application/octet-stream", size_max=10)
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"A" * 50,  # 5x cap
    )
    assert r.status_code == 413
    # Partial cleaned up
    root: Path = app.state.artifact_backend.root_dir
    partial = root / f"{aid}_x.bin.partial"
    assert not partial.exists()


def test_receiver_empty_body_400(client: TestClient, app: FastAPI) -> None:
    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "text/plain", size_max=100)
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"",
    )
    assert r.status_code == 400
    # No file left on disk
    root: Path = app.state.artifact_backend.root_dir
    assert not (root / f"{aid}_x.bin").exists()


def test_receiver_size_max_missing_400(client: TestClient) -> None:
    """JWT lacking size_max (or 0) must be rejected before chunk loop runs."""
    aid = "art_abcdef012345"
    # Manually mint a JWT without size_max
    payload = {
        "aud": "local-presign",
        "aid": aid,
        "filename": "x.bin",
        "mime": "text/plain",
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"x",
    )
    assert r.status_code == 400


# ── backend state guard ──────────────────────────────────────────


def test_receiver_503_when_backend_not_local(tmp_path: Path) -> None:
    """If app.state.artifact_backend is missing or not 'local', refuse."""
    app = FastAPI()
    app.include_router(files_local_receiver.router)
    app.state.local_presign_secret = SECRET
    app.state.artifact_backend = _FakeBackend(name="s3", root_dir=tmp_path)
    client = TestClient(app)
    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "text/plain", size_max=100)
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"x",
    )
    assert r.status_code == 503


def test_receiver_503_when_secret_missing(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(files_local_receiver.router)
    app.state.local_presign_secret = None
    app.state.artifact_backend = _FakeBackend(name="local", root_dir=tmp_path)
    client = TestClient(app)
    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "text/plain", size_max=100)
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"x",
    )
    assert r.status_code == 503


# ── replay / clobber protection (review P1-S-1) ───────────────────


def test_receiver_replay_clobber_blocked_409(client: TestClient, app: FastAPI) -> None:
    """Same JWT POSTed twice within exp → second call must NOT overwrite the
    first artifact body. Review P1-S-1.
    """
    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "text/plain", size_max=200, exp_delta=600)
    first = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"first-body",
    )
    assert first.status_code == 204, first.text
    second = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"OVERWRITE-attempt",
    )
    assert second.status_code == 409
    root: Path = app.state.artifact_backend.root_dir
    assert (root / f"{aid}_x.bin").read_bytes() == b"first-body"


def test_receiver_stale_partial_blocks_409(client: TestClient, app: FastAPI) -> None:
    """Orphan .partial from a crashed upload blocks new POST → 409
    (avoid clobbering in-progress upload).
    """
    aid = "art_abcdef012345"
    root: Path = app.state.artifact_backend.root_dir
    orphan = root / f"{aid}_x.bin.partial"
    orphan.write_bytes(b"orphan")
    token = _mint(aid, "x.bin", "text/plain", size_max=200)
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"new-body",
    )
    assert r.status_code == 409
    # Orphan preserved (operator cleans up explicitly)
    assert orphan.read_bytes() == b"orphan"


# ── ClientDisconnect mid-stream cleanup (review P1-O-3) ───────────


def test_receiver_cleanup_on_mid_stream_disconnect(monkeypatch, client: TestClient, app: FastAPI) -> None:
    """Simulate disconnect mid-stream — assert .partial cleaned up + 500 surfaced.
    Review P1-O-3 test gap for client-disconnect cleanup.
    """
    from starlette.requests import ClientDisconnect, Request

    aid = "art_abcdef012345"
    token = _mint(aid, "x.bin", "text/plain", size_max=10_000)

    async def bad_stream(self):  # type: ignore[no-untyped-def]
        yield b"first-chunk-"
        raise ClientDisconnect("simulated mid-stream disconnect")

    monkeypatch.setattr(Request, "stream", bad_stream)
    r = client.post(
        f"/api/v1/files/local-receiver/{aid}",
        headers={"Authorization": f"Bearer {token}"},
        content=b"ignored",
    )
    assert r.status_code == 500
    root: Path = app.state.artifact_backend.root_dir
    assert not (root / f"{aid}_x.bin.partial").exists()
    assert not (root / f"{aid}_x.bin").exists()


# ── min secret length validation (review P1-S-2) ──────────────────


def test_resolve_local_presign_secret_rejects_short_env_secret(monkeypatch) -> None:
    """`BOWHEAD_LOCAL_PRESIGN_SECRET` < 32 chars must refuse to boot."""
    from bowhead_gateway.main import _resolve_local_presign_secret
    from bowhead_gateway.settings import Settings

    s = Settings(allow_local_receiver=True, local_presign_secret="too-short")
    with pytest.raises(RuntimeError, match="≥32"):
        _resolve_local_presign_secret(s)


def test_resolve_local_presign_secret_accepts_long_env_secret() -> None:
    from bowhead_gateway.main import _resolve_local_presign_secret
    from bowhead_gateway.settings import Settings

    long_secret = "a" * 32
    s = Settings(allow_local_receiver=True, local_presign_secret=long_secret)
    assert _resolve_local_presign_secret(s) == long_secret


def test_resolve_local_presign_secret_auto_gen_when_blank() -> None:
    """Blank env + allow_local_receiver=True → auto-gen 43-char base64 secret."""
    from bowhead_gateway.main import _resolve_local_presign_secret
    from bowhead_gateway.settings import Settings

    s = Settings(allow_local_receiver=True, local_presign_secret="")
    sec = _resolve_local_presign_secret(s)
    assert sec is not None
    assert len(sec) >= 32


def test_resolve_local_presign_secret_none_when_disabled() -> None:
    """allow_local_receiver=False → None (LocalBackend disables presign path)."""
    from bowhead_gateway.main import _resolve_local_presign_secret
    from bowhead_gateway.settings import Settings

    s = Settings(allow_local_receiver=False, local_presign_secret="anything")
    assert _resolve_local_presign_secret(s) is None


def test_resolve_local_presign_secret_autogen_differs_per_call(monkeypatch) -> None:
    """**Documentation test for review P0-S-1**: blank-secret + auto-gen path
    yields a DIFFERENT secret every call. Therefore main.py MUST call this
    helper ONCE per lifespan and share the value between LocalBackend and
    app.state. A regression that calls it twice would silently break JWT
    verification (FE signs with secret A, receiver verifies with secret B).
    """
    from bowhead_gateway.main import _resolve_local_presign_secret
    from bowhead_gateway.settings import Settings

    s = Settings(allow_local_receiver=True, local_presign_secret="")
    a = _resolve_local_presign_secret(s)
    b = _resolve_local_presign_secret(s)
    assert a is not None and b is not None
    assert a != b, "auto-gen must produce different secrets per call (callers must share)"
