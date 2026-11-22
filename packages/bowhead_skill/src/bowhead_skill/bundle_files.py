"""Skill bundle 文件契约；配置页、生成与 Agent 读取共用，不依赖 Gateway/Core。"""

import mimetypes
import os
from collections.abc import Collection, Iterable
from pathlib import Path, PurePath

BUNDLE_FILE_KINDS = ("references", "scripts", "evals")
MAX_TEXT_BYTES = 1024 * 1024


def is_python_cache_path(relative_path: str | PurePath) -> bool:
    """Identify generated Python cache entries, not source package content."""
    path = PurePath(relative_path)
    return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}


class BundleFileError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_file_ref(kind: str, filename: str) -> None:
    if kind not in BUNDLE_FILE_KINDS:
        raise BundleFileError("skill_file_kind_invalid")
    if "\\" in filename or "\x00" in filename or any(not part or part.startswith(".") for part in filename.split("/")):
        raise BundleFileError("skill_filename_invalid")


def bundle_file_path(skill_dir: Path, kind: str, filename: str) -> Path:
    """允许安全的嵌套相对路径；拒绝沿途符号链接（包括 kind 目录）。"""
    validate_file_ref(kind, filename)
    path = skill_dir
    parts = (kind, *filename.split("/"))
    for index, part in enumerate(parts):
        path = path / part
        if path.is_symlink() or (index < len(parts) - 1 and path.exists() and not path.is_dir()):
            raise BundleFileError("skill_filename_invalid")
    try:
        path.resolve().relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise BundleFileError("skill_filename_invalid") from exc
    return path


def list_bundle_files(skill_dir: Path, kind: str) -> list[tuple[str, int]]:
    validate_file_ref(kind, "probe")
    root = skill_dir / kind
    if root.is_symlink() or not root.is_dir():
        return []
    result: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        try:
            safe = bundle_file_path(skill_dir, kind, name)
            if safe.is_file():
                result.append((name, safe.stat().st_size))
        except (BundleFileError, OSError):
            continue
    return result


