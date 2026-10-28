"""Space instructions: administrator-written guidance for every conversation in a Space.

Space administrators maintain one free-form text per Space. Every save appends an immutable
version; the highest version is current, and an empty text clears the instructions without
discarding history. Content is deliberately unrestricted: administrators decide what belongs
there, so this module never filters topics or wording.

Each Turn reads the current version **once** at turn start (`snapshot_for_turn`). Lead and every
executor receive the same rendered bytes, and the Turn records which version it received in
`turn_space_instructions`, so live injection, the persisted record and a later replay agree.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bowhead_core._db import open_shared_ctx

logger = logging.getLogger(__name__)

#: Maximum characters per version. Saving longer text is rejected with the limit and the actual
#: length; injection never truncates, because a silently shortened text would mislead the model.
MAX_CONTENT_CHARS = 6000

_HEADER = "<space_instructions>"
_FOOTER = "</space_instructions>"
_INTRO = (
    "The administrators of this Space wrote these instructions for every conversation in it. "
    "Apply them wherever they are relevant to your work. When the user's request in this "
    "conversation or the user's remembered personal preferences say otherwise, follow the user."
)


class SpaceInstructionsError(Exception):
    """A rejected write; `code` is the stable API error code."""

    def __init__(self, code: str, **details: int) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class SpaceInstructionsVersion:
    workspace_id: str
    version: int
    content: str
    created_by: str
    created_at: str
    restored_from: int | None = None
    #: The adopted suggestion inbox candidate whose wording this save added, if any.
    source_candidate_id: str | None = None


@dataclass(frozen=True)
class SpaceInstructionsSnapshot:
    """The instructions one Turn received; `block` is the exact prompt text."""

    workspace_id: str
    version: int
    block: str


def _normalize(content: str) -> str:
    """Trim like the browser's String.trim(), which also removes a byte-order mark."""
    return content.replace("\ufeff", "").strip()


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _row(row: tuple) -> SpaceInstructionsVersion:
    return SpaceInstructionsVersion(
        workspace_id=row[0],
        version=int(row[1]),
        content=row[2],
        created_by=row[3],
        created_at=row[4],
        restored_from=None if row[5] is None else int(row[5]),
        source_candidate_id=row[6],
    )


def render_block(content: str) -> str:
    """Render the prompt block, or "" for empty content.

    Only this block's own delimiters are neutralized so the text cannot close the block early;
    the content itself is passed through unchanged.
    """
    text = content.strip()
    if not text:
        return ""
    # Full-width brackets keep the words readable while no longer forming the delimiter.
    text = re.sub(r"<(\s*/?\s*)space_instructions(\s*)>", r"＜\1space_instructions\2＞", text, flags=re.IGNORECASE)
    return "\n".join([_HEADER, _INTRO, "", text, _FOOTER])


