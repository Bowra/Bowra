"""Run-scoped TeX upload admission and exact retained-source downloads."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import ArtifactStore, LocalBackend, artifact_fetch_producer_output, mint_artifact_fetch_token
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes import files
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import migrations

pytestmark = pytest.mark.integration
_SECRET = "latex-output-scope-test-secret-000000000000"
_SCOPE = {"session_id": "s_tex", "turn_id": "t_tex", "workspace_id": "ws_tex"}
_SOURCE = "\\documentclass{article}\n\\begin{document}中文\\end{document}".encode()


@pytest.fixture
async def client_store(tmp_path):
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "artifacts"), db_path=tmp_path / "files.db")
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{store.db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    await store.initialize()
    sessions = SessionStore(store.db_path)
    await sessions.initialize()
    await sessions.ensure_session(session_id="s_tex", workspace_id="ws_tex", user_id="u_owner")
    await sessions.record_turn(turn_id="t_tex", session_id="s_tex", user_text="Create a LaTeX report")
    await sessions.ensure_session(session_id="s_other", workspace_id="ws_tex", user_id="u_other")
    await sessions.record_turn(turn_id="t_other", session_id="s_other", user_text="Other task")
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.session_store = sessions
    app.state.artifact_fetch_secret = _SECRET
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="u_owner")
    return TestClient(app), store, sessions


def _upload(client, *, scope=_SCOPE, user="u_owner", mime="application/x-tex", content=_SOURCE, params=None, extra_headers=None):
    token = mint_artifact_fetch_token(_SECRET, user, producer_output=scope)
    return client.post(
        "/api/v1/files/upload",
        params=params or {"source": "python_sandbox", "session_id": "s_tex", "created_by_turn": "t_tex", "kind": "deliverable"},
        headers={"Authorization": "Bearer " + token, **(extra_headers or {})},
        files={"file": ("main.tex", content, mime)},
    )


@pytest.mark.parametrize("mime", ["application/x-tex", "text/x-tex", "application/octet-stream", "text/plain; charset=utf-8"])
async def test_scoped_tex_output_retains_exact_bytes_and_session_membership(client_store, mime):
    client, store, _ = client_store
    response = _upload(client, mime=mime)
    assert response.status_code == 200, response.text
    artifact = await store.get(response.json()["file_id"])
    assert artifact.mime == "text/x-tex"
    assert artifact.filename == "main.tex"
    assert artifact.created_by == "u_owner"
    assert artifact.created_by_turn == "t_tex"
    assert artifact.kind == "deliverable"
    download = client.get(f"/api/v1/files/{artifact.id}", headers={"Authorization": "Bearer " + mint_artifact_fetch_token(_SECRET, "u_owner")})
    assert download.status_code == 200, download.text
    assert download.content == _SOURCE
    assert artifact.id in {a.id for a in await store.list_for_session(session_id="s_tex", user_id="u_owner")}


@pytest.mark.parametrize(
    "case",
    [
        "browser",
        "legacy_token",
        "wrong_user",
        "wrong_session",
        "wrong_turn",
        "wrong_space",
        "ordinary_source",
        "header_override",
        "binary",
        "invalid_utf8",
        "wrong_mime",
    ],
)
async def test_unproven_or_binary_source_is_rejected_before_storage(client_store, case):
    client, store, _ = client_store
    kwargs = {}
    if case == "browser":
        kwargs["extra_headers"] = {"Authorization": ""}
    elif case == "legacy_token":
        kwargs["scope"] = None
    elif case == "wrong_user":
        kwargs["user"] = "u_other"
    elif case in {"wrong_session", "wrong_turn", "wrong_space"}:
        key = {"wrong_session": "session_id", "wrong_turn": "turn_id", "wrong_space": "workspace_id"}[case]
        kwargs["scope"] = {**_SCOPE, key: "other"}
    elif case == "ordinary_source":
        kwargs["params"] = {"source": "upload", "session_id": "s_tex", "created_by_turn": "t_tex"}
    elif case == "header_override":
        kwargs["extra_headers"] = {"X-Bowhead-Upload-Source": "upload"}
    elif case in {"binary", "invalid_utf8"}:
        kwargs["content"] = b"\x00text" if case == "binary" else b"\xff"
    elif case == "wrong_mime":
        kwargs["mime"] = "application/pdf"
    response = _upload(client, **kwargs)
    assert response.status_code == 415, response.text
    assert not await store.list_for_session(session_id="s_tex", user_id="u_owner")


async def test_finished_turn_cannot_upload_more_source(client_store):
    client, _, sessions = client_store
    await sessions.complete_turn(turn_id="t_tex", intent=None, final_answer="Done", tokens_input=0, tokens_output=0, cost_usd=0)
    assert _upload(client).status_code == 415


def test_token_scope_requires_signature_and_expiry():
    token = mint_artifact_fetch_token(_SECRET, "u_owner", producer_output=_SCOPE)
    assert artifact_fetch_producer_output(_SECRET, token) == _SCOPE
    assert artifact_fetch_producer_output("wrong-secret-with-32-characters-000", token) is None
    assert artifact_fetch_producer_output(_SECRET, mint_artifact_fetch_token(_SECRET, "u_owner", ttl_sec=-1, producer_output=_SCOPE)) is None
    assert artifact_fetch_producer_output(_SECRET, mint_artifact_fetch_token(_SECRET, "u_owner")) is None


def test_turn_minter_binds_source_to_current_execution():
    from bowhead_core.turn_runner import _build_artifact_token_minter

    ctx = SimpleNamespace(**_SCOPE)
    minter = _build_artifact_token_minter(SimpleNamespace(artifact_fetch_secret=_SECRET), "u_owner", ctx=ctx)
    assert artifact_fetch_producer_output(_SECRET, minter()) == _SCOPE
