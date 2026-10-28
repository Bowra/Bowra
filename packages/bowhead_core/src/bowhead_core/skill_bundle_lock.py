"""Cross-process, fail-fast serialization for a mutable Skill package.

The lock lives beside the bundle so directory replacement cannot replace its
inode. Contention never blocks the event loop or waits while holding SQLite.
Lock files are deliberately retained, including after package deletion.
"""

from __future__ import annotations

import errno
import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def skill_write_conflict() -> Exception:
    from bowhead_core.skill_upload import SkillUploadError

    return SkillUploadError(code="skill_write_conflict")


@contextmanager
def skill_bundle_lock(bundle: Path, *, shared: bool = False) -> Iterator[None]:
    """Hold the bundle lock; ``shared`` readers never conflict with each other, only with a writer."""
    path = Path(bundle).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # All mutation paths use the same server-owned bundle location.
    with (path.parent / f".{path.name}.write-lock").open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise skill_write_conflict() from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
