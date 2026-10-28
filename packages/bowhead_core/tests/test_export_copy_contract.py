from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_export import init_export_jobs
from bowhead_core.export_copy_contract import (
    CURRENT_MANIFEST_PRODUCER,
    EXPORT_COPY_CONTRACT_VERSION,
    TRUST_COPY_CONTRACT,
    classify_export_artifact_versions,
    classify_export_copy_versions,
    evict_export_copy_manifest,
    output_producer_for,
)


@pytest.fixture
async def artifact_store(tmp_path) -> ArtifactStore:
    store = ArtifactStore(
        db_path=str(tmp_path / "copy-contract.db"),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await store.initialize()
    evict_export_copy_manifest()
    return store


async def _job_with_pair(
    artifact_store: ArtifactStore,
    *,
    job_id: str = "exp_current",
    manifest_payload: object | None = None,
    manifest_content: bytes | None = None,
    output_producer: str | None = None,
    manifest_producer: str = CURRENT_MANIFEST_PRODUCER,
    manifest_mime: str = "application/json",
    manifest_filename: str = "report.manifest.json",
    output_mime: str = "application/pdf",
    output_filename: str = "report.pdf",
    link_output_to_manifest: bool = True,
    contract_version: int = 2,
):
    payload = (
        manifest_payload
        if manifest_payload is not None
        else {
            "manifest_version": 2,
            "trust_copy_contract": TRUST_COPY_CONTRACT,
            "job_id": job_id,
            "format": "pdf",
        }
    )
    manifest = await artifact_store.put_bytes(
        created_by=None,
        content=manifest_content if manifest_content is not None else json.dumps(payload).encode(),
        filename=manifest_filename,
        mime=manifest_mime,
        source="canvas_export",
        producer_kind=manifest_producer,
    )
    output = await artifact_store.put_bytes(
        created_by=None,
        content=b"%PDF-1.7\n",
        filename=output_filename,
        mime=output_mime,
        source="canvas_export",
        producer_kind=output_producer or output_producer_for(engine="durable", fmt="pdf"),
        manifest_artifact_id=manifest.id if link_output_to_manifest else None,
    )
    return SimpleNamespace(
        id=job_id,
        fmt="pdf",
        status="done",
        contract_version=contract_version,
        artifact_id=output.id,
        manifest_artifact_id=manifest.id,
    )


@pytest.mark.asyncio
async def test_classifier_requires_complete_positive_evidence(artifact_store: ArtifactStore) -> None:
    job = await _job_with_pair(artifact_store)
    versions = await classify_export_copy_versions([job], artifact_store=artifact_store)
    assert versions[job.id] == EXPORT_COPY_CONTRACT_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"manifest_payload": {"trust_copy_contract": TRUST_COPY_CONTRACT, "job_id": "other", "format": "pdf"}},
        {"manifest_producer": "canvas_export_manifest_b1a_v1_suffix"},
        {"output_producer": "canvas_export_b1a_v1_pdf_v2_suffix"},
        {"output_producer": output_producer_for(engine="legacy", fmt="pdf")},
        {"manifest_mime": "text/plain"},
        {"manifest_filename": "report.json"},
        {"output_mime": "text/plain"},
        {"output_filename": "report.txt"},
        {"link_output_to_manifest": False},
        {"manifest_content": b"{not-json"},
        {"manifest_payload": {}},
        {"manifest_payload": []},
    ],
)
async def test_classifier_fail_closes_malformed_or_prefix_evidence(
    artifact_store: ArtifactStore,
    mutation: dict,
) -> None:
    job = await _job_with_pair(artifact_store, **mutation)
    versions = await classify_export_copy_versions([job], artifact_store=artifact_store)
    assert versions[job.id] == 2


@pytest.mark.asyncio
async def test_classifier_requires_job_to_point_to_the_same_manifest(artifact_store: ArtifactStore) -> None:
    job = await _job_with_pair(artifact_store)
    job.manifest_artifact_id = "art_missing_manifest"
    versions = await classify_export_copy_versions([job], artifact_store=artifact_store)
    assert versions[job.id] == 2


@pytest.mark.asyncio
async def test_classifier_uses_engine_specific_output_producer(artifact_store: ArtifactStore) -> None:
    job = await _job_with_pair(
        artifact_store,
        output_producer=output_producer_for(engine="legacy", fmt="pdf"),
        contract_version=1,
    )
    versions = await classify_export_copy_versions([job], artifact_store=artifact_store)
    assert versions[job.id] == EXPORT_COPY_CONTRACT_VERSION


@pytest.mark.asyncio
async def test_classifier_never_upgrades_running_job(artifact_store: ArtifactStore) -> None:
    job = await _job_with_pair(artifact_store)
    job.status = "running"
    versions = await classify_export_copy_versions([job], artifact_store=artifact_store)
    assert versions[job.id] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_engine", [True, "2", 3, None])
async def test_classifier_fails_closed_for_non_exact_engine_version(
    artifact_store: ArtifactStore,
    malformed_engine: object,
) -> None:
    job = await _job_with_pair(artifact_store)
    job.contract_version = malformed_engine

    versions = await classify_export_copy_versions([job], artifact_store=artifact_store)

    assert versions[job.id] == 1


@pytest.mark.asyncio
async def test_classifier_fails_closed_on_metadata_or_blob_io_error(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _job_with_pair(artifact_store)

    async def fail_metadata(_artifact_ids: list[str]):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(artifact_store, "get_many", fail_metadata)
    assert (await classify_export_copy_versions([job], artifact_store=artifact_store))[job.id] == 2

    monkeypatch.undo()

    async def fail_blob(_artifact):
        raise RuntimeError("blob unavailable")

    monkeypatch.setattr(artifact_store, "open_read", fail_blob)
    assert (await classify_export_copy_versions([job], artifact_store=artifact_store))[job.id] == 2


@pytest.mark.asyncio
async def test_artifact_classifier_fails_closed_when_multiple_jobs_claim_output(
    artifact_store: ArtifactStore,
) -> None:
    job = await _job_with_pair(artifact_store)
    output = (await artifact_store.get_many([job.artifact_id]))[0]
    await init_export_jobs(artifact_store.db_path)
    async with open_shared_ctx(artifact_store.db_path) as db:
        for job_id in (job.id, "exp_duplicate"):
            await db.execute(
                "INSERT INTO canvas_export_jobs "
                "(id,canvas_id,fmt,status,contract_version,artifact_id,manifest_artifact_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    "cv_duplicate",
                    job.fmt,
                    job.status,
                    job.contract_version,
                    job.artifact_id,
                    job.manifest_artifact_id,
                    "2026-08-01T00:00:00Z",
                    "2026-08-01T00:00:00Z",
                ),
            )
        await db.commit()

    assert (
        await classify_export_artifact_versions(
            [output],
            artifact_store=artifact_store,
        )
        == {}
    )
