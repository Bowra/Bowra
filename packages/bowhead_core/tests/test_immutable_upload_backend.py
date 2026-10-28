"""Exact-byte create-only storage and bounded reads; no external S3 requests.

Mutations: overwrite the candidate; remove the actual read limit; remove the
IfNoneMatch precondition. Storage/provider boundaries only are substituted.
"""

from __future__ import annotations

import io
from pathlib import Path
from uuid import uuid4

import anyio
import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from bowhead_core.artifact_store import ArtifactReadLimitError, LocalBackend, S3Backend

pytestmark = pytest.mark.integration


async def test_local_candidate_is_create_only_under_retries_and_concurrency(tmp_path) -> None:
    backend = LocalBackend(root_dir=tmp_path)
    path = backend.immutable_upload_path(uuid4().hex)

    async def put(data):
        await backend.put_immutable_upload(path, content=data, mime="application/zip")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(put, b"first")
        tasks.start_soon(put, b"second")
    winner = await backend.read_bounded(path, limit=6)
    assert winner in {b"first", b"second"}
    await put(b"late overwrite")
    assert await backend.read_bounded(path, limit=6) == winner


async def test_local_failed_publish_never_exposes_partial_candidate(tmp_path, monkeypatch) -> None:
    backend = LocalBackend(root_dir=tmp_path)
    path = backend.immutable_upload_path(uuid4().hex)
    real_link = Path.hardlink_to

    def fail_link(*args):
        raise OSError("simulated crash before atomic publish")

    monkeypatch.setattr(Path, "hardlink_to", fail_link)
    with pytest.raises(OSError):
        await backend.put_immutable_upload(path, content=b"complete bytes", mime="application/zip")
    assert not Path(path).exists()
    monkeypatch.setattr(Path, "hardlink_to", real_link)
    await backend.put_immutable_upload(path, content=b"complete bytes", mime="application/zip")
    assert await backend.read_bounded(path, limit=14) == b"complete bytes"


async def test_local_reader_enforces_actual_limit(tmp_path) -> None:
    backend = LocalBackend(root_dir=tmp_path)
    path = await backend.put(aid="art_test", content=b"123456", filename="test", mime="application/zip")
    with pytest.raises(ArtifactReadLimitError):
        await backend.read_bounded(path, limit=5)
    assert await backend.read_bounded(path, limit=6) == b"123456"


@pytest.fixture
def s3():
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    backend = S3Backend(endpoint_url="https://s3.test.invalid", bucket="test-bucket", access_key="test", secret_key="test", _client=client)
    with Stubber(client) as stub:
        yield backend, stub
        stub.assert_no_pending_responses()


async def test_s3_put_uses_conditional_create_and_accepts_only_exists_as_replay(s3) -> None:
    backend, stub = s3
    path = backend.immutable_upload_path(uuid4().hex)
    expected = {"Bucket": "test-bucket", "Key": path, "Body": b"first", "ContentType": "application/zip", "IfNoneMatch": "*"}
    stub.add_response("put_object", {}, expected)
    await backend.put_immutable_upload(path, content=b"first", mime="application/zip")
    stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412, expected_params=expected)
    await backend.put_immutable_upload(path, content=b"first", mime="application/zip")


@pytest.mark.parametrize("code, status", [("NotImplemented", 501), ("ConditionalRequestConflict", 409), ("AccessDenied", 403)])
async def test_s3_cannot_downgrade_failed_condition_to_unconditional_put(s3, code, status) -> None:
    backend, stub = s3
    path = backend.immutable_upload_path(uuid4().hex)
    stub.add_client_error(
        "put_object",
        code,
        http_status_code=status,
        expected_params={
            "Bucket": "test-bucket",
            "Key": path,
            "Body": b"bytes",
            "ContentType": "application/zip",
            "IfNoneMatch": "*",
        },
    )
    with pytest.raises(ClientError):
        await backend.put_immutable_upload(path, content=b"bytes", mime="application/zip")


@pytest.mark.parametrize("limit, exceeds", [(5, True), (6, False)])
async def test_s3_read_is_bounded_and_closes_body_even_on_limit_error(s3, limit, exceeds) -> None:
    backend, stub = s3

    class TrackedStream(io.BytesIO):
        reads = []

        def read(self, size=-1):
            self.reads.append(size)
            return super().read(size)

    stream = TrackedStream(b"123456")
    stub.add_response("get_object", {"Body": StreamingBody(stream, 6)}, {"Bucket": "test-bucket", "Key": "quarantine"})
    if exceeds:
        with pytest.raises(ArtifactReadLimitError):
            await backend.read_bounded("quarantine", limit=limit)
    else:
        assert await backend.read_bounded("quarantine", limit=limit) == b"123456"
    assert stream.reads[-1] == limit + 1 and stream.closed


@pytest.mark.parametrize("code, status, error", [("NoSuchKey", 404, FileNotFoundError), ("AccessDenied", 403, ClientError)])
async def test_s3_read_distinguishes_missing_from_provider_failure(s3, code, status, error) -> None:
    backend, stub = s3
    stub.add_client_error("get_object", code, http_status_code=status, expected_params={"Bucket": "test-bucket", "Key": "quarantine"})
    with pytest.raises(error):
        await backend.read_bounded("quarantine", limit=10)


@pytest.mark.parametrize("token", ["../outside", "x/y", "art_123", "A" * 32])
async def test_candidate_namespace_does_not_accept_caller_paths(tmp_path, token) -> None:
    backend = LocalBackend(root_dir=tmp_path)
    with pytest.raises(ValueError):
        backend.immutable_upload_path(token)
