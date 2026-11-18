"""Run-scoped JavaScript and CSS upload admission and inert attachment downloads.

Case: on 116 (1.7.49, 2026-11-17) a website folder delivered file by file lost `css/style.css` and
`js/app.js` to 415, and a helper `chk.js` failed a finished game's sandbox call.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import ArtifactStore, LocalBackend, mint_artifact_fetch_token
from bowhead_core.attachments_injector import _extract_one
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_gateway.artifact_representations import build_representation
from bowhead_gateway.routes import files
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import migrations

pytestmark = pytest.mark.integration
_SECRET = "js-css-output-scope-test-secret-00000000000000"
_SCOPE = {"session_id": "s_web", "turn_id": "t_web", "workspace_id": "ws_web"}
_ISOLATION_CSP = "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"
_SOURCES = {
    "app.js": ("text/javascript", "document.title = '专注番茄钟';\nconsole.log('start');\n".encode()),
    "style.css": ("text/css", "body { color: #333; }\n.brand::after { content: '番茄'; }\n".encode()),
}


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
    await sessions.ensure_session(session_id="s_web", workspace_id="ws_web", user_id="u_owner")
    await sessions.record_turn(turn_id="t_web", session_id="s_web", user_text="Build a Pomodoro site")
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.session_store = sessions
    app.state.artifact_fetch_secret = _SECRET
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="u_owner")
    return TestClient(app), store, sessions


def _upload(client, filename, *, mime=None, content=None, scope=_SCOPE, params=None, extra_headers=None):
    stored_mime, body = _SOURCES[filename]
    token = mint_artifact_fetch_token(_SECRET, "u_owner", producer_output=scope)
    return client.post(
        "/api/v1/files/upload",
        params=params or {"source": "python_sandbox", "session_id": "s_web", "created_by_turn": "t_web", "kind": "deliverable"},
        headers={"Authorization": "Bearer " + token, **(extra_headers or {})},
        files={"file": (filename, body if content is None else content, mime or stored_mime)},
    )


@pytest.mark.parametrize(
    ("filename", "declared"),
    [
        ("app.js", "text/javascript"),
        ("app.js", "application/javascript"),
        ("app.js", "application/x-javascript"),
        ("app.js", "text/plain"),
        ("app.js", "application/octet-stream"),
        ("style.css", "text/css"),
        ("style.css", "text/plain"),
        ("style.css", "application/octet-stream"),
    ],
)
async def test_scoped_source_output_is_stored_and_downloaded_as_inert_attachment(client_store, filename, declared):
    client, store, _ = client_store
    stored_mime, body = _SOURCES[filename]
    response = _upload(client, filename, mime=declared)
    assert response.status_code == 200, response.text
    artifact = await store.get(response.json()["file_id"])
    assert (artifact.mime, artifact.filename, artifact.kind, artifact.created_by_turn) == (stored_mime, filename, "deliverable", "t_web")
    # Neither an explicit inline request nor a plain download hands the browser runnable script or styles.
    for query in ("", "?disposition=inline"):
        download = client.get(f"/api/v1/files/{artifact.id}{query}", headers={"X-Bowhead-User-Id": "u_owner"})
        assert download.status_code == 200, download.text
        assert download.content == body
        assert download.headers["content-type"] == "text/plain; charset=utf-8"
        assert download.headers["content-disposition"].startswith("attachment")
        assert filename in download.headers["content-disposition"]
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers["content-security-policy"] == _ISOLATION_CSP
        assert download.headers["referrer-policy"] == "no-referrer"
    # Results shows the source as text; it is never rendered.
    raw = await build_representation(store, artifact, "raw")
    assert raw.mode == "text" and raw.text == body.decode()
    preview = await build_representation(store, artifact, "preview")
    assert preview.mode == "text" and body.decode().splitlines()[0] in (preview.text or "")


@pytest.mark.parametrize("filename", ["app.js", "style.css"])
@pytest.mark.parametrize("case", ["browser", "legacy_token", "ordinary_source", "binary", "invalid_utf8", "empty", "wrong_mime"])
async def test_unproven_or_non_text_source_is_rejected_before_storage(client_store, filename, case):
    client, store, _ = client_store
    kwargs = {}
    if case == "browser":
        kwargs["extra_headers"] = {"Authorization": ""}
    elif case == "legacy_token":
        kwargs["scope"] = None
    elif case == "ordinary_source":
        kwargs["params"] = {"source": "upload", "session_id": "s_web", "created_by_turn": "t_web"}
    elif case == "binary":
        kwargs["content"] = b"body {}\x00"
    elif case == "invalid_utf8":
        kwargs["content"] = b"body { content: '\xff'; }"
    elif case == "empty":
        kwargs["content"] = b"  \n"
    elif case == "wrong_mime":
        kwargs["mime"] = "text/html"
    response = _upload(client, filename, **kwargs)
    assert response.status_code == 415, response.text
    assert response.json()["detail"]["error"]["code"] == "file_type_unsupported"
    assert not await store.list_for_session(session_id="s_web", user_id="u_owner")


async def test_finished_turn_cannot_upload_more_source(client_store):
    client, _, sessions = client_store
    await sessions.complete_turn(turn_id="t_web", intent=None, final_answer="Done", tokens_input=0, tokens_output=0, cost_usd=0)
    assert _upload(client, "app.js").status_code == 415


@pytest.mark.parametrize("filename", ["app.js", "style.css"])
def test_model_reads_source_as_text(filename):
    mime, body = _SOURCES[filename]
    prepared = _extract_one(SimpleNamespace(id="art_src", mime=mime, filename=filename, size=len(body)), body)
    assert prepared.error is None
    assert body.decode().splitlines()[0] in prepared.extracted_text
