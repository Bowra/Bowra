"""Skill bundle 文件契约；配置页、生成与 Agent 读取共用，不依赖 Gateway/Core。"""

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
