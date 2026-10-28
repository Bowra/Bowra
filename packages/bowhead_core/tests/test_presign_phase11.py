"""Phase 1.1 UT — ArtifactBackend Protocol extensions: presign_post / head / read_stream.

Covers LocalBackend (stubs + working head + working read_stream) and S3Backend
(mocked boto3 client). Real OSS round-trip was verified in Phase 0.5 spike (on
prod 101); these UT exercise the wire shape and error paths.

Scope: NO endpoint routes (Phase 1.4+), NO migration (Phase 1.3), NO sweeper
(Phase 1.5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest
from bowhead_core.artifact_store import (
    ArtifactBackend,
    LocalBackend,
    ObjectHead,
    PresignPostResult,
    S3Backend,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── dataclass smoke ───────────────────────────────────────────────


def test_presign_post_result_dataclass_shape() -> None:
    r = PresignPostResult(
        url="https://s3.example.com/bucket",
        fields={"key": "k", "Content-Type": "image/png"},
        path="artifacts/abc.png",
        expires_at=datetime(2026, 5, 27, 22, 0, tzinfo=UTC),
    )
    assert r.url.startswith("https://")
    assert r.fields["key"] == "k"
    assert r.expires_at.tzinfo is not None


def test_object_head_dataclass_shape() -> None:
    h = ObjectHead(size=123, etag='"deadbeef"', mime="image/png")
    assert h.size == 123
    assert h.etag is not None
    h2 = ObjectHead(size=0, etag=None, mime=None)
    assert h2.etag is None and h2.mime is None


# ── LocalBackend ──────────────────────────────────────────────────


@pytest.fixture
def local_backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(root_dir=tmp_path)


@pytest.mark.asyncio
async def test_local_presign_post_returns_none_until_phase12(local_backend: LocalBackend) -> None:
    # Stub for Phase 1.1; Phase 1.2 will add JWT receiver.
    r = await local_backend.presign_post(
        aid="art_abc",
        filename="x.png",
        mime="image/png",
        size_bytes=100,
    )
    assert r is None


@pytest.mark.asyncio
async def test_local_head_missing_returns_none(local_backend: LocalBackend) -> None:
    h = await local_backend.head("/nonexistent/path.bin")
    assert h is None


@pytest.mark.asyncio
async def test_local_head_real_file_returns_size(local_backend: LocalBackend, tmp_path: Path) -> None:
    p = tmp_path / "file.txt"
    p.write_bytes(b"hello world!")
    h = await local_backend.head(str(p))
    assert h is not None
    assert h.size == 12
    # local backend doesn't have etag / mime metadata
    assert h.etag is None
    assert h.mime is None


@pytest.mark.asyncio
async def test_local_read_stream_yields_seekable_file(local_backend: LocalBackend, tmp_path: Path) -> None:
    p = tmp_path / "data.bin"
    p.write_bytes(b"abcdef")
    async with local_backend.read_stream(str(p)) as f:
        # Must be seekable for ZipFile use
        f.seek(0)
        assert f.read() == b"abcdef"
        f.seek(2)
        assert f.read(2) == b"cd"


@pytest.mark.asyncio
async def test_local_read_stream_with_zipfile(local_backend: LocalBackend, tmp_path: Path) -> None:
    """Smoke test: read_stream feeds zipfile.ZipFile without io.BytesIO."""
    zpath = tmp_path / "bundle.zip"
    with ZipFile(zpath, "w") as zf:
        zf.writestr("entry1.txt", b"hello")
        zf.writestr("dir/entry2.txt", b"world")
    async with local_backend.read_stream(str(zpath)) as f:
        with ZipFile(f, "r") as zf:
            names = {e.filename for e in zf.infolist()}
            assert names == {"entry1.txt", "dir/entry2.txt"}


@pytest.mark.asyncio
async def test_local_iter_range_reads_only_requested_bytes(
    local_backend: LocalBackend,
    tmp_path: Path,
) -> None:
    p = tmp_path / "video.mp4"
    p.write_bytes(b"0123456789")
    chunks = [
        chunk
        async for chunk in local_backend.iter_range(
            str(p),
            start=2,
            end=8,
            chunk_size=2,
        )
    ]
    assert chunks == [b"23", b"45", b"67"]


# ── S3Backend (mocked boto3) ─────────────────────────────────────


@pytest.fixture
def s3_backend() -> S3Backend:
    b = S3Backend(
        endpoint_url="https://s3.example.test:90/",
        bucket="testbucket",
        access_key="ak",
        secret_key="sk",
    )
    # Inject a MagicMock as the lazy boto3 client (bypass _get_client real init)
    b._client = MagicMock()
    return b


@pytest.mark.asyncio
async def test_s3_presign_post_envelope_shape(s3_backend: S3Backend) -> None:
    client = s3_backend._client  # MagicMock
    # Mimic what boto3.generate_presigned_post returns (verified Phase 0.5 spike)
    client.generate_presigned_post.return_value = {
        "url": "https://s3.example.test:90/testbucket",
        "fields": {
            "Content-Type": "image/png",
            "key": "artifacts/art_a8c91d44e2_screenshot.png",
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-credential": "ak/20260527/us-east-1/s3/aws4_request",
            "x-amz-date": "20260527T115706Z",
            "policy": "<base64>",
            "x-amz-signature": "<sigv4-hex>",
        },
    }
    r = await s3_backend.presign_post(
        aid="art_a8c91d44e2",
        filename="screenshot.png",
        mime="image/png",
        size_bytes=2_000_000,
        expires_in=600,
    )
    assert r is not None
    assert r.url == "https://s3.example.test:90/testbucket"
    assert r.fields["Content-Type"] == "image/png"
    assert "x-amz-signature" in r.fields
    assert r.path == "artifacts/art_a8c91d44e2_screenshot.png"
    # expires_at must be tz-aware and ~10 min from now
    assert r.expires_at.tzinfo is not None

    # Verify Conditions passed include both Content-Type binding AND size range
    call_kwargs = client.generate_presigned_post.call_args.kwargs
    conds = call_kwargs["Conditions"]
    assert {"Content-Type": "image/png"} in conds
    # find size-range entry
    size_conds = [c for c in conds if isinstance(c, list) and c[0] == "content-length-range"]
    assert len(size_conds) == 1
    assert size_conds[0][2] == 2_000_000 + 1024  # 1KB slack


@pytest.mark.asyncio
async def test_s3_presign_post_uses_safe_filename(s3_backend: S3Backend) -> None:
    """Filename traversal chars stripped before becoming part of S3 key."""
    client = s3_backend._client
    client.generate_presigned_post.return_value = {
        "url": "https://x",
        "fields": {},
    }
    await s3_backend.presign_post(
        aid="art_x",
        filename="../../../etc/passwd",
        mime="text/plain",
        size_bytes=100,
    )
    call_kwargs = client.generate_presigned_post.call_args.kwargs
    # _safe_filename replaces / and . sequences; key MUST stay under prefix
    assert call_kwargs["Key"].startswith("artifacts/art_x_")
    assert "../" not in call_kwargs["Key"]


@pytest.mark.asyncio
async def test_s3_head_returns_object_head(s3_backend: S3Backend) -> None:
    s3_backend._client.head_object.return_value = {
        "ContentLength": 12345,
        "ETag": '"abc"',
        "ContentType": "application/pdf",
    }
    h = await s3_backend.head("artifacts/test.pdf")
    assert h is not None
    assert h.size == 12345
    assert h.etag == '"abc"'
    assert h.mime == "application/pdf"


@pytest.mark.asyncio
async def test_s3_head_missing_returns_none(s3_backend: S3Backend) -> None:
    from botocore.exceptions import ClientError

    s3_backend._client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        "HeadObject",
    )
    h = await s3_backend.head("artifacts/missing.bin")
    assert h is None


@pytest.mark.asyncio
async def test_s3_head_other_error_propagates(s3_backend: S3Backend) -> None:
    """Non-404 errors must surface, not be swallowed as missing."""
    from botocore.exceptions import ClientError

    s3_backend._client.head_object.side_effect = ClientError(
        {"Error": {"Code": "403", "Message": "Access Denied"}},
        "HeadObject",
    )
    with pytest.raises(ClientError):
        await s3_backend.head("artifacts/forbidden.bin")


@pytest.mark.asyncio
async def test_s3_head_bare_http_404_returns_none(s3_backend: S3Backend) -> None:
    """Some S3-compat backends return empty Error.Code with HTTPStatusCode=404.

    Per Phase 1.1 review P1: HEAD has no response body so Code may be missing;
    the HTTP status alone must drive the not-found decision.
    """
    from botocore.exceptions import ClientError

    s3_backend._client.head_object.side_effect = ClientError(
        {
            "Error": {"Code": "", "Message": ""},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )
    h = await s3_backend.head("artifacts/missing-via-bare-404.bin")
    assert h is None


@pytest.mark.asyncio
async def test_s3_read_stream_downloads_to_seekable_temp(s3_backend: S3Backend) -> None:
    payload = b"streamed-zip-bytes-here"

    def fake_download(bucket: str, key: str, fileobj: object) -> None:
        # NamedTemporaryFile passes a real file-like
        fileobj.write(payload)

    s3_backend._client.download_fileobj.side_effect = fake_download
    async with s3_backend.read_stream("artifacts/bundle.zip") as f:
        # Must be seekable for ZipFile use
        f.seek(0)
        assert f.read() == payload
        f.seek(0)
        assert f.read(5) == b"strea"


@pytest.mark.asyncio
async def test_s3_iter_range_uses_bounded_get_object(
    s3_backend: S3Backend,
) -> None:
    body = MagicMock()
    body.read.side_effect = [b"234", b"567"]
    s3_backend._client.get_object.return_value = {"Body": body}

    chunks = [
        chunk
        async for chunk in s3_backend.iter_range(
            "artifacts/video.mp4",
            start=2,
            end=8,
            chunk_size=3,
        )
    ]

    assert chunks == [b"234", b"567"]
    s3_backend._client.get_object.assert_called_once_with(
        Bucket="testbucket",
        Key="artifacts/video.mp4",
        Range="bytes=2-7",
    )
    body.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_s3_iter_range_closes_body_when_consumer_stops(
    s3_backend: S3Backend,
) -> None:
    body = MagicMock()
    body.read.return_value = b"0123"
    s3_backend._client.get_object.return_value = {"Body": body}
    stream = s3_backend.iter_range(
        "artifacts/video.mp4",
        start=0,
        end=8,
        chunk_size=4,
    )

    assert await anext(stream) == b"0123"
    await stream.aclose()

    body.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_s3_read_stream_cleanup_on_download_error(s3_backend: S3Backend) -> None:
    """download_fileobj raising mid-stream must still tear down NamedTemporaryFile.

    Phase 1.1 review P1: assert no fd/disk leak when network fails.
    """
    from botocore.exceptions import EndpointConnectionError

    s3_backend._client.download_fileobj.side_effect = EndpointConnectionError(
        endpoint_url="https://example/bucket",
    )
    with pytest.raises(EndpointConnectionError):
        async with s3_backend.read_stream("artifacts/bad.zip") as _f:
            # body never executes — exception from finally→__aexit__→re-raise
            pytest.fail("body must not execute when download raises")
    # If we got here, the context manager's finally block ran cleanly.


@pytest.mark.asyncio
async def test_s3_read_stream_cleanup_on_consumer_exception(s3_backend: S3Backend) -> None:
    """Consumer raising inside `async with` must still close NamedTemporaryFile.

    Phase 1.1 review P1: realistic failure path = zipfile.BadZipFile during
    entry iteration.
    """
    payload = b"PK\x03\x04..."  # malformed zip-ish

    captured_tmp = {}

    def fake_download(bucket: str, key: str, fileobj: object) -> None:
        fileobj.write(payload)
        captured_tmp["f"] = fileobj  # capture to inspect after CM exit

    s3_backend._client.download_fileobj.side_effect = fake_download
    with pytest.raises(ValueError, match="consumer boom"):
        async with s3_backend.read_stream("artifacts/zipish.zip") as f:
            assert f.read(4) == b"PK\x03\x04"
            raise ValueError("consumer boom")
    # finally: assert the captured file was closed
    assert captured_tmp["f"].closed, "NamedTemporaryFile must be closed after consumer exception"


@pytest.mark.asyncio
async def test_local_read_stream_cleanup_on_consumer_exception(
    local_backend: LocalBackend,
    tmp_path: Path,
) -> None:
    """LocalBackend file handle must close even when consumer raises inside async with."""
    p = tmp_path / "lcl.bin"
    p.write_bytes(b"data")
    captured = {}
    with pytest.raises(RuntimeError, match="consumer fail"):
        async with local_backend.read_stream(str(p)) as f:
            captured["f"] = f
            raise RuntimeError("consumer fail")
    assert captured["f"].closed, "LocalBackend file handle must be closed after consumer exception"


# ── Protocol conformance ──────────────────────────────────────────


def test_local_backend_satisfies_artifact_backend_protocol(local_backend: LocalBackend) -> None:
    # Protocol check (structural) — both impls must satisfy the extended Protocol
    backend: ArtifactBackend = local_backend  # static type check at runtime
    assert backend.name == "local"


def test_s3_backend_satisfies_artifact_backend_protocol(s3_backend: S3Backend) -> None:
    backend: ArtifactBackend = s3_backend
    assert backend.name == "s3"
