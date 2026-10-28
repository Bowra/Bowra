"""Concurrent Skill writers preserve complete bytes and matching metadata."""

import multiprocessing

import aiosqlite
import pytest
from bowhead_core.skill_bundle_lock import skill_bundle_lock
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_upload import SkillUploadError, bundle_sha256, validate_and_save_generated

pytestmark = pytest.mark.unit


def save(root, body="Original", *, overwrite=False):
    return validate_and_save_generated(
        body_md=f"---\nname: owned-method\ndescription: Test method\nversion: 1.0.0\n---\n{body}\n",
        scripts=None,
        user_id="user_owner",
        dest_root=root,
        existing_user_skills={},
        overwrite=overwrite,
    )


def _competing_writer(root, sender):
    try:
        save(root, "Competing process", overwrite=True)
    except SkillUploadError as exc:
        sender.send(exc.code)
    else:
        sender.send("unexpected_success")
    finally:
        sender.close()


def test_another_process_cannot_replace_a_locked_skill_package(tmp_path):
    original = save(tmp_path)
    original_hash = bundle_sha256(original.source_dir)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    with skill_bundle_lock(original.source_dir):
        process = context.Process(target=_competing_writer, args=(tmp_path, sender))
        process.start()
        try:
            assert receiver.poll(10), "Competing writer must fail without waiting for the lock"
            assert receiver.recv() == "skill_write_conflict"
            process.join(10)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
    assert bundle_sha256(original.source_dir) == original_hash
    assert save(tmp_path, "Next edit", overwrite=True).content_hash != original.content_hash


async def test_delayed_metadata_cannot_overwrite_a_newer_package(tmp_path):
    store = SkillInstanceStore(tmp_path / "skills.db")
    await store.initialize()
    first = save(tmp_path)
    latest = save(tmp_path, "Newer manual edit", overwrite=True)
    args = {"user_id": "user_owner", "name": latest.name, "source_type": "user_generated", "source_path": str(latest.source_dir)}
    await store.upsert(**args, description="Newer metadata", expected_content_hash=latest.content_hash)
    with pytest.raises(SkillUploadError, match="skill_write_conflict"):
        await store.upsert(**args, description="Stale metadata", expected_content_hash=first.content_hash)
    assert (await store.get(user_id="user_owner", name=latest.name)).description == "Newer metadata"
    assert "Newer manual edit" in (latest.source_dir / "SKILL.md").read_text()


async def test_skill_metadata_and_domain_receipt_can_share_one_transaction(tmp_path):
    store = SkillInstanceStore(tmp_path / "skills.db")
    await store.initialize()
    saved = save(tmp_path)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with skill_bundle_lock(saved.source_dir):
            await store.upsert(
                user_id="user_owner",
                name=saved.name,
                source_type="user_generated",
                source_path=str(saved.source_dir),
                expected_content_hash=saved.content_hash,
                connection=db,
            )
            assert db.in_transaction
            await db.rollback()
    assert await store.get(user_id="user_owner", name=saved.name) is None


def test_shared_readers_coexist_and_still_exclude_a_writer(tmp_path):
    package = save(tmp_path).source_dir
    readers = (skill_bundle_lock(package, shared=True), skill_bundle_lock(package, shared=True))
    with readers[0], readers[1], pytest.raises(SkillUploadError, match="skill_write_conflict"), skill_bundle_lock(package):
        raise AssertionError("A writer acquired the package while readers held it")
    with skill_bundle_lock(package), pytest.raises(SkillUploadError, match="skill_write_conflict"), skill_bundle_lock(package, shared=True):
        raise AssertionError("A reader acquired the package while a writer held it")
