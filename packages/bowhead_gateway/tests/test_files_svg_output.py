"""Run-scoped SVG upload admission and isolated attachment downloads."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import ArtifactStore, LocalBackend, mint_artifact_fetch_token
from bowhead_core.attachments_injector import _extract_one
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_gateway.routes import files
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import migrations

pytestmark = pytest.mark.integration
_SECRET = "svg-output-scope-test-secret-0000000000000000"
_SCOPE = {"session_id": "s_svg", "turn_id": "t_svg", "workspace_id": "ws_svg"}
_SVG = '<?xml version="1.0" encoding="UTF-8"?>\n<!-- fish -->\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><title>鱼</title><circle r="4"/></svg>\n'.encode()
_ISOLATION_CSP = "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"


@pytest.fixture
async def client_store(tmp_path):
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "artifacts"), db_path=tmp_path / "files.db")
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{store.db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, SOURCE_REVISION)
    await store.initialize()
    sessions = SessionStore(store.db_path)
    await sessions.initialize()
    await sessions.ensure_session(session_id="s_svg", workspace_id="ws_svg", user_id="u_owner")
    await sessions.record_turn(turn_id="t_svg", session_id="s_svg", user_text="Draw a fish as SVG")
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.session_store = sessions
    app.state.artifact_fetch_secret = _SECRET
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="u_owner")
    return TestClient(app), store, sessions


def _upload(client, *, scope=_SCOPE, mime="image/svg+xml", content=_SVG, filename="fish.svg", params=None, extra_headers=None):
    token = mint_artifact_fetch_token(_SECRET, "u_owner", producer_output=scope)
    return client.post(
        "/api/v1/files/upload",
        params=params or {"source": "python_sandbox", "session_id": "s_svg", "created_by_turn": "t_svg", "kind": "deliverable"},
        headers={"Authorization": "Bearer " + token, **(extra_headers or {})},
        files={"file": (filename, content, mime)},
    )


@pytest.mark.parametrize("mime", ["image/svg+xml", "application/octet-stream", "text/plain", "text/xml", "application/xml"])
async def test_scoped_svg_output_is_stored_and_downloaded_as_isolated_attachment(client_store, mime):
    client, store, _ = client_store
    response = _upload(client, mime=mime)
    assert response.status_code == 200, response.text
    artifact = await store.get(response.json()["file_id"])
    assert (artifact.mime, artifact.filename, artifact.kind, artifact.created_by_turn) == ("image/svg+xml", "fish.svg", "deliverable", "t_svg")
    assert artifact.id in {a.id for a in await store.list_for_session(session_id="s_svg", user_id="u_owner")}
    # Neither an explicit inline request nor a plain download renders the SVG on the application origin.
    for query in ("", "?disposition=inline"):
        download = client.get(f"/api/v1/files/{artifact.id}{query}", headers={"X-Bowhead-User-Id": "u_owner"})
        assert download.status_code == 200, download.text
        assert download.content == _SVG
        assert download.headers["content-disposition"].startswith("attachment")
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers["content-security-policy"] == _ISOLATION_CSP


@pytest.mark.parametrize(
    "case",
    ["browser", "legacy_token", "ordinary_source", "html_body", "internal_subset", "binary", "invalid_utf8", "wrong_mime"],
)
async def test_unproven_or_non_svg_source_is_rejected_before_storage(client_store, case):
    client, store, _ = client_store
    kwargs = {}
    if case == "browser":
        kwargs["extra_headers"] = {"Authorization": ""}
    elif case == "legacy_token":
        kwargs["scope"] = None
    elif case == "ordinary_source":
        kwargs["params"] = {"source": "upload", "session_id": "s_svg", "created_by_turn": "t_svg"}
    elif case == "html_body":
        kwargs["content"] = b"<html><body><svg></svg></body></html>"
    elif case == "internal_subset":
        kwargs["content"] = b'<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>'
    elif case in {"binary", "invalid_utf8"}:
        kwargs["content"] = b"<svg>\x00</svg>" if case == "binary" else b"<svg>\xff</svg>"
    elif case == "wrong_mime":
        kwargs["mime"] = "text/html"
    response = _upload(client, **kwargs)
    assert response.status_code == 415, response.text
    assert not await store.list_for_session(session_id="s_svg", user_id="u_owner")


async def test_finished_turn_cannot_upload_more_svg(client_store):
    client, _, sessions = client_store
    await sessions.complete_turn(turn_id="t_svg", intent=None, final_answer="Done", tokens_input=0, tokens_output=0, cost_usd=0)
    assert _upload(client).status_code == 415


def test_model_reads_svg_markup_as_text_source():
    prepared = _extract_one(SimpleNamespace(id="art_svg", mime="image/svg+xml", filename="fish.svg", size=len(_SVG)), _SVG)
    assert prepared.error is None
    assert prepared.image_base64 is None
    assert '<circle r="4"/>' in prepared.extracted_text
