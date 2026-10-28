"""Skill bundle 附属文件的列举与单文件读取（个人 /skills 与团队 /team-skills 共用一份实现）.

``references/`` ``scripts/`` ``evals/`` 的列举、预览与就地编辑是同一套语义：支持安全的嵌套相对路径、
1MB 文本读取上限、非文本不预览。此前只有个人侧有实现——团队侧 detail
不带 references、也没有单文件端点，于是团队副本盘上文件完好、用户面却完全看不到
（用户报「skill 发布到团队后关联的 skill 没了」的真因是**读取面缺失**，不是数据丢失）。
补齐时把这套语义收在这里，避免两份实现漂移（错误码 / 上限 / 防护必须同步）。

错误一律经 ``skill_workflow_http_error`` 有限投影（平铺信封，与个人侧既有契约一致；
团队侧直接抛的 coded 错误同样是平铺形状，只有 SkillUploadError 那条走嵌套）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Final, Literal, NoReturn

from bowhead_core.skill_bundle_lock import skill_bundle_lock
from bowhead_core.skill_upload import ValidatedSkill, _split_frontmatter, _validate_skill_dir, bundle_sha256
from bowhead_skill.bundle_files import BUNDLE_FILE_KINDS, MAX_TEXT_BYTES, BundleFileError, bundle_file_path, read_bundle_text
from bowhead_skill.bundle_files import (
    list_bundle_files as _list_bundle_files,
)
from bowhead_skill.bundle_files import (
    validate_file_ref as _validate_file_ref,
)
from fastapi import status
from pydantic import BaseModel, Field

from bowhead_gateway.skill_workflow_errors import skill_workflow_http_error

MAX_PREVIEW_BYTES: Final = MAX_TEXT_BYTES

_MIME_BY_SUFFIX: Final[dict[str, str]] = {
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".json": "application/json",
    ".txt": "text/plain",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


def list_bundle_files(skill_dir: Path, kind: str) -> list[tuple[str, int]]:
    return _list_bundle_files(skill_dir, kind)


def read_bundle_manifest(skill_dir: Path) -> str:
    """读 bundle 的 SKILL.md **全文**（含 frontmatter）；缺文件/不可读返空串.

    编辑面的草稿源。展示面用的是去掉 frontmatter 的正文（个人侧 ``spec.body_md``、
    团队侧 ``display_body``）——两者不能混：编辑面写回的是整份 SKILL.md。
    """
    fp = skill_dir / "SKILL.md"
    if not fp.is_file():
        return ""
    try:
        return fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def display_body(manifest_md: str) -> str:
    """SKILL.md 全文 → 展示用正文（剥 frontmatter）；剥不动就原样返回.

    团队侧详情此前直接把全文当 body_md 下发，drawer 说明 tab 会把
    ``name: xxx / description: xxx`` 当正文渲出来（116 真机可见）。
    """
    try:
        _fm, body = _split_frontmatter(manifest_md)
    except Exception:
        return manifest_md
    return body


def evals_summary(skill_dir: Path) -> tuple[int, bool]:
    """``(evals_count, has_evals)``——以 ``evals/evals.json`` 是否存在为准，解析失败 count 保 0."""
    evals_json = skill_dir / "evals" / "evals.json"
    if not evals_json.is_file():
        return 0, False
    try:
        data = json.loads(evals_json.read_text(encoding="utf-8"))
        return len(data.get("evals", [])), True
    except (OSError, ValueError, AttributeError):
        return 0, True


def _file_error(exc: BundleFileError) -> NoReturn:
    code = exc.code
    http_status = {"skill_file_not_found": 404, "skill_file_too_large": 413, "skill_file_not_text": 415}.get(code, 400)
    raise skill_workflow_http_error(http_status, code) from exc


def validate_file_ref(kind: str, filename: str) -> None:
    try:
        _validate_file_ref(kind, filename)
    except BundleFileError as exc:
        _file_error(exc)


def _file_path(skill_dir: Path, kind: str, filename: str) -> Path:
    try:
        return bundle_file_path(skill_dir, kind, filename)
    except BundleFileError as exc:
        _file_error(exc)


def read_bundle_file(skill_dir: Path, kind: str, filename: str) -> dict[str, Any]:
    try:
        content = read_bundle_text(skill_dir, kind, filename)
    except BundleFileError as exc:
        _file_error(exc)
    return {
        "path": f"{kind}/{filename}",
        "content": content,
        "size": len(content.encode("utf-8")),
        "mime": _MIME_BY_SUFFIX.get(Path(filename).suffix.lower(), "text/plain"),
    }


def read_bundle_snapshot(skill_dir: Path, *, editable: bool) -> dict[str, Any]:
    """Read editable bytes and their precondition under the existing package lock."""
    with skill_bundle_lock(skill_dir, shared=True) if editable else nullcontext():
        body = read_bundle_manifest(skill_dir)
        count, has_evals = evals_summary(skill_dir)
        return {
            "body_raw": body,
            "body_md": display_body(body),
            **{
                kind if kind != "evals" else "eval_files": [{"name": name, "size": size} for name, size in list_bundle_files(skill_dir, kind)]
                for kind in BUNDLE_FILE_KINDS
            },
            "evals_count": count,
            "has_evals": has_evals,
            "content_sha256": bundle_sha256(skill_dir) if editable and body else None,
        }


def read_bundle_file_snapshot(skill_dir: Path, kind: str, filename: str, *, editable: bool) -> dict[str, Any]:
    with skill_bundle_lock(skill_dir, shared=True) if editable else nullcontext():
        payload = read_bundle_file(skill_dir, kind, filename)
        payload["content_sha256"] = bundle_sha256(skill_dir) if editable else None
        return payload


class BundleFileChange(BaseModel):
    operation: Literal["create", "rename", "delete"]
    kind: Literal["references", "scripts", "evals"]
    filename: str = Field(min_length=1, max_length=512)
    target: str | None = Field(None, max_length=512)
    content: str = Field("", max_length=MAX_TEXT_BYTES)
    expected_content_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")


def require_bundle_authoring() -> None:
    pass


def _validate_text(content: str) -> None:
    _reject_oversize(len(content.encode("utf-8")))
    if "\x00" in content:
        raise skill_workflow_http_error(415, "skill_file_not_text")


def _reject_oversize(size: int) -> None:
    """写侧与预览侧同一上限——编辑面存不下「打不开」的内容（细粒度上限仍由上传 gate 管）."""
    if size > MAX_PREVIEW_BYTES:
        raise skill_workflow_http_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "skill_file_too_large",
        )


def _mutate_and_validate(
    skill_dir: Path,
    mutate: Callable[[Path], None],
    publish: Callable[[Path], None],
    *,
    check: Callable[[ValidatedSkill], None] | None = None,
    expected_content_sha256: str | None = None,
) -> ValidatedSkill:
    # Validate a same-filesystem copy first, then atomically publish one file; running agents never read temporary invalid bytes.
    with skill_bundle_lock(skill_dir), tempfile.TemporaryDirectory(prefix=".skill-edit-", dir=skill_dir.parent) as tmp:
        if expected_content_sha256 is not None and bundle_sha256(skill_dir) != expected_content_sha256:
            raise skill_workflow_http_error(409, "skill_write_conflict")
        staged = Path(tmp) / "bundle"
        shutil.copytree(skill_dir, staged, symlinks=True)
        mutate(staged)
        validated = _validate_skill_dir(staged)
        if check is not None:
            check(validated)
        publish(staged)
        validated.source_dir = skill_dir
        return validated


def _write_and_validate(
    skill_dir: Path,
    fp: Path,
    content: str,
    *,
    expected_name: str | None = None,
    check: Callable[[ValidatedSkill], None] | None = None,
    expected_content_sha256: str | None = None,
) -> ValidatedSkill:
    relative = fp.relative_to(skill_dir)

    def mutate(staged: Path) -> None:
        if not (staged / relative).is_file():
            raise skill_workflow_http_error(404, "skill_file_not_found")
        (staged / relative).write_text(content, encoding="utf-8")
        if expected_name is not None:
            fm, _ = _split_frontmatter(content)
            if fm.get("name") and str(fm["name"]).strip() != expected_name:
                raise skill_workflow_http_error(422, "skill_body_name_immutable")

    return _mutate_and_validate(
        skill_dir, mutate, lambda staged: (staged / relative).replace(fp), check=check, expected_content_sha256=expected_content_sha256
    )


def change_bundle_file(
    skill_dir: Path,
    kind: str,
    filename: str,
    *,
    operation: str,
    content: str = "",
    target: str | None = None,
    check: Callable[[ValidatedSkill], None] | None = None,
    expected_content_sha256: str | None = None,
) -> tuple[dict[str, Any], ValidatedSkill]:
    fp = _file_path(skill_dir, kind, filename)
    if operation == "create":
        _validate_text(content)
    destination = _file_path(skill_dir, kind, target) if operation == "rename" and target else fp
    if operation == "rename" and not target:
        raise skill_workflow_http_error(409, "skill_file_exists")
    if operation not in {"create", "delete", "rename"}:
        raise skill_workflow_http_error(400, "skill_filename_invalid")

    payload: dict[str, Any] = {"path": f"{kind}/{filename}", "content": "", "size": 0, "mime": "text/plain"}

    def mutate(staged: Path) -> None:
        source = staged / fp.relative_to(skill_dir)
        dest = staged / destination.relative_to(skill_dir)
        if operation == "create" and source.exists() or operation == "rename" and dest.exists():
            raise skill_workflow_http_error(409, "skill_file_exists")
        if operation != "create" and not source.is_file():
            raise skill_workflow_http_error(404, "skill_file_not_found")
        if operation == "create":
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(content, encoding="utf-8")
        elif operation == "delete":
            source.unlink()
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            source.replace(dest)
        if operation != "delete":
            payload.update(read_bundle_file(staged, kind, target if operation == "rename" and target else filename))

    def publish(staged: Path) -> None:
        if operation == "delete":
            fp.unlink()
        elif operation == "rename":
            destination.parent.mkdir(parents=True, exist_ok=True)
            fp.replace(destination)
        else:
            fp.parent.mkdir(parents=True, exist_ok=True)
            (staged / fp.relative_to(skill_dir)).replace(fp)

    validated = _mutate_and_validate(skill_dir, mutate, publish, check=check, expected_content_sha256=expected_content_sha256)
    payload["content_sha256"] = validated.content_hash.removeprefix("sha256:")
    return payload, validated


def write_bundle_file(
    skill_dir: Path,
    kind: str,
    filename: str,
    content: str,
    *,
    check: Callable[[ValidatedSkill], None] | None = None,
    expected_content_sha256: str | None = None,
) -> tuple[dict[str, Any], ValidatedSkill]:
    """覆写 bundle 里**已存在**的一个 references/scripts/evals 文件（与 GET 对称）.

    只覆写、不新建：编辑面是「打开你正在看的那个文件再存回去」，新增文件走独立的 create 操作。
    调用方须先过 ``validate_file_ref`` 与各自的可编辑性门。
    """
    fp = _file_path(skill_dir, kind, filename)
    if not fp.is_file():
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_file_not_found",
        )
    _validate_text(content)
    validated = _write_and_validate(skill_dir, fp, content, check=check, expected_content_sha256=expected_content_sha256)
    return {
        "path": f"{kind}/{filename}",
        "content": content,
        "size": len(content.encode("utf-8")),
        "mime": _MIME_BY_SUFFIX.get(Path(filename).suffix.lower(), "text/plain"),
        "content_sha256": validated.content_hash.removeprefix("sha256:"),
    }, validated


def write_bundle_body(
    skill_dir: Path,
    content: str,
    *,
    expected_name: str,
    check: Callable[[ValidatedSkill], None] | None = None,
    expected_content_sha256: str | None = None,
) -> tuple[dict[str, Any], ValidatedSkill]:
    """覆写 bundle 的 SKILL.md 正文。名字（frontmatter ``name``）不可改——改名=换身份.

    盘目录名、skill_instances 行、团队行、grants、子代理引用全都按 name 索引；就地改名
    会让这些引用同时指错。要改名走「另存为新 skill」。
    """
    fp = skill_dir / "SKILL.md"
    if not fp.is_file():
        raise skill_workflow_http_error(
            status.HTTP_404_NOT_FOUND,
            "skill_file_not_found",
        )
    _validate_text(content)
    validated = _write_and_validate(skill_dir, fp, content, expected_name=expected_name, check=check, expected_content_sha256=expected_content_sha256)
    return (
        {
            "path": "SKILL.md",
            "content": content,
            "size": len(content.encode("utf-8")),
            "mime": "text/markdown",
            "content_sha256": validated.content_hash.removeprefix("sha256:"),
        },
        validated,
    )


__all__ = [
    "BUNDLE_FILE_KINDS",
    "MAX_PREVIEW_BYTES",
    "display_body",
    "evals_summary",
    "list_bundle_files",
    "read_bundle_file",
    "read_bundle_file_snapshot",
    "read_bundle_snapshot",
    "read_bundle_manifest",
    "validate_file_ref",
    "write_bundle_body",
    "write_bundle_file",
    "change_bundle_file",
    "BundleFileChange",
    "require_bundle_authoring",
]
