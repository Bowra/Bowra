"""CCA P3 · S6.1 (F-B) — artifact-fetch token mint/verify + store.get NULL-owner reject.

The python_sandbox host-side runner carries a gateway-signed, artifact-fetch-SCOPED token
on its artifact GET so the download owner-check runs as the REAL caller. The token is signed
with a DEDICATED secret + aud="artifact-fetch" (NOT the main JWT secret → invalid on per-user
routes → genuinely scoped). store.get(reject_null_owner=True) closes the owner-less /
cross-user exfil vector on that scoped path only.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from bowhead_core.artifact_store import (
    ArtifactStore,
    LocalBackend,
    mint_artifact_fetch_token,
    verify_artifact_fetch_token,
)


def _run(coro):
    return asyncio.run(coro)


# ── token mint / verify ───────────────────────────────────────────────────────


def test_mint_verify_roundtrip() -> None:
    tok = mint_artifact_fetch_token("s" * 40, "user_42")
    assert verify_artifact_fetch_token("s" * 40, tok) == "user_42"


def test_verify_wrong_secret_rejected() -> None:
    tok = mint_artifact_fetch_token("secret-A" * 5, "u1")
    assert verify_artifact_fetch_token("secret-B" * 5, tok) is None


def test_verify_expired_rejected() -> None:
    tok = mint_artifact_fetch_token("s" * 40, "u1", ttl_sec=-1)  # already expired
    assert verify_artifact_fetch_token("s" * 40, tok) is None


def test_verify_main_jwt_token_rejected() -> None:
    """A token minted WITHOUT aud=artifact-fetch (e.g. a main-JWT user token shape) must
    NOT verify as an artifact-fetch token — the aud check is the scoping boundary."""
    import jwt

    main_like = jwt.encode({"sub": "u1", "exp": 9999999999}, "s" * 40, algorithm="HS256")
    if isinstance(main_like, bytes):
        main_like = main_like.decode()
    assert verify_artifact_fetch_token("s" * 40, main_like) is None  # no aud=artifact-fetch


def test_verify_garbage_and_empty() -> None:
    assert verify_artifact_fetch_token("s" * 40, "not.a.jwt") is None
    assert verify_artifact_fetch_token("s" * 40, "") is None
    assert verify_artifact_fetch_token("", mint_artifact_fetch_token("s" * 40, "u")) is None


# ── store.get NULL-owner reject (scoped to the runner path) ────────────────────


def _store(tmp: str) -> ArtifactStore:
    s = ArtifactStore(db_path=str(Path(tmp) / "a.db"), backend=LocalBackend(root_dir=Path(tmp) / "artifacts"))
    _run(s.initialize())
    return s


def test_get_reject_null_owner_only_on_scoped_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        # owner-less artifact (created_by=None)
        art = _run(store.put_bytes(content=b"secret-bytes", filename="x.csv", mime="text/csv", source="bi_fetch_rows", created_by=None))
        aid = art.id
        # regular per-user download (reject_null_owner=False, default) → still readable
        # (legit shared/system artifacts keep working)
        got = _run(store.get(aid, check_created_by="anyone"))
        assert got is not None and got.id == aid
        # scoped runner path (reject_null_owner=True) → owner-less artifact DENIED
        denied = _run(store.get(aid, check_created_by="anyone", reject_null_owner=True))
        assert denied is None


def test_get_owned_artifact_matches_owner_on_scoped_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        art = _run(store.put_bytes(content=b"mine", filename="m.csv", mime="text/csv", source="bi_fetch_rows", created_by="user_1"))
        # the real owner (token sub) fetches its own artifact → OK even on scoped path
        assert _run(store.get(art.id, check_created_by="user_1", reject_null_owner=True)) is not None
        # a different user is denied (existing owner-check)
        assert _run(store.get(art.id, check_created_by="user_2", reject_null_owner=True)) is None


# ── turn_runner minter closure (secret stays captured, never on ctx) ───────────


def test_minter_closure_produces_verifiable_token() -> None:
    from types import SimpleNamespace

    from bowhead_core.turn_runner import _build_artifact_token_minter

    orch = SimpleNamespace(artifact_fetch_secret="m" * 40)
    minter = _build_artifact_token_minter(orch, "user_9")
    assert minter is not None
    tok = minter()  # fresh mint per call
    assert verify_artifact_fetch_token("m" * 40, tok) == "user_9"


def test_minter_closure_none_without_secret_or_user() -> None:
    from types import SimpleNamespace

    from bowhead_core.turn_runner import _build_artifact_token_minter

    assert _build_artifact_token_minter(SimpleNamespace(artifact_fetch_secret=None), "u") is None
    assert _build_artifact_token_minter(SimpleNamespace(artifact_fetch_secret="m" * 40), "") is None
    assert _build_artifact_token_minter(SimpleNamespace(), "u") is None  # no attr at all


# ── files.py download route — F-B end-to-end auth path (TestClient) ─────────────

_SECRET = "artifact-fetch-secret-aaaaaaaaaaaaaaaaaaaa"


def _download_app(tmp: str):
    from types import SimpleNamespace

    from bowhead_gateway.routes import files as files_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    store = _store(tmp)
    app = FastAPI()
    app.include_router(files_mod.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.artifact_fetch_secret = _SECRET
    app.state.settings = SimpleNamespace(dev_default_user="dev_default_user")
    app.state.deployment_mode = "saas"
    return TestClient(app), store


def _bearer(user_id: str) -> dict:
    return {"Authorization": f"Bearer {mint_artifact_fetch_token(_SECRET, user_id)}"}


def test_download_runner_token_fetches_own_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, store = _download_app(tmp)
        art = _run(store.put_bytes(content=b"my-rows", filename="r.csv", mime="text/csv", source="bi_fetch_rows", created_by="user_1"))
        r = client.get(f"/api/v1/files/{art.id}", headers=_bearer("user_1"))
        assert r.status_code == 200, r.text
        assert r.content == b"my-rows"


def test_download_runner_token_denied_null_owner() -> None:
    """F-B: an owner-less artifact is NOT fetchable on the scoped runner (token) path."""
    with tempfile.TemporaryDirectory() as tmp:
        client, store = _download_app(tmp)
        art = _run(store.put_bytes(content=b"ownerless", filename="o.csv", mime="text/csv", source="bi_fetch_rows", created_by=None))
        # with the runner token → 404 (reject_null_owner)
        assert client.get(f"/api/v1/files/{art.id}", headers=_bearer("user_1")).status_code == 404
        # WITHOUT a token in saas mode → main auth fail-closed returns 401 before any
        # dev_default_user fallback. Owner-less artifacts remain denied to runner tokens.
        assert client.get(f"/api/v1/files/{art.id}").status_code == 401


def test_download_runner_token_denied_cross_user() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, store = _download_app(tmp)
        art = _run(store.put_bytes(content=b"theirs", filename="t.csv", mime="text/csv", source="bi_fetch_rows", created_by="user_2"))
        # user_1's runner token cannot fetch user_2's artifact
        assert client.get(f"/api/v1/files/{art.id}", headers=_bearer("user_1")).status_code == 404


def test_chained_sandbox_output_input_symmetric_identity() -> None:
    """S6 review P1 regression: a sandbox OUTPUT upload carrying the artifact-fetch Bearer is
    owned by the token sub, so a chained fetch by the SAME sub matches (no 404). Pre-fix the
    upload was owned by dev_default_user → cross-user mismatch → 404 in authenticated prod."""
    with tempfile.TemporaryDirectory() as tmp:
        client, store = _download_app(tmp)
        up = client.post("/api/v1/files/upload", files={"file": ("out.csv", b"a,b\n1,2\n", "text/csv")}, headers=_bearer("user_1"))
        assert up.status_code == 200, up.text
        aid = up.json()["file_id"]
        # the upload is owned by the token sub (user_1), NOT dev_default_user
        art = _run(store.get(aid))
        assert art is not None and art.created_by == "user_1"
        # chained fetch as the SAME user_1 → owner matches → 200 (the regression this closes)
        got = client.get(f"/api/v1/files/{aid}", headers=_bearer("user_1"))
        assert got.status_code == 200, got.text
        assert got.content == b"a,b\n1,2\n"


def test_artifact_fetch_secret_derivation_deterministic_and_separate(monkeypatch) -> None:
    """S6 review: the secret is HMAC-derived from the main JWT secret → DETERMINISTIC across
    workers/containers (mint-on-A verifies on B) yet SEPARATE from the JWT secret."""
    from bowhead_gateway.main import _resolve_artifact_fetch_secret

    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", f"active={'j' * 40},previous={'p' * 40}")
    s1 = _resolve_artifact_fetch_secret()
    s2 = _resolve_artifact_fetch_secret()
    assert s1 == s2  # deterministic → same across workers/containers
    assert s1 != "j" * 40  # separate from the main JWT secret
    assert len(s1) >= 32


def test_download_forged_or_wrong_secret_token_falls_back() -> None:
    """A Bearer signed with the wrong secret is NOT a valid artifact-fetch identity →
    in saas mode the normal auth path fails closed instead of falling back."""
    with tempfile.TemporaryDirectory() as tmp:
        client, store = _download_app(tmp)
        art = _run(store.put_bytes(content=b"x", filename="x.csv", mime="text/csv", source="bi_fetch_rows", created_by="user_1"))
        forged = mint_artifact_fetch_token("WRONG-secret-zzzzzzzzzzzzzzzzzzzzzz", "user_1")
        # forged token → _artifact_fetch_identity None → normal Bearer auth rejects it → 401
        assert client.get(f"/api/v1/files/{art.id}", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
