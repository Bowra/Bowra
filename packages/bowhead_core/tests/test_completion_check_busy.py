"""A busy SQLite lock during the completion's authority check is never a verdict about the sources.

A reproduced task finished its report, then the completion check met a busy authority
lease; the skill source revalidation reported it as ``reference_skill_source_unavailable`` and the
finished Turn failed. A busy error now passes through and the pure-read check repeats.
"""

from __future__ import annotations

import sqlite3

import pytest
from bowhead_core.session_store import verify_publication_authority
from bowhead_core.skill_read_resolution import SkillReadResolutionError

from bowhead_core import skill_read_resolution


def _busy() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked: SQLite authority lease is busy; retry the complete transaction")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def _resolve(raising: BaseException) -> None:
    try:
        raise raising
    except Exception:
        skill_read_resolution._fail()


def test_a_busy_lock_is_not_an_unavailable_skill_source() -> None:
    with pytest.raises(sqlite3.OperationalError, match="authority lease is busy"):
        _resolve(_busy())
    # Anything else still becomes the finite source error.
    with pytest.raises(SkillReadResolutionError, match="reference_skill_source_unavailable"):
        _resolve(sqlite3.OperationalError("no such table: skills"))
    with pytest.raises(SkillReadResolutionError, match="reference_skill_source_unavailable"):
        _resolve(KeyError("path"))


class _Publication:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.calls = 0

    async def verify_authority(self, answer: str) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return f"verified:{answer}"


async def test_the_completion_check_repeats_while_a_lock_is_busy() -> None:
    publication = _Publication([_busy(), _busy()])
    assert await verify_publication_authority(publication, "answer") == "verified:answer"  # type: ignore[arg-type]
    assert publication.calls == 3


async def test_the_completion_check_does_not_repeat_a_verdict() -> None:
    publication = _Publication([SkillReadResolutionError("reference_skill_source_unavailable")])
    with pytest.raises(SkillReadResolutionError):
        await verify_publication_authority(publication, "answer")  # type: ignore[arg-type]
    assert publication.calls == 1
