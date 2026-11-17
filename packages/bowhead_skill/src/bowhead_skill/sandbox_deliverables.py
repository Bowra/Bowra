"""Declared sandbox deliverables: top-level files, nested files and folders.

A ``python_exec`` call names what it delivers in ``deliverable_outputs``:

- ``game.html``: a top-level file, saved under that name;
- ``build/report.pdf``: one nested file, saved under its base name;
- ``site/``: a folder and everything below it, saved as one ``site.zip``.

Nothing below the top level is delivered unless it is listed. The tool layer validates
entries here; the stdlib-only sandbox runner repeats the path checks before it reads
anything. Design: docs/sprints/sandbox-nested-deliverables-2026-11-16/tech-design.md §4.1.
"""

from __future__ import annotations

from typing import Any

RESERVED_TOP_DIRS = frozenset({"inputs", "skill"})
SKIPPED_DIR_NAMES = frozenset({"__pycache__"})
MAX_ENTRY_DEPTH = 4
MAX_ENTRY_CHARS = 255

_FORMS = "such as 'game.html', 'build/report.pdf' or 'site/'"


class DeliverableEntryError(ValueError):
    """A ``deliverable_outputs`` entry that cannot be accepted; the message names the entry and the reason."""


def entry_parts(entry: str) -> tuple[tuple[str, ...], bool]:
    """Split a normalized entry into its path parts and whether it names a folder."""
    is_folder = entry.endswith("/")
    return tuple((entry[:-1] if is_folder else entry).split("/")), is_folder


def deliverable_saved_name(entry: str) -> str:
    """The file name an entry is saved under: a folder becomes ``<folder>.zip``."""
    parts, is_folder = entry_parts(entry)
    return f"{parts[-1]}.zip" if is_folder else parts[-1]


def _normalized_entry(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise DeliverableEntryError(f"python_sandbox input.deliverable_outputs entries must be file or folder paths {_FORMS}.")
    text = raw.strip()
    is_folder = text.endswith("/")
    body = text[:-1] if is_folder else text
    parts = body.split("/")
    if (
        len(text) > MAX_ENTRY_CHARS
        or "\\" in text
        or "\x00" in text
        or not body
        or body.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise DeliverableEntryError(
            f"python_sandbox input.deliverable_outputs entry {raw!r} must be a relative path inside the working directory, {_FORMS}; "
            "absolute paths, '.', '..', empty parts and backslashes are not allowed."
        )
    if len(parts) > MAX_ENTRY_DEPTH:
        raise DeliverableEntryError(f"python_sandbox input.deliverable_outputs entry {raw!r} is deeper than {MAX_ENTRY_DEPTH} levels.")
    if (len(parts) > 1 or is_folder) and parts[0] in RESERVED_TOP_DIRS or any(part in SKIPPED_DIR_NAMES for part in parts):
        raise DeliverableEntryError(
            f"python_sandbox input.deliverable_outputs entry {raw!r} is inside inputs/, skill/ or __pycache__, which are never delivered."
        )
    return body + ("/" if is_folder else "")


def parse_deliverable_entries(raw_entries: list[Any]) -> list[str]:
    """Normalize ``deliverable_outputs`` entries in order, without duplicates, or raise ``DeliverableEntryError``."""
    entries: list[str] = []
    for raw in raw_entries:
        entry = _normalized_entry(raw)
        if entry not in entries:
            entries.append(entry)
    split = {entry: entry_parts(entry) for entry in entries}
    for folder, (folder_parts, is_folder) in split.items():
        if not is_folder:
            continue
        for other, (other_parts, _) in split.items():
            if other != folder and other_parts[: len(folder_parts)] == folder_parts:
                raise DeliverableEntryError(
                    f"python_sandbox input.deliverable_outputs entry {other!r} is already inside the declared folder {folder!r}; "
                    "list either the folder or the files in it."
                )
    saved: dict[str, str] = {}
    for entry in entries:
        name = deliverable_saved_name(entry)
        if name in saved:
            raise DeliverableEntryError(
                f"python_sandbox input.deliverable_outputs entries {saved[name]!r} and {entry!r} would both be saved as {name!r}; rename one of them."
            )
        saved[name] = entry
    return entries
