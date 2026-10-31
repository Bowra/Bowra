"""Large continuation storage preserves original bytes and original lifecycle fences."""

import base64
import os
import sqlite3
from copy import deepcopy

import pytest
from bowhead_router.types import ImageRef, Message
from test_subagent_resume_store import TRANSCRIPT, _capture, _db
from test_subagent_resume_store import _fernet as _fernet

from bowhead_core import subagent_resume as sr

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("shape", ["large_tool", "many_messages"])
async def test_segmented_resume_retains_complete_results_and_images(tmp_path, shape, _fernet):
    db_path = await _db(tmp_path)
    transcript = deepcopy(TRANSCRIPT)
    transcript[0].images = [ImageRef(base64="aW1hZ2U=", mime="image/png")]
    if shape == "large_tool":
        transcript[-1].content = base64.b64encode(os.urandom(1200 * 1024)).decode()
    else:
        transcript.extend(Message(role="user" if index % 2 == 0 else "assistant", content=f"Retained note {index}") for index in range(4100))
    identity = await _capture(db_path, messages=transcript, segmented=True)
    checkpoint = await sr.claim(db_path, checkpoint_id=identity, session_id="s_1", user_id="u_1", turn_id="t_2")
    restored = await sr.open_material_async(checkpoint, db_path=db_path)
    assert restored.messages[:-1] == transcript
    with sqlite3.connect(db_path) as db:
        segments = db.execute("SELECT ciphertext FROM continuation_payload_segments WHERE owner_id=?", (identity,)).fetchall()
    assert len(segments) >= (3 if shape == "large_tool" else 1)
    assert all("Retained note" not in row[0] and transcript[-1].content not in row[0] for row in segments)
    await _capture(db_path, segmented=True)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM continuation_payload_segments WHERE owner_id=?", (identity,)).fetchone()[0] == 0


async def test_missing_segment_fails_without_partial_or_cleared_transcript(tmp_path, _fernet):
    db_path = await _db(tmp_path)
    identity = await _capture(db_path, segmented=True)
    checkpoint = await sr.claim(db_path, checkpoint_id=identity, session_id="s_1", user_id="u_1", turn_id="t_2")
    with sqlite3.connect(db_path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE continuation_payload_segments SET ciphertext='changed' WHERE owner_id=?", (identity,))
        db.execute("DELETE FROM continuation_payload_segments WHERE owner_id=?", (identity,))
    with pytest.raises(sr.ResumeRefused) as error:
        await sr.open_material_async(checkpoint, db_path=db_path)
    assert error.value.code == "unreadable"


async def test_payload_parts_cannot_commit_without_the_original_card(tmp_path, _fernet):
    db_path = await _db(tmp_path)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        db.execute(
            "INSERT INTO continuation_payload_segments "
            "(kind,owner_id,ordinal,session_id,turn_id,clarification_id,digest,byte_count,ciphertext) "
            "VALUES ('ordinary','cl_missing',0,'s_1','t_1','cl_missing',?,1,'sealed')",
            ("0" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.commit()
        db.rollback()
        assert db.execute("SELECT count(*) FROM continuation_payload_segments").fetchone()[0] == 0
