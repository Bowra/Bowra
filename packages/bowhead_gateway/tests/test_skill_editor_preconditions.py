"""Original Skill editors cannot overwrite a package changed after their read."""

from __future__ import annotations

import pytest
from bowhead_core.skill_upload import SkillUploadError, bundle_sha256
from test_skill_bundle_write_routes import ADMIN, AUTHOR, _app, _client, _personal_skill, _skill_md, _team_skill

from bowhead_gateway import skill_bundle_files as bundles


@pytest.fixture(autouse=True)
def _database(tmp_path, initialize_migrated_core_database):
    initialize_migrated_core_database(tmp_path / "u.db")


@pytest.mark.parametrize("scope", ["personal", "team"])
@pytest.mark.parametrize("operation", ["body", "file", "create", "rename", "delete"])
def test_original_editor_rejects_stale_package_without_overwriting(scope, operation, tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    if scope == "personal":
        directory = _personal_skill(tmp_path, app)
        client = _client(app, AUTHOR)
        url = "/api/v1/skills/channel-gm-recipe"
    else:
        skill_id, directory = _team_skill(app, ADMIN)
        client = _client(app, ADMIN)
        url = f"/api/v1/team-skills/{skill_id}"
    # The same original writer models another editor or Coach changing a dependency.
    detail = client.get(url)
    assert detail.status_code == 200, detail.text
    baseline = detail.json()["content_sha256"]
    assert baseline == bundle_sha256(directory)
    created = client.post(
        f"{url}/files",
        json={
            "operation": "create",
            "kind": "references",
            "filename": "current.md",
            "content": "Current dependency",
            "expected_content_sha256": baseline,
        },
    )
    assert created.status_code == 200, created.text
    current_hash = bundle_sha256(directory)
    assert created.json()["content_sha256"] == current_hash != baseline

    def send(revision):
        if operation == "body":
            return client.put(f"{url}/body", json={"content": _skill_md(body="New instructions"), "expected_content_sha256": revision})
        if operation == "file":
            return client.put(f"{url}/files/references/current.md", json={"content": "Revised dependency", "expected_content_sha256": revision})
        return client.post(
            f"{url}/files",
            json={
                "operation": operation,
                "kind": "references",
                "filename": "added.md" if operation == "create" else "current.md",
                "target": "renamed.md" if operation == "rename" else None,
                "content": "Added dependency",
                "expected_content_sha256": revision,
            },
        )

    rejected = send(baseline)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "skill_write_conflict"
    assert bundle_sha256(directory) == current_hash
    # An explicit current read permits a newly reviewed edit through the same endpoint.
    fresh = client.get(f"{url}/files/references/current.md")
    assert fresh.json()["content"] == "Current dependency"
    assert fresh.json()["content_sha256"] == current_hash
    saved = send(fresh.json()["content_sha256"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["content_sha256"] == bundle_sha256(directory)


def test_edit_snapshot_holds_writer_lock_while_reading_body_and_hash(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    directory = _personal_skill(tmp_path, app)
    read = bundles.read_bundle_manifest

    def competing_read(path):
        body = read(path)
        with pytest.raises(SkillUploadError, match="skill_write_conflict"):
            bundles.write_bundle_body(path, _skill_md(body="Concurrent edit"), expected_name="channel-gm-recipe")
        return body

    monkeypatch.setattr(bundles, "read_bundle_manifest", competing_read)
    detail = _client(app, AUTHOR).get("/api/v1/skills/channel-gm-recipe")
    assert detail.status_code == 200, detail.text
    assert detail.json()["body_raw"] == (directory / "SKILL.md").read_text()
    assert detail.json()["content_sha256"] == bundle_sha256(directory)


def test_save_acknowledges_its_own_bytes_even_if_another_writer_follows(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    directory = _personal_skill(tmp_path, app)
    write = bundles._write_and_validate
    acknowledged_hash = None

    def followed_by_writer(*args, **kwargs):
        nonlocal acknowledged_hash
        result = write(*args, **kwargs)
        acknowledged_hash = bundle_sha256(directory)
        write(directory, directory / "SKILL.md", _skill_md(body="Later writer"), expected_name="channel-gm-recipe")
        return result

    monkeypatch.setattr(bundles, "_write_and_validate", followed_by_writer)
    content = _skill_md(body="Original request")
    receipt, _ = bundles.write_bundle_body(directory, content, expected_name="channel-gm-recipe", expected_content_sha256=bundle_sha256(directory))
    assert receipt["content"] == content
    assert receipt["content_sha256"] == acknowledged_hash != bundle_sha256(directory)


@pytest.mark.parametrize("operation", ["body", "file", "create", "rename", "delete"])
def test_personal_editor_rejects_recreated_identity_even_with_identical_bytes(operation, tmp_path, monkeypatch):
    import sqlite3

    app = _app(tmp_path, monkeypatch)
    directory = _personal_skill(tmp_path, app)
    client = _client(app, AUTHOR)
    url = "/api/v1/skills/channel-gm-recipe"
    original = client.get(url).json()
    revision = bundle_sha256(directory)
    with sqlite3.connect(app.state.skill_instance_store.db_path) as db:
        db.execute("UPDATE skill_instances SET id='sk_recreated' WHERE id=?", (original["id"],))
    preconditions = {"expected_content_sha256": revision, "expected_instance_id": original["id"]}
    if operation == "body":
        response = client.put(url + "/body", json={"content": _skill_md(body="Old editor content"), **preconditions})
    elif operation == "file":
        response = client.put(url + "/files/references/notes.md", json={"content": "Old editor content", **preconditions})
    else:
        response = client.post(
            url + "/files",
            json={
                "operation": operation,
                "kind": "references",
                "filename": "new.md" if operation == "create" else "notes.md",
                "target": "renamed.md" if operation == "rename" else None,
                "content": "Old editor content",
                **preconditions,
            },
        )
    assert response.status_code == 409, response.text
    assert bundle_sha256(directory) == revision
    current = client.put(
        url + "/body", json={"content": _skill_md(body="Current editor"), "expected_content_sha256": revision, "expected_instance_id": "sk_recreated"}
    )
    assert current.status_code == 200, current.text


def test_personal_identity_is_rechecked_inside_the_shared_package_writer(tmp_path, monkeypatch):
    import sqlite3

    from bowhead_gateway.routes import skills

    app = _app(tmp_path, monkeypatch)
    directory = _personal_skill(tmp_path, app)
    client = _client(app, AUTHOR)
    url = "/api/v1/skills/channel-gm-recipe"
    original = client.get(url).json()
    quota = skills._personal_quota_check

    async def recreate_after_resolution(*args):
        check = await quota(*args)
        with sqlite3.connect(app.state.skill_instance_store.db_path) as db:
            db.execute("UPDATE skill_instances SET id='sk_recreated' WHERE id=?", (original["id"],))
        return check

    monkeypatch.setattr(skills, "_personal_quota_check", recreate_after_resolution)
    response = client.put(
        url + "/body",
        json={"content": _skill_md(body="Old editor"), "expected_content_sha256": original["content_sha256"], "expected_instance_id": original["id"]},
    )
    assert response.status_code == 409, response.text
    assert bundle_sha256(directory) == original["content_sha256"]
