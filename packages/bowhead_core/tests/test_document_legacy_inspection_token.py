"""Compatibility for unexpired inspection tokens and guarded artifact writes."""

import time
from unittest.mock import AsyncMock

import jwt
import pytest
from bowhead_core.artifact_store import artifact_fetch_document_inspection, mint_artifact_fetch_token


def legacy_inspection_token(secret: str, user_id: str, *, scope: dict) -> str:
    """Reproduce the historical signed claim without a production writer."""
    return jwt.encode(
        {"aud": "artifact-fetch", "sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 600, "document_inspection": scope},
        secret,
        algorithm="HS256",
    )


def test_legacy_inspection_token_preserves_its_signed_identity():
    scope = {
        "team_id": "team_route",
        "user_id": "usr_route",
        "workspace_id": "ws_route",
        "session_id": "s_route",
        "turn_id": "t_route",
        "execution_generation": 1,
        "execution_owner_id": "worker_route",
    }
    secret = "test-only-document-routing-secret-" * 2
    assert artifact_fetch_document_inspection(secret, mint_artifact_fetch_token(secret, "usr_route")) is None
    token = legacy_inspection_token(secret, "usr_route", scope=scope)
    assert artifact_fetch_document_inspection(secret, token) == scope
    with pytest.raises(ValueError, match="document_inspection_scope_invalid"):
        artifact_fetch_document_inspection(secret, legacy_inspection_token(secret, "usr_other", scope=scope))


@pytest.mark.asyncio
async def test_rejected_guarded_upload_never_writes_a_blob_or_artifact(tmp_path):
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend

    backend = LocalBackend(tmp_path / "blobs")
    store = ArtifactStore(str(tmp_path / "store.db"), backend)
    await store.initialize()
    backend.put = AsyncMock(side_effect=AssertionError("Rejected uploads must not reach blob persistence"))

    async def revoked(db):
        assert db.in_transaction
        raise ValueError("worker_revoked")

    with pytest.raises(ValueError, match="worker_revoked"):
        await store.put_bytes(
            content=b"inspection", filename="sandbox-stdout.txt", mime="text/plain", created_by="usr_route", authorize_in_connection=revoked
        )
    backend.put.assert_not_called()
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(store.db_path) as db, db.execute("SELECT COUNT(*) FROM artifacts") as cursor:
        assert (await cursor.fetchone())[0] == 0
