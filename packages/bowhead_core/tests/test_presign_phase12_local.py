"""Phase 1.2 LocalBackend.presign_post UT — JWT envelope shape + gate rules.

Receiver route UT lives separately in bowhead_gateway tests since it needs
FastAPI + TestClient. This file covers only the backend-level signing.
"""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
from bowhead_core.artifact_store import LocalBackend

SECRET = "test-secret-phase12"
BASE_URL = "http://localhost:8090"


@pytest.fixture
def opted_in_backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(
        root_dir=tmp_path,
        gateway_base_url=BASE_URL,
        local_presign_secret=SECRET,
        allow_local_receiver=True,
    )


# ── gate rules (returns None when not fully configured) ──────────


@pytest.mark.asyncio
async def test_presign_post_returns_none_when_allow_false(tmp_path: Path) -> None:
    b = LocalBackend(
        root_dir=tmp_path,
        gateway_base_url=BASE_URL,
        local_presign_secret=SECRET,
        allow_local_receiver=False,
    )
    r = await b.presign_post(aid="art_abcdef012345", filename="x.bin", mime="application/octet-stream", size_bytes=100)
    assert r is None


@pytest.mark.asyncio
async def test_presign_post_returns_none_when_url_unset(tmp_path: Path) -> None:
    b = LocalBackend(root_dir=tmp_path, local_presign_secret=SECRET, allow_local_receiver=True)
    r = await b.presign_post(aid="art_abcdef012345", filename="x.bin", mime="application/octet-stream", size_bytes=100)
    assert r is None


@pytest.mark.asyncio
async def test_presign_post_returns_none_when_secret_unset(tmp_path: Path) -> None:
    b = LocalBackend(root_dir=tmp_path, gateway_base_url=BASE_URL, allow_local_receiver=True)
    r = await b.presign_post(aid="art_abcdef012345", filename="x.bin", mime="application/octet-stream", size_bytes=100)
    assert r is None


# ── aid shape ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_presign_post_rejects_bad_aid_shape(opted_in_backend: LocalBackend) -> None:
    """aid MUST be ``art_[a-f0-9]{12}`` — anything else is an integration bug."""
    with pytest.raises(ValueError, match="bad aid"):
        await opted_in_backend.presign_post(
            aid="../etc/passwd",
            filename="x.bin",
            mime="text/plain",
            size_bytes=100,
        )
    with pytest.raises(ValueError, match="bad aid"):
        await opted_in_backend.presign_post(
            aid="art_xx",
            filename="x.bin",
            mime="text/plain",
            size_bytes=100,
        )
    with pytest.raises(ValueError, match="bad aid"):
        await opted_in_backend.presign_post(
            aid="ART_ABCDEF012345",
            filename="x.bin",
            mime="text/plain",
            size_bytes=100,
        )


# ── envelope shape ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_presign_post_returns_envelope_with_jwt(opted_in_backend: LocalBackend, tmp_path: Path) -> None:
    aid = "art_abcdef012345"
    r = await opted_in_backend.presign_post(
        aid=aid,
        filename="report.pdf",
        mime="application/pdf",
        size_bytes=2_000_000,
        expires_in=300,
    )
    assert r is not None
    assert r.url == f"/api/v1/files/local-receiver/{aid}"
    assert r.fields["Content-Type"] == "application/pdf"
    assert r.fields["Authorization"].startswith("Bearer ")
    # path field reflects where the receiver WILL write
    assert r.path.endswith(f"{aid}_report.pdf")
    # expires_at is tz-aware
    assert r.expires_at.tzinfo is not None


@pytest.mark.asyncio
async def test_presign_post_jwt_decodes_with_expected_claims(opted_in_backend: LocalBackend) -> None:
    aid = "art_abcdef012345"
    r = await opted_in_backend.presign_post(
        aid=aid,
        filename="x.bin",
        mime="application/octet-stream",
        size_bytes=1000,
        expires_in=300,
    )
    assert r is not None
    token = r.fields["Authorization"].removeprefix("Bearer ")
    decoded = jwt.decode(token, SECRET, algorithms=["HS256"], audience="local-presign")
    assert decoded["aid"] == aid
    assert decoded["filename"] == "x.bin"
    assert decoded["mime"] == "application/octet-stream"
    # size_max = size_bytes + 1024 (1KB slack matching S3 Conditions)
    assert decoded["size_max"] == 1000 + 1024
    assert "exp" in decoded
    assert decoded["exp"] > time.time()


@pytest.mark.asyncio
async def test_presign_post_filename_sanitized_in_path(opted_in_backend: LocalBackend) -> None:
    """_safe_filename replaces / with _ but may leave ``..`` — what matters is
    that the resolved path stays inside root_dir (containment invariant).
    Receiver route enforces resolve+relative_to as second line of defense.
    """
    aid = "art_abcdef012345"
    r = await opted_in_backend.presign_post(
        aid=aid,
        filename="../../etc/passwd",
        mime="text/plain",
        size_bytes=100,
    )
    assert r is not None
    # No literal directory separator in the path beyond root_dir
    target = Path(r.path)
    root = Path(opted_in_backend.root_dir).resolve()
    # Containment: resolved target MUST be under root_dir
    assert target.resolve().is_relative_to(root), f"path escapes root: {target}"
    # Filename component has no slashes (replaced)
    assert "/" not in target.name
    # aid prefix preserved
    assert target.name.startswith(f"{aid}_")


@pytest.mark.asyncio
async def test_presign_post_jwt_signed_with_configured_secret_only(opted_in_backend: LocalBackend) -> None:
    """JWT must NOT verify with a wrong secret — sanity check."""
    aid = "art_abcdef012345"
    r = await opted_in_backend.presign_post(
        aid=aid,
        filename="x.bin",
        mime="text/plain",
        size_bytes=100,
    )
    assert r is not None
    token = r.fields["Authorization"].removeprefix("Bearer ")
    # Right secret: decodes
    jwt.decode(token, SECRET, algorithms=["HS256"], audience="local-presign")
    # Wrong secret: raises
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "WRONG-SECRET", algorithms=["HS256"], audience="local-presign")
    # Wrong audience: raises
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(token, SECRET, algorithms=["HS256"], audience="other-aud")