class SpaceInstructionsStore:
    """Versioned Space instructions in the core database (tables owned by migrations 0178 and 0181)."""

    _COLUMNS = "workspace_id, version, content, created_by, created_at, restored_from, source_candidate_id"

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def current(self, workspace_id: str) -> SpaceInstructionsVersion | None:
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                f"SELECT {self._COLUMNS} FROM space_instruction_versions WHERE workspace_id=? ORDER BY version DESC LIMIT 1",
                (workspace_id,),
            )
            row = await cur.fetchone()
        return _row(tuple(row)) if row else None

    async def get_version(self, workspace_id: str, version: int) -> SpaceInstructionsVersion | None:
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                f"SELECT {self._COLUMNS} FROM space_instruction_versions WHERE workspace_id=? AND version=?",
                (workspace_id, version),
            )
            row = await cur.fetchone()
        return _row(tuple(row)) if row else None

    async def list_versions(self, workspace_id: str, *, limit: int = 50) -> list[SpaceInstructionsVersion]:
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                f"SELECT {self._COLUMNS} FROM space_instruction_versions WHERE workspace_id=? ORDER BY version DESC LIMIT ?",
                (workspace_id, limit),
            )
            rows = await cur.fetchall()
        return [_row(tuple(row)) for row in rows]

    async def save(
        self,
        *,
        workspace_id: str,
        content: str,
        created_by: str,
        expected_version: int,
        restored_from: int | None = None,
        source_candidate_id: str | None = None,
    ) -> SpaceInstructionsVersion:
        """Append a version. `expected_version` is the current version the editor started from (0 = none).

        Raises `SpaceInstructionsError` with `too_long`, `stale_version`, `unchanged` or
        `restore_source_missing`.
        """
        text = _normalize(content)
        if len(text) > MAX_CONTENT_CHARS:
            raise SpaceInstructionsError("too_long", length=len(text), limit=MAX_CONTENT_CHARS)
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT version, content FROM space_instruction_versions WHERE workspace_id=? ORDER BY version DESC LIMIT 1",
                    (workspace_id,),
                )
                latest = await cur.fetchone()
                current_version = int(latest[0]) if latest else 0
                if current_version != expected_version:
                    raise SpaceInstructionsError("stale_version", current_version=current_version)
                if (latest[1] if latest else "") == text:
                    raise SpaceInstructionsError("unchanged", current_version=current_version)
                if restored_from is not None:
                    cur = await db.execute(
                        "SELECT content FROM space_instruction_versions WHERE workspace_id=? AND version=?",
                        (workspace_id, restored_from),
                    )
                    source = await cur.fetchone()
                    if source is None:
                        raise SpaceInstructionsError("restore_source_missing", version=restored_from)
                    if source[0] != text:
                        # Edited after restoring: an ordinary new version, so history never claims otherwise.
                        restored_from = None
                saved = SpaceInstructionsVersion(
                    workspace_id=workspace_id,
                    version=current_version + 1,
                    content=text,
                    created_by=created_by,
                    created_at=_now(),
                    restored_from=restored_from,
                    source_candidate_id=source_candidate_id,
                )
                await db.execute(
                    f"INSERT INTO space_instruction_versions ({self._COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        saved.workspace_id,
                        saved.version,
                        saved.content,
                        saved.created_by,
                        saved.created_at,
                        saved.restored_from,
                        saved.source_candidate_id,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return saved

    async def versions_from_candidate(self, candidate_id: str) -> dict[str, SpaceInstructionsVersion]:
        """The latest version per Space that added this suggestion's wording."""
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                f"SELECT {self._COLUMNS} FROM space_instruction_versions WHERE source_candidate_id=? ORDER BY version",
                (candidate_id,),
            )
            rows = await cur.fetchall()
        return {item.workspace_id: item for item in (_row(tuple(row)) for row in rows)}

    async def current_versions(self, workspace_ids: list[str]) -> dict[str, int]:
        """Current version number per Space; Spaces without instructions are absent."""
        if not workspace_ids:
            return {}
        marks = ",".join("?" * len(workspace_ids))
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                f"SELECT workspace_id, MAX(version) FROM space_instruction_versions WHERE workspace_id IN ({marks}) GROUP BY workspace_id",
                tuple(workspace_ids),
            )
            rows = await cur.fetchall()
        return {row[0]: int(row[1]) for row in rows}

    async def snapshot_for_turn(self, *, turn_id: str, workspace_id: str) -> SpaceInstructionsSnapshot | None:
        """Read the current version once for a Turn and record which version it received.

        Returns None when the Space has no non-empty instructions; nothing is recorded then.
        """
        if not workspace_id:
            return None
        # A resumed, steered or continued Turn keeps its turn_id and runs this again. It must keep the
        # version its first execution received: Lead resumes from a checkpointed prompt holding that
        # block, so reading the current version here would split Lead and executors.
        recorded = await self.block_for_turn(turn_id, workspace_id=workspace_id)
        if recorded is not None:
            return recorded
        current = await self.current(workspace_id)
        if current is None or not current.content.strip():
            return None
        try:
            async with open_shared_ctx(self._db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO turn_space_instructions (turn_id, workspace_id, version, created_at) VALUES (?, ?, ?, ?)",
                    (turn_id, workspace_id, current.version, _now()),
                )
                await db.commit()
        except Exception as exc:
            # The answer still follows the instructions; only the audit record is missing.
            logger.warning("space_instructions.record_failed turn=%s ws=%s err=%s", turn_id, workspace_id, exc)
        return SpaceInstructionsSnapshot(workspace_id=workspace_id, version=current.version, block=render_block(current.content))

    async def block_for_turn(self, turn_id: str, *, workspace_id: str | None = None) -> SpaceInstructionsSnapshot | None:
        """The snapshot a Turn recorded, rebuilt from its immutable version; None if it recorded none."""
        recorded = await self.version_for_turn(turn_id)
        if recorded is None or (workspace_id is not None and recorded[0] != workspace_id):
            return None
        version = await self.get_version(*recorded)
        if version is None or not version.content.strip():
            return None
        return SpaceInstructionsSnapshot(workspace_id=version.workspace_id, version=version.version, block=render_block(version.content))

    async def version_for_turn(self, turn_id: str) -> tuple[str, int] | None:
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "SELECT workspace_id, version FROM turn_space_instructions WHERE turn_id=?",
                (turn_id,),
            )
            row = await cur.fetchone()
        return (row[0], int(row[1])) if row else None


__all__ = [
    "MAX_CONTENT_CHARS",
    "SpaceInstructionsError",
    "SpaceInstructionsSnapshot",
    "SpaceInstructionsStore",
    "SpaceInstructionsVersion",
    "render_block",
]
