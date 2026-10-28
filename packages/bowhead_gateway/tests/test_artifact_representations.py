"""B0a-3: real builders + local blob/HTTP boundary, no parser mock on Raw.

Mutation targets: Raw must not call build_file_preview/open_read; strict decoder
must reject interior corruption and preserve whitespace; authorization precedes I/O.
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend, S3Backend
from bowhead_core.session_store import SessionStore
from bowhead_gateway.artifact_representations import RAW_LIMIT, build_representation
from bowhead_gateway.routes import files
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
async def stored(tmp_path):
    store = ArtifactStore(db_path=tmp_path / "a.db", backend=LocalBackend(root_dir=tmp_path / "blobs"))
    await store.initialize()
    art = await store.put_bytes(content=b" \r\na,b\r\n1,2\r\n ", filename="rows.csv", mime="text/csv", source="upload", created_by="owner")
    return store, art


async def test_raw_survives_preview_parser_failure_and_preserves_bytes(stored, monkeypatch):
    store, art = stored

    # This CSV's leading blank line produces incompatible DictReader row keys:
    # use the real parser failure, not a prearranged response.
    with pytest.raises(HTTPException) as exc:
        await build_representation(store, art, "preview")
    assert exc.value.status_code == 422

    async def forbidden(*args, **kwargs):
        pytest.fail("Raw/Details must not read the whole blob or call an extractor")

    monkeypatch.setattr(ArtifactStore, "open_read", forbidden)
    monkeypatch.setattr(files, "build_file_preview", forbidden)
    raw = await build_representation(store, art, "raw")
    assert raw.text == " \r\na,b\r\n1,2\r\n "
    assert raw.returned_byte_count == len(raw.text.encode())
    assert raw.total_byte_count == art.size and not raw.truncated
    assert raw.revision and raw.representation == "raw"
    await store.backend.delete(art.path)
    details = await build_representation(store, art, "details")
    assert details.details["filename"] == "rows.csv"
    assert not {"path", "created_by", "derived_from", "manifest_artifact_id"} & details.details.keys()
    with pytest.raises(HTTPException) as exc:
        await build_representation(store, art, "raw")
    assert exc.value.detail["error"]["code"] == "artifact_representation_unavailable"


@pytest.mark.parametrize("encoding,bom", [("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")])
async def test_raw_encoding_and_output_budget(stored, encoding, bom):
    store, _ = stored
    value = " \r\n中文🌊\t" * 90000
    payload = bom + value.encode(encoding)
    art = await store.put_bytes(content=payload, filename="data.txt", mime="text/plain", source="upload", created_by="owner")
    raw = await build_representation(store, art, "raw")
    assert raw.encoding == encoding
    assert raw.text and value.startswith(raw.text)
    assert raw.truncated and raw.total_byte_count == len(payload)
    assert raw.returned_byte_count == len(raw.text.encode("utf-8")) <= RAW_LIMIT


@pytest.mark.parametrize("payload", [b"", b" \n\t ", b"\xef\xbb\xbf", "中文🌊".encode()])
async def test_raw_empty_and_small(stored, payload):
    store, _ = stored
    art = await store.put_bytes(content=payload, filename="x.txt", mime="text/plain", source="upload", created_by="owner")
    raw = await build_representation(store, art, "raw")
    assert raw.text == payload.decode("utf-8-sig")
    assert not raw.truncated


@pytest.mark.parametrize("payload", [b"x\xffy", b"end\xe4", b"\xff\xfe\x00\xd8x\x00", b"\xff\xfe\x00\x00a\x00\x00\x00"])
async def test_raw_corrupt_encoding(stored, payload):
    store, _ = stored
    art = await store.put_bytes(content=payload, filename="x.txt", mime="text/plain", source="upload", created_by="owner")
    with pytest.raises(HTTPException) as exc:
        await build_representation(store, art, "raw")
    assert exc.value.status_code == 422


@pytest.mark.parametrize("offset", [0, 1, 2, 3])
async def test_raw_never_reads_beyond_budget_and_drops_only_partial_tail(stored, monkeypatch, offset):
    store, art = stored
    payload = b"a" * (RAW_LIMIT - offset) + "🌊".encode() + b"tail"
    calls = []

    async def bounded(self, artifact, *, start, end):
        calls.append((start, end))
        yield payload[start:end]

    monkeypatch.setattr(ArtifactStore, "iter_read_range", bounded)
    raw = await build_representation(store, replace(art, size=len(payload)), "raw")
    assert calls == [(0, RAW_LIMIT + 4)]
    assert raw.text == "a" * (RAW_LIMIT - offset)
    assert raw.truncated


@pytest.mark.parametrize(
    "mime,name",
    [("application/pdf", "x.pdf"), ("image/png", "x.png"), ("video/mp4", "x.mp4"), ("application/zip", "x.csv"), ("image/svg+xml", "x.svg")],
)
async def test_binary_raw_unsupported(stored, mime, name):
    store, art = stored
    with pytest.raises(HTTPException) as exc:
        await build_representation(store, replace(art, mime=mime, filename=name), "raw")
    assert exc.value.status_code == 415


@pytest.mark.parametrize("mode", ["preview", "raw", "details"])
async def test_files_authorization_and_missing_are_indistinguishable(stored, mode):
    store, art = stored
    art = await store.put_bytes(content=b"a,b\n1,2\n", filename="valid.csv", mime="text/csv", source="upload", created_by="owner")
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.settings = SimpleNamespace(dev_default_user="owner", auth_mode="dev")
    client = TestClient(app)
    path = f"/api/v1/files/{art.id}/representations/{mode}"
    assert client.get(path).status_code == 200
    denied = client.get(path, headers={"X-Bowhead-User-Id": "other"})
    missing = client.get(f"/api/v1/files/missing/representations/{mode}")
    await store.soft_delete(art.id, user_id="owner")
    deleted = client.get(path)
    for response in (denied, missing, deleted):
        assert response.status_code == 404
        assert response.json() == {"detail": {"error": {"code": "artifact_scope_unavailable"}}}


async def test_unauthenticated_representation_does_not_query_object(stored, monkeypatch):
    store, art = stored
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.settings = SimpleNamespace(auth_mode="jwt")

    async def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated request queried object existence")

    monkeypatch.setattr(ArtifactStore, "get", forbidden)
    assert TestClient(app).get(f"/api/v1/files/{art.id}/representations/raw").status_code == 401


async def test_raw_s3_uses_range_and_closes_body(stored):
    _, art = stored
    remaining = RAW_LIMIT + 4
    reads = []

    def read(n):
        nonlocal remaining
        reads.append(n)
        part = b"a" * min(n, remaining)
        remaining -= len(part)
        return part

    body = Mock(read=read)
    client = Mock()
    client.get_object.return_value = {"Body": body}
    backend = S3Backend(endpoint_url="http://storage.invalid", bucket="test", access_key="test", secret_key="test", _client=client)
    store = ArtifactStore(db_path="unused", backend=backend)
    raw = await build_representation(store, replace(art, size=100_000_000), "raw")
    assert raw.truncated and len(raw.text) == RAW_LIMIT
    assert sum(reads) == RAW_LIMIT + 4
    client.get_object.assert_called_once_with(Bucket="test", Key=art.path, Range=f"bytes=0-{RAW_LIMIT + 3}")
    body.close.assert_called_once()


async def test_metadata_revision_never_depends_on_blob_reads(stored, monkeypatch):
    store, art = stored

    def forbidden(*args, **kwargs):
        pytest.fail("Details touched blob storage")

    monkeypatch.setattr(ArtifactStore, "iter_read_range", forbidden)
    monkeypatch.setattr(ArtifactStore, "open_read", forbidden)
    monkeypatch.setattr(LocalBackend, "head", forbidden)
    old = await build_representation(store, art, "details")
    new = await build_representation(store, replace(art, filename="renamed.csv"), "details")
    assert old.revision != new.revision
    changed_trust = await build_representation(store, replace(art, trust="unverified"), "details")
    assert old.revision != changed_trust.revision


async def test_details_metadata_preserves_evaluated_export_trust_without_reading_blobs(stored, monkeypatch):
    store, _ = stored
    await SessionStore(store.db_path).initialize()
    art = await store.put_bytes(content=b"%PDF-fixture", filename="report.pdf", mime="application/pdf", source="canvas_export", created_by="owner")
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.settings = SimpleNamespace(dev_default_user="owner", auth_mode="dev")
    client = TestClient(app)
    listing = client.get("/api/v1/files/?source=all")
    assert listing.status_code == 200
    listed = next(item for item in listing.json()["items"] if item["file_id"] == art.id)
    assert art.trust == "trusted"
    assert listed["trust"] == "export_copy_legacy_source_missing"

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata must not parse export outputs or manifests to infer verification")

    monkeypatch.setattr(ArtifactStore, "open_read", forbidden)
    monkeypatch.setattr(ArtifactStore, "iter_read_range", forbidden)
    monkeypatch.setattr(LocalBackend, "head", forbidden)
    details = client.get(f"/api/v1/files/{art.id}/representations/details")
    assert details.status_code == 200
    assert details.headers["cache-control"] == "no-store"
    metadata = details.json()["details"]
    assert metadata["id"] == art.id and metadata["filename"] == listed["filename"]
    # Safe enrichment must work even for a client that merges metadata over its
    # evaluated catalog object. Storage 'trusted' is not export verification.
    assert {**listed, **metadata}["trust"] == listed["trust"]
