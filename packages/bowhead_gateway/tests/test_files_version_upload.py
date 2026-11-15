"""A sandbox output declared as the next version of a file (file-versions design W1).

Only a running Turn's own deliverable output, carrying its producer-output token, may name
`version_of`; the store checks the target's scope and keeps history linear.
"""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import ArtifactStore, LocalBackend, mint_artifact_fetch_token
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_gateway.routes import files
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import migrations

pytestmark = pytest.mark.integration
_SECRET = "file-version-scope-test-secret-00000000000"
_SCOPE = {"session_id": "s_v", "turn_id": "t_v", "workspace_id": "ws_v"}
_PARAMS = {"source": "python_sandbox", "session_id": "s_v", "created_by_turn": "t_v", "kind": "deliverable"}


@pytest.fixture
async def client_store(tmp_path):
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "artifacts"), db_path=tmp_path / "files.db")
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{store.db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, SOURCE_REVISION)
    await store.initialize()
    # The content domain after 0228 (the legacy single-file schema itself stays frozen).
    contract = import_module("bowhead_core.migrations.versions.0228_artifact_versions")
    async with aiosqlite.connect(store.db_path) as db:
        for column, declaration in contract.COLUMNS:
            await db.execute(f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}")
        for statement in (contract.LINEAGE_INDEX, contract.VERSION_OF_UNIQUE, contract.VERSION_IMMUTABLE):
            await db.execute(statement)
        await db.commit()
    sessions = SessionStore(store.db_path)
    await sessions.initialize()
    await sessions.ensure_session(session_id="s_v", workspace_id="ws_v", user_id="u_owner")
    await sessions.record_turn(turn_id="t_v", session_id="s_v", user_text="Fix the report charts")
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.session_store = sessions
    app.state.artifact_fetch_secret = _SECRET
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="u_owner")
    return TestClient(app), store


def _upload(client, *, version_of=None, token=True, params=None, content=None):
    headers = {"Authorization": "Bearer " + mint_artifact_fetch_token(_SECRET, "u_owner", producer_output=_SCOPE)} if token else {}
    query = {**(params or _PARAMS), **({"version_of": version_of} if version_of else {})}
    # Each upload is a distinct revision: the same run re-saving identical bytes keeps its version (dogfood M3).
    body = content if content is not None else f"<html>{version_of or 'first'}</html>".encode()
    return client.post("/api/v1/files/upload", params=query, headers=headers, files={"file": ("report.html", body, "text/html")})


async def test_a_run_output_records_the_next_version(client_store):
    client, store = client_store
    first = _upload(client)
    assert first.status_code == 200, first.text
    second = _upload(client, version_of=first.json()["file_id"])
    assert second.status_code == 200, second.text
    stored = await store.get(second.json()["file_id"])
    assert (stored.version_of, stored.lineage_id, stored.version_no) == (first.json()["file_id"], first.json()["file_id"], 2)

    stale = _upload(client, version_of=first.json()["file_id"], content=b"<html>stale</html>")
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == {"code": "version_target_not_latest", "latest_id": second.json()["file_id"]}

    # The run saving the same bytes again keeps its version instead of adding a duplicate.
    again = _upload(client, version_of=second.json()["file_id"], content=f"<html>{first.json()['file_id']}</html>".encode())
    assert again.status_code == 200 and again.json()["file_id"] == second.json()["file_id"]


@pytest.mark.parametrize("case", ["browser", "analysis_output"])
async def test_only_a_run_deliverable_may_declare_a_version(client_store, case):
    client, _ = client_store
    first = _upload(client)
    assert first.status_code == 200, first.text
    if case == "browser":
        response = _upload(client, version_of=first.json()["file_id"], token=False)
    else:
        response = _upload(client, version_of=first.json()["file_id"], params={**_PARAMS, "kind": "tool_data"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "invalid_file_upload_request"


async def test_deleting_a_file_used_by_a_document_names_the_version(client_store, monkeypatch):
    """A2: a document reference to any version refuses deleting the file with an actionable 409, not a 500.

    The store-side check over every version is covered in bowhead_core test_artifact_versions.py.
    """
    from bowhead_core.documents.retention import DocumentArtifactInUse

    client, store = client_store
    first = _upload(client).json()["file_id"]
    second = _upload(client, version_of=first).json()["file_id"]

    async def in_use(artifact_id, *, user_id):
        error = DocumentArtifactInUse()
        error.artifact_id = first
        raise error

    monkeypatch.setattr(store, "soft_delete", in_use)
    response = client.delete(f"/api/v1/files/{second}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == {"code": "file_in_document", "details": {"artifact_id": first, "version_no": 1}}


async def test_a_new_version_schedules_pruning_of_its_file(client_store, monkeypatch):
    """B4: pruning runs after the new version committed, for that file only."""
    from bowhead_gateway import file_versions_pruning

    client, _ = client_store
    scheduled: list[str] = []
    monkeypatch.setattr(file_versions_pruning, "schedule_file_version_prune", lambda app, root: scheduled.append(root))
    first = _upload(client).json()["file_id"]
    assert scheduled == [], "A first version has nothing to prune"
    assert _upload(client, version_of=first).status_code == 200
    assert scheduled == [first]
