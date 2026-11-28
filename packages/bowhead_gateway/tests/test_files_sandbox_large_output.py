"""Large sandbox files use the existing scoped file upload and download routes."""

import hashlib

import pytest
from bowhead_core.artifact_store import mint_artifact_fetch_token
from bowhead_gateway.routes import files
from test_files_latex_output import _SCOPE, _SECRET
from test_files_latex_output import client_store as client_store


@pytest.mark.integration
async def test_only_current_sandbox_producer_can_save_a_large_csv(client_store, monkeypatch) -> None:
    client, store, _ = client_store
    monkeypatch.setattr(files, "MAX_SIZE", 16)
    monkeypatch.setattr(files, "STANDARD_SANDBOX_OUTPUT_BYTES", 16)
    data = b"id,value\n1,example\n2,example\n"
    monkeypatch.setattr(files, "LARGE_SANDBOX_OUTPUT_BYTES", len(data))
    token = mint_artifact_fetch_token(_SECRET, "u_owner", producer_output=_SCOPE)
    params = {"source": "python_sandbox", "session_id": "s_tex", "created_by_turn": "t_tex", "kind": "deliverable"}

    response = client.post("/api/v1/files/upload", params=params, files={"file": ("result.csv", data, "text/csv")})
    assert response.status_code == 413
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/api/v1/files/upload", params=params, headers=headers, files={"file": ("result.csv", data + b"\n", "text/csv")})
    assert response.status_code == 413
    response = client.post("/api/v1/files/upload", params=params, headers=headers, files={"file": ("result.csv", data, "text/csv")})
    assert response.status_code == 200, response.text
    artifact = await store.get(response.json()["file_id"])
    assert artifact is not None and artifact.kind == "deliverable" and artifact.size == len(data)
    assert artifact.created_by == "u_owner" and artifact.created_by_turn == "t_tex"
    assert await store.hash_artifact(artifact) == hashlib.sha256(data).hexdigest()
    response = client.get(f"/api/v1/files/{artifact.id}", headers=headers)
    assert response.status_code == 200 and response.content == data