def read_bundle_text(skill_dir: Path, kind: str, filename: str) -> str:
    path = bundle_file_path(skill_dir, kind, filename)
    if not path.is_file():
        raise BundleFileError("skill_file_not_found")
    with path.open("rb") as stream:
        data = stream.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise BundleFileError("skill_file_too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundleFileError("skill_file_not_text") from exc
    if "\x00" in text:
        raise BundleFileError("skill_file_not_text")
    return text


# Reading a Skill package by its path from the Skill root
# (docs/sprints/skill-bundle-read-2026-11-20/tech-design.md). Any file of the package is
# readable; folders are conventions, not limits. The kind-scoped helpers above
# remain the settings-page contract.

MAX_LISTED_PACKAGE_FILES = 200


def normalize_package_path(raw: str) -> str:
    """Return a clean path relative to the Skill root, as SKILL.md links write it.

    A file inside a folder links its siblings with `..` (`references/format.md` →
    `../examples/create.json`), so `..` steps back and a leading one is dropped.
    The result is only ever looked up among the package's own files.
    """
    text = raw.strip() if isinstance(raw, str) else ""
    if not text or text.startswith("/") or "\\" in text or any(ord(char) < 32 for char in text):
        raise BundleFileError("skill_path_invalid")
    parts: list[str] = []
    for part in text.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in {"", "."}:
            parts.append(part)
    if not parts:
        raise BundleFileError("skill_path_invalid")
    return "/".join(parts)


MAX_PACKAGE_PATH_CHARS = 512


def validate_package_write_path(raw: object) -> str:
    """A path an edit may write: relative to the Skill root, exactly as it will be stored.

    Any folder and file name is allowed, hidden names included, as for reading.
    Unlike reading, no `.` or `..` step and no empty segment is accepted, so the
    path names one place. SKILL.md is the manifest; it is written through its
    own body path and can never be deleted or renamed here.
    """
    text = raw if isinstance(raw, str) else ""
    if (
        not text
        or len(text) > MAX_PACKAGE_PATH_CHARS
        or text.startswith("/")
        or "\\" in text
        or any(ord(char) < 32 for char in text)
        or any(part in {"", ".", ".."} for part in text.split("/"))
        or text == "SKILL.md"
    ):
        raise BundleFileError("skill_filename_invalid")
    return text


def resolve_package_path(raw: str, paths: Collection[str], *, prefer_references: bool) -> str | None:
    """Map what the model passed to one package path, or None.

    `ref` callers (prefer_references) keep the legacy reading under references/
    first; `file` callers try the path from the Skill root first. A missing
    `.md` is added only to a name without a suffix. A bare file name that
    matches exactly one file anywhere in the package resolves to it.
    """
    path = normalize_package_path(raw)
    available = set(paths)
    with_md = [path, path + ".md"] if not PurePath(path).suffix else [path]
    under_references = ["references/" + item for item in with_md]
    for candidate in (*under_references, *with_md) if prefer_references else (*with_md, *under_references):
        if candidate in available:
            return candidate
    if "/" not in path:
        names = set(with_md)
        matches = [item for item in available if PurePath(item).name in names]
        if len(matches) == 1:
            return matches[0]
    return None


def package_file_path(skill_dir: Path, path: str) -> Path:
    """A package file by its normalized path: contained, and no symlink along the way."""
    parts = normalize_package_path(path).split("/")
    current = skill_dir
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink() or (index < len(parts) - 1 and current.exists() and not current.is_dir()):
            raise BundleFileError("skill_path_invalid")
    try:
        current.resolve().relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise BundleFileError("skill_path_invalid") from exc
    return current


def list_package_files(skill_dir: Path, *, builtin: bool = False) -> list[tuple[str, int]]:
    """Every regular file of the package, `(path, size)`, sorted; symlinks are skipped."""
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        return []
    result: list[tuple[str, int]] = []
    for base, dirnames, filenames in skill_dir.walk(follow_symlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not (base / name).is_symlink())
        for name in filenames:
            file = base / name
            relative = file.relative_to(skill_dir).as_posix()
            if file.is_symlink() or (builtin and is_python_cache_path(relative)):
                continue
            try:
                if file.is_file():
                    result.append((relative, file.stat().st_size))
            except OSError:
                continue
    return sorted(result)


def decode_package_text(data: bytes) -> str | None:
    """UTF-8 text without NUL, or None for content that is not text."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def package_file_listing(files: Iterable[tuple[str, int]]) -> dict:
    """The model-facing file list: at most MAX_LISTED_PACKAGE_FILES entries."""
    entries = [{"path": path, "size": size} for path, size in files]
    listing: dict = {"package_files": entries[:MAX_LISTED_PACKAGE_FILES]}
    if len(entries) > MAX_LISTED_PACKAGE_FILES:
        listing["package_files_omitted"] = len(entries) - MAX_LISTED_PACKAGE_FILES
    return listing


def media_type_for(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def read_package_file(skill_dir: Path, path: str) -> tuple[bytes, int]:
    """Up to MAX_TEXT_BYTES + 1 bytes of a package file, and its full size."""
    file = package_file_path(skill_dir, path)
    if not file.is_file():
        raise BundleFileError("skill_file_not_found")
    with file.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        return stream.read(MAX_TEXT_BYTES + 1), size


def package_read_result(name: str, path: str, data: bytes, *, ref_requested: bool, size: int | None = None) -> dict:
    """The model-facing result for one resolved package file."""
    size = len(data) if size is None else size
    if size > MAX_TEXT_BYTES or len(data) > MAX_TEXT_BYTES:
        return {
            "error_kind": "skill_file_too_large",
            "error": f"{path} is {size} bytes; skill_read returns text files up to {MAX_TEXT_BYTES} bytes.",
            "file": path,
            "size": size,
            "limit": MAX_TEXT_BYTES,
        }
    text = decode_package_text(data)
    if text is None:
        return {
            "name": name,
            "file": path,
            "binary": True,
            "size": size,
            "media_type": media_type_for(path),
            # Script mode runs from /workdir and command mode from /workdir/skill, so name the absolute path.
            "note": (
                f"Not text, so its content is not shown. Its path from the Skill root is {path}; "
                f"a script of this Skill running in the sandbox reaches it at /workdir/skill/{path}."
            ),
        }
    result = {"name": name, "file": path, "body": text}
    if ref_requested and path.startswith("references/"):
        result["ref"] = path.removeprefix("references/")
    return result


def package_miss(name: str, requested: str, files: Iterable[tuple[str, int]], *, reason: str = "skill_file_not_found") -> dict:
    """An actionable miss: what was asked, and every file the package has."""
    listing = package_file_listing(files)
    if reason == "skill_path_invalid":
        error = f"{requested!r} is not a path inside Skill '{name}'. Paths are relative to the Skill root; absolute paths are not allowed."
        kind = "invalid_args"
    else:
        error = f"File {requested!r} not found in Skill '{name}'. Pass one of package_files as file, exactly as listed."
        kind = "skill_file_not_found"
    return {
        "error_kind": kind,
        "error": error,
        "requested": requested,
        **listing,
        # Pre-existing key: names under references/, for callers that read it.
        "available": [entry["path"].removeprefix("references/") for entry in listing["package_files"] if entry["path"].startswith("references/")],
    }
