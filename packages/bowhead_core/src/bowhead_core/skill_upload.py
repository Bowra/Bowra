"""Skill upload validation + extract to user directory.

PRD § 6 安全 & 验证
    - Frontmatter: name (^[a-z0-9-]+$, ≤64) / description (≤2048) / version 必须
    - Body ≤ 256 KB
    - scripts/ 下**入口脚本**（后缀 ∈ entry_extensions）ast.parse 语法通过；
      其余文件按**数据文件**放行（schema / 模板 / 词表…），拒符号链与可执行 magic
    - 单文件 ≤ 1 MB；Total ≤ 16 MB
    - Per-user 最多 30 个；总存储 ≤ 100 MB

Two entrypoints:
    validate_and_extract(file_bytes, filename, *, user_id, dest_root)
        → ValidatedSkill(name, source_dir, description, version, ...)
    作为 gateway `POST /api/v1/skills/upload` 的核心工作流.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import io
import logging
import re
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from bowhead_skill.bundle_files import MAX_TEXT_BYTES, BundleFileError, bundle_file_path
from bowhead_skill.script_langs import (
    all_entry_extensions,
    entry_extensions,
    guaranteed_node_packages,
    interpreter_for,
    multiruntime_enabled,
)

from bowhead_core.skill_bundle_lock import skill_bundle_lock

logger = logging.getLogger(__name__)

# Gate calibration: observed maxima across Anthropic public skills (anthropics/skills, 19), with 2-3×
# headroom. Old limits **fell below real skills**: canvas-design bundle 5,553,923 B exceeds 5 MB;
# claude-api SKILL.md 85,701 B already reaches 84% of old 100 KB; docx/pptx/xlsx
# scripts/office/schemas/ISO-IEC29500-4_2016/sml.xsd 242,277 B exceeds old 200 KB.
MAX_SKILL_SIZE = 16 * 1024 * 1024  # 16 MB (~3× largest real skill, 5.55 MB).
MAX_BODY_SIZE = 256 * 1024  # 256 KB (3× largest real SKILL.md, 85.7 KB).
MAX_SCRIPT_SIZE = 1024 * 1024  # 1 MB per file (entry scripts and data files share limit).
MAX_USER_SKILLS = 30
MAX_USER_STORAGE = 100 * 1024 * 1024  # 100 MB
# skill-team-governance: `__candidate` / `__old` reserved suffixes for team-approval storage operations
# (.data/team_skills/<team_id>/<name>__candidate|__old); pattern excludes underscores,
# so skill names cannot structurally collide. This is the redundant second safeguard (tech-design risk note).
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_DESC_LEN = 2048
# Heuristic sensitive keywords (warning/risk report only, never block).
_DANGER_KEYWORDS = ("rm -rf", "sudo ", "curl ", "wget ", "| bash", "| sh")


@functools.lru_cache(maxsize=1)
def _reserved_skill_names() -> frozenset[str]:
    """内置 skill 名 ∪ PROFILES agent 名 —— user create 不可撞（workflow-solidify §5.2）.

    lru_cache：builtin catalog 只 parse 一次（impl-feasibility-4 性能）. import 失败
    降级空集（不阻断保存）.
    """
    names: set[str] = set()
    try:
        from bowhead_skill import builtin_skill_names

        names.update(builtin_skill_names())  # review-F8: shared cached owner
    except Exception:  # noqa: S110
        pass
    try:
        from bowhead_core.subagent import PROFILES

        names.update(PROFILES.keys())
    except Exception:  # noqa: S110
        pass
    try:
        # G5 (docs/skills-tools-ssot.md §5-G5): tool and skill name sets must be disjoint;
        # user skills must not collide with any tool, including pseudo tools (python_sandbox lesson).
        # RESERVED_TOOL_NAMES = active tools ∪ **prerename names**. Legacy names matter:
        # relinquished names can become user skills, indistinguishable in historical traces
        # (docs/design/工具改名迁移.md).
        from bowhead_core.agents._generated.tool_names import RESERVED_TOOL_NAMES

        names.update(RESERVED_TOOL_NAMES)
    except Exception:  # noqa: S110
        pass
    return frozenset(names)


@dataclass
class ValidatedSkill:
    name: str
    source_dir: Path  # .data/user_skills/<user>/<name>/
    description: str
    version: str | None
    body_md: str
    size_bytes: int
    script_count: int
    warnings: list[str]  # Nonfatal (e.g. "contains rm -rf keyword").
    content_hash: str = ""
    risk_report: dict[str, Any] = field(default_factory=dict)  # multiruntime: language/keywords/shebang (persist only).


class SkillUploadError(ValueError):
    """校验 / 解压失败的业务异常；HTTP 层只发布登记 code 的双语投影.

    ``message`` 只供服务端日志和 traceback 使用，不是用户显示协议。新调用方必须提供
    ``code``；Gateway 对历史 message-only 异常 fail closed 为 ``skill_bundle_invalid``。
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        **params: object,
    ) -> None:
        self.code: str | None = code
        self.params: dict[str, object] = params
        if code is not None and message is None:
            # Superclass fallback message for developer tracebacks.
            message = f"{code} {params}" if params else code
        super().__init__(message or "")


def peek_skill_name(body_md: str) -> str | None:
    """轻量解析 SKILL.md frontmatter 的 name（不做完整校验）——供调用方在 validate 前
    取 per-skill 锁用（team create 的盘写要与同名 promote 互斥）。非法/缺失返 None，
    调用方回落到无锁 validate（它会抛 coded 校验错，不会成功写盘）。"""
    try:
        fm, _ = _split_frontmatter(body_md or "")
        name = str(fm.get("name") or "").strip()
        return name if NAME_PATTERN.match(name) else None
    except Exception:
        return None


def bundle_sha256(skill_dir: Path) -> str:
    """skill bundle 内容指纹（团队审批 CAS 键 + 落后/已同步判定，tech-design §1）.

    按相对路径排序逐文件喂 path+content，与目录名无关——候选目录 rename 成已批目录后
    hash 不变，crash 恢复检测（approved hash==candidate_sha256）依赖这一点。"""
    h = hashlib.sha256()
    root = Path(skill_dir)
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(p.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def copy_validated_bundle(
    src_dir: Path,
    dest_dir: Path,
    *,
    expected_name: str | None = None,
    overwrite: bool = False,
    existing_user_skills: Mapping[str, int] | None = None,
) -> ValidatedSkill:
    """整目录快照拷贝 + 全量重验 + 落位（promote / copy-to-personal 共用；tech-design P1b/P2）.

    与 ``validate_and_save_generated`` 的 dest/overwrite 语义刻意不同（review P1-5 禁用后者）：
    那条路只重建 SKILL.md+scripts，会静默丢 references/ 等其余文件；其 overwrite 还会
    rmtree 目标——对审批盘布局意味着可 rmtree live 已批目录=审批绕过。本函数 copytree
    保留 bundle 全部文件、校验在 staging 副本上做（不信任盘上旧文件）、绝不触碰 dest
    之外的目录；staging 建在 dest 同父目录（同盘 move）。

    - ``expected_name``：frontmatter name 必须一致（promote/copy 都是同名转移，改名走对话重存）
    - ``existing_user_skills`` 传入时走个人配额闸（copy-to-personal）；None=团队侧（无配额，有意设计）
    - ``size_bytes`` 按 bundle 全量文件实测（含 references/，配额与展示一致）
    - dest 存在且 overwrite=False → ``E_SKILL_NAME_EXISTS``（FE 覆盖确认流按 code 判定）
    """
    src = Path(src_dir)
    if not (src / "SKILL.md").is_file():
        raise SkillUploadError(code="E_SKILL_MISSING_MANIFEST")
    # Reject symlinks unconditionally (independent of multiruntime flag; transfer function protects itself, security review P2):
    # copytree dereferences by default; a bundle symlink to any disk file becomes an ordinary copied file in
    # destination (cross-trust-boundary read bypass), making post-copy _validate_skill_dir is_symlink
    # always False. Scan without following links, then copy.
    for base, dirnames, filenames in src.walk(follow_symlinks=False):
        for n in (*dirnames, *filenames):
            p = base / n
            if p.is_symlink():
                raise SkillUploadError(
                    code="E_SKILL_SYMLINK_NOT_ALLOWED",
                )
    dest = Path(dest_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".skill-stage-", dir=str(dest.parent)))
    try:
        work = staging / "bundle"
        shutil.copytree(src, work)
        vs = _validate_skill_dir(work)
        if expected_name is not None and vs.name != expected_name:
            raise SkillUploadError(
                code="E_SKILL_NAME_MISMATCH",
            )
        vs.size_bytes = sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
        if existing_user_skills is not None:
            _check_user_quota(dict(existing_user_skills), name=vs.name, new_size_bytes=vs.size_bytes)
        with skill_bundle_lock(dest):
            if dest.exists():
                if not overwrite:
                    raise SkillUploadError(code="E_SKILL_NAME_EXISTS", name=vs.name)
                shutil.rmtree(dest)
            shutil.move(str(work), str(dest))
            vs.source_dir = dest
            return vs
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# Core entry points.
# ─────────────────────────────────────────────────────────────
# `validate_and_save_generated` manages only these two; other old-bundle contents must survive overwrite unchanged.
def _adopt_unmanaged_files(tmp_dir: Path, old_dir: Path, *, managed: tuple[str, ...]) -> None:
    """把旧 bundle 里**这次没被管到的**条目复制进 tmp_dir（覆盖保存前调）.

    `managed` = 本次调用真正提供了新内容的条目，按新内容整体替换；其余原样保留。

    ★ `scripts/` 是否托管取决于**调用方有没有传 scripts 参数**，不是看传没传空：
      - `scripts=None`（压根没提）⇒ 不托管 ⇒ 旧脚本原样留着。
        对话编辑必须这样——`skill_read` 只回脚本**文件名**、不回内容（2026-09-02 在 116 实测），
        agent 结构上拿不到脚本正文，要求它"原样带回"是不可能完成的动作，
        真机结果就是改一次正文 `script_count` 归零。
      - `scripts=[]`（显式空表）⇒ 托管 ⇒ 清空，这才是"我要删掉所有脚本"。
    旧目录不存在 / 读不动都静默跳过：这是保护性步骤，不该有能力搞死保存本身。
    """
    try:
        if not old_dir.is_dir():
            return
        for item in old_dir.iterdir():
            if item.name in managed:
                continue
            dest = tmp_dir / item.name
            if dest.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, dest, symlinks=True)
            else:
                shutil.copy2(item, dest)
    except OSError as e:
        raise SkillUploadError(code="skill_bundle_invalid") from e


def validate_and_save_generated(
    *,
    body_md: str,
    scripts: list[dict[str, str]] | None,
    user_id: str,
    dest_root: Path,
    existing_user_skills: Mapping[str, int],
    source_turn_id: str | None = None,
    overwrite: bool = False,
    reserved_skill_names: set[str] | None = None,
    files: list[dict[str, Any]] | None = None,
) -> ValidatedSkill:
    """对话生成路径：LLM 直接给 SKILL.md 文本 + 0-N 份 script 源码，走同一套 validator.

    跟 upload 不同的地方：输入不是 zip / 文件，而是 JSON 里的文本；其余 validation
    逻辑完全复用（把内容先写到 tmp_dir，然后调 _validate_skill_dir）.

    existing_user_skills: 现有 user skill 的 name → size_bytes 映射（仅
    user_upload/user_generated）。配额检查需要知道本次是否覆盖同名旧版——名字来自
    SKILL.md frontmatter，调用方无法预知，因此传映射而非聚合值。
    """
    # Unify count/storage checks in _check_user_quota after name known, no preparse fail-fast:
    # FE first hop always overwrite=false; full-quota same-name reupload must parse name first to return
    # E_SKILL_NAME_EXISTS (overwrite confirmation); early rejection swallows it as quota error (review P1).
    existing_skills = dict(existing_user_skills)

    tmp_dir = Path(tempfile.mkdtemp(prefix="skill-gen-"))
    try:
        # Write SKILL.md.
        if not body_md or not body_md.strip():
            raise SkillUploadError(code="E_SKILL_BODY_EMPTY")
        (tmp_dir / "SKILL.md").write_text(body_md, encoding="utf-8")

        # Write optional scripts/.
        if scripts:
            if not isinstance(scripts, list):
                raise SkillUploadError(code="E_SKILL_SCRIPTS_NOT_LIST")
            scripts_dir = tmp_dir / "scripts"
            scripts_dir.mkdir(exist_ok=True)
            for s in scripts:
                if not isinstance(s, dict):
                    raise SkillUploadError(code="E_SKILL_SCRIPT_INVALID")
                sname = str(s.get("name") or "").strip()
                content = s.get("content")
                if not sname or not isinstance(content, str):
                    raise SkillUploadError(code="E_SKILL_SCRIPT_INVALID")
                try:
                    script_path = bundle_file_path(tmp_dir, "scripts", sname)
                except BundleFileError as e:
                    raise SkillUploadError(code="skill_filename_invalid") from e
                if Path(sname).suffix not in entry_extensions():
                    raise SkillUploadError(code="E_SKILL_SCRIPT_NOT_PY", name=sname)
                script_path.parent.mkdir(parents=True, exist_ok=True)
                script_path.write_text(content, encoding="utf-8")

        # ★ On same-name overwrite, first carry over old-bundle parts **not supplied this time** (references/ evals/ etc.).
        # Otherwise `rmtree(final_dir) + move(tmp_dir)` permanently deletes them.
        # Originally for creation (absent target, harmless rmtree+move); later overwrite flag made
        # the same code destructive for existing bundles with references, without redefining overwrite.
        # Promotion **bypassed** it (copy_validated_bundle,
        # routes/skills.py `_promote_skill` comment); this function itself stayed unfixed.
        # Adopt **before** validation: validation/quota uses final shape (total size, symlinks, executable bits).
        # ⚠ Adopt only after peeking a **real name**: invalid frontmatter makes `peek_skill_name` return None,
        # while `dest_root/user_id/""` is **user skill root**, so adoption copies all sibling
        # skill directories into this bundle (observed). Currently peek failure ⇒
        # _validate_skill_dir also raises, an unwritten implicit invariant preventing damage,
        # after needlessly copying up to 30 skills. Avoid that path entirely.
        peeked = peek_skill_name(body_md)
        if not peeked:
            _validate_skill_dir(tmp_dir)
            raise SkillUploadError(code="E_SKILL_MISSING_MANIFEST")
        with skill_bundle_lock(dest_root / user_id / peeked):
            if overwrite and peeked:
                # scripts managed only if caller **actually supplied** argument (None = omitted ⇒ retain old scripts).
                managed = ("SKILL.md", "scripts") if scripts is not None else ("SKILL.md",)
                _adopt_unmanaged_files(tmp_dir, dest_root / user_id / peeked, managed=managed)

            if files is not None:
                if not isinstance(files, list):
                    raise SkillUploadError(code="skill_filename_invalid")
                seen: set[str] = set()
                for item in files:
                    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or "content" not in item:
                        raise SkillUploadError(code="skill_filename_invalid")
                    path = item["path"]
                    kind, _, filename = path.partition("/")
                    if path in seen or (scripts is not None and kind == "scripts"):
                        raise SkillUploadError(code="skill_filename_invalid")
                    seen.add(path)
                    try:
                        fp = bundle_file_path(tmp_dir, kind, filename)
                    except BundleFileError as e:
                        if e.code == "skill_file_kind_invalid":
                            raise SkillUploadError(code="skill_file_kind_invalid") from e
                        raise SkillUploadError(code="skill_filename_invalid") from e
                    content = item["content"]
                    if content is None:
                        if not fp.is_file():
                            raise SkillUploadError(code="skill_file_not_found")
                        fp.unlink()
                    else:
                        if not isinstance(content, str) or "\x00" in content:
                            raise SkillUploadError(code="skill_file_not_text")
                        if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
                            raise SkillUploadError(code="skill_file_too_large")
                        fp.parent.mkdir(parents=True, exist_ok=True)
                        fp.write_text(content, encoding="utf-8")

            # Full validation (same upload gate).
            vs = _validate_skill_dir(tmp_dir)
            if reserved_skill_names and vs.name in reserved_skill_names:
                raise SkillUploadError(
                    code="E_SKILL_RESERVED_NAME",
                    name=vs.name,
                )
            _check_user_quota(existing_skills, name=vs.name, new_size_bytes=vs.size_bytes)

            # Move to final location.
            user_dir = dest_root / user_id
            user_dir.mkdir(parents=True, exist_ok=True)
            final_dir = user_dir / vs.name
            if final_dir.exists():
                if not overwrite:
                    raise SkillUploadError(code="E_SKILL_NAME_EXISTS", name=vs.name)
                shutil.rmtree(final_dir)
            shutil.move(str(tmp_dir), str(final_dir))
            vs.source_dir = final_dir
            return vs
    except Exception:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        # Moved tmp_dir is gone; otherwise clean it.
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def uploaded_skill_name(data: bytes, filename: str) -> str | None:
    """在写入用户目录前解析上传包身份，供异步授权使用；ZIP 仍经过解压边界检查。"""
    if len(data) > MAX_SKILL_SIZE:
        raise SkillUploadError(code="E_SKILL_TOO_LARGE", max_mb=MAX_SKILL_SIZE // 1024 // 1024)
    if filename.lower().endswith(".zip"):
        with tempfile.TemporaryDirectory(prefix="skill-inspect-") as tmp:
            source = _extract_zip(data, Path(tmp))
            manifest = source / "SKILL.md"
            return peek_skill_name(manifest.read_text(encoding="utf-8", errors="replace")) if manifest.is_file() else None
    return peek_skill_name(data.decode("utf-8", errors="replace"))


def validate_and_extract(
    data: bytes,
    filename: str,
    *,
    user_id: str,
    dest_root: Path,
    existing_user_skills: Mapping[str, int],
    overwrite: bool = False,
    reserved_skill_names: set[str] | None = None,
) -> ValidatedSkill:
    """从上传的 bytes 抽出 skill，验证后写到最终目录. 失败抛 SkillUploadError.

    existing_user_skills: 现有 user skill 的 name → size_bytes 映射（仅
    user_upload/user_generated），语义同 validate_and_save_generated.
    """
    # Count/storage checks unified in _check_user_quota once name known (see validate_and_save_generated
    # matching comment; ≤5MB extraction cost acceptable).
    existing_skills = dict(existing_user_skills)
    # 1. Size precheck.
    if len(data) > MAX_SKILL_SIZE:
        raise SkillUploadError(
            code="E_SKILL_TOO_LARGE",
            max_mb=MAX_SKILL_SIZE // 1024 // 1024,
        )

    # 2. Dispatch by filename suffix: .zip / .md / .markdown.
    low = filename.lower()
    tmp_dir = Path(tempfile.mkdtemp(prefix="skill-upload-"))
    try:
        if low.endswith(".zip"):
            skill_src = _extract_zip(data, tmp_dir)
        elif low.endswith((".md", ".markdown")):
            # Single file: tmp_dir is skill directory.
            (tmp_dir / "SKILL.md").write_bytes(data)
            skill_src = tmp_dir
        else:
            raise SkillUploadError(
                code="E_SKILL_FILE_TYPE_UNSUPPORTED",
            )

        # 3. Validate contents.
        vs = _validate_skill_dir(skill_src)
        if reserved_skill_names and vs.name in reserved_skill_names:
            raise SkillUploadError(
                code="E_SKILL_RESERVED_NAME",
                name=vs.name,
            )

        # 4. User quota (same-name overwrite replaces, no new slot; deduct old version bytes).
        _check_user_quota(existing_skills, name=vs.name, new_size_bytes=vs.size_bytes)

        # 5. Check name conflict.
        user_dir = dest_root / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        final_dir = user_dir / vs.name
        with skill_bundle_lock(final_dir):
            if final_dir.exists():
                if not overwrite:
                    # Coded: FE overwrite confirmation checks code (audit 2026-07-21 #1; never revert to bare text).
                    raise SkillUploadError(code="E_SKILL_NAME_EXISTS", name=vs.name)
                shutil.rmtree(final_dir)

            # 6. Move to final location (whole directory).
            shutil.move(str(skill_src), str(final_dir))
            vs.source_dir = final_dir
            return vs
    except Exception:
        # cleanup tmp on error
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        # If skill_src equals tmp_dir, moved already; if subdir, remove empty shell.
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# Internal helpers.
# ─────────────────────────────────────────────────────────────
def _check_user_quota(
    existing_skills: Mapping[str, int],
    *,
    name: str,
    new_size_bytes: int,
) -> None:
    """条数 + 存储配额检查（名字已知后调用）.

    同名条目在 DB 层是 upsert 全量替换——不算净新增、旧版字节要扣除；否则用户到
    30 个上限后连覆盖同名都被拒、存储被双计（audit 2026-07-21 #4）。
    """
    prior_bytes = existing_skills.get(name)
    if prior_bytes is None and len(existing_skills) >= MAX_USER_SKILLS:
        raise SkillUploadError(
            code="E_SKILL_LIMIT_REACHED",
            max_count=MAX_USER_SKILLS,
        )
    projected = sum(existing_skills.values()) - (prior_bytes or 0) + new_size_bytes
    if projected > MAX_USER_STORAGE:
        raise SkillUploadError(
            code="E_SKILL_STORAGE_LIMIT_REACHED",
            max_mb=MAX_USER_STORAGE // 1024 // 1024,
        )


_MAX_ZIP_ENTRIES = 2000


def _extract_zip(data: bytes, tmp_dir: Path) -> Path:
    """解压到 tmp_dir. 返回含 SKILL.md 的目录（可能是 tmp_dir 本身或唯一子目录）."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SkillUploadError(code="E_SKILL_INVALID_ZIP") from e

    # skill-audit batch C (#10): bounded central-directory precheck before extraction; ≤5MB compressed zip bomb
    # can create GB-scale temp files (disk DoS). Reject declared extraction size above bundle cap before disk writes;
    # cap entry count against inode exhaustion too. zipfile read caps at declared file_size, trustworthy as upper bound.
    infos = zf.infolist()
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise SkillUploadError(
            code="E_SKILL_ZIP_TOO_MANY_ENTRIES",
            max_entries=_MAX_ZIP_ENTRIES,
        )
    declared_total = sum(i.file_size for i in infos)
    if declared_total > MAX_SKILL_SIZE:
        raise SkillUploadError(
            code="E_SKILL_TOO_LARGE",
            max_mb=MAX_SKILL_SIZE // 1024 // 1024,
        )

    # Prevent path traversal / absolute paths.
    for member in zf.namelist():
        if member.startswith("/") or ".." in Path(member).parts:
            raise SkillUploadError(code="E_SKILL_INVALID_ZIP_PATH")

    zf.extractall(tmp_dir)

    # SKILL.md must be at root or the sole subdirectory.
    if (tmp_dir / "SKILL.md").is_file():
        return tmp_dir
    children = [p for p in tmp_dir.iterdir() if p.is_dir()]
    if len(children) == 1 and (children[0] / "SKILL.md").is_file():
        return children[0]
    raise SkillUploadError(code="E_SKILL_MANIFEST_NOT_FOUND_IN_ZIP")


def _validate_skill_dir(skill_dir: Path) -> ValidatedSkill:
    """校验 SKILL.md + scripts/，返回规范化的 ValidatedSkill（未 move）."""
    md_path = skill_dir / "SKILL.md"
    if not md_path.is_file():
        raise SkillUploadError(code="E_SKILL_MISSING_MANIFEST")
    md_raw = md_path.read_text(encoding="utf-8", errors="replace")
    if len(md_raw.encode("utf-8")) > MAX_BODY_SIZE:
        raise SkillUploadError(
            code="E_SKILL_MANIFEST_TOO_LARGE",
            max_kb=MAX_BODY_SIZE // 1024,
        )

    # Parse YAML frontmatter.
    fm, body = _split_frontmatter(md_raw)

    # Required fields.
    name = str(fm.get("name") or "").strip()
    description = str(fm.get("description") or "").strip()
    version = fm.get("version")
    version = str(version).strip() if version is not None else None

    if not NAME_PATTERN.match(name):
        raise SkillUploadError(
            code="E_SKILL_NAME_INVALID",
        )
    if name in _reserved_skill_names():
        # workflow-solidify §5.2: reject builtin skill/agent name collisions. Per-request resolution structurally prevents
        # shadowing (catalog.get chooses builtin first); this is hygiene against user confusion (python_sandbox
        # creation appears ineffective) + PROFILES collisions. Generate/upload share
        # _validate_skill_dir chokepoint, covering standalone /upload+/generate.
        raise SkillUploadError(
            code="E_SKILL_RESERVED_NAME",
            name=name,
        )
    if not description:
        raise SkillUploadError(code="E_SKILL_MISSING_DESCRIPTION")
    if len(description) > MAX_DESC_LEN:
        raise SkillUploadError(
            code="E_SKILL_DESCRIPTION_TOO_LONG",
            max_len=MAX_DESC_LEN,
        )

    if "read_only_entry" in fm:
        from bowhead_skill.readonly_entry import ReadOnlyEntryError, parse_read_only_entry, validate_read_only_entry_files

        try:
            entry = parse_read_only_entry(fm["read_only_entry"])
            # Missing declared files belong to the shared pending-import workflow;
            # present files must already satisfy the text/path/size contract.
            validate_read_only_entry_files(entry, skill_dir, allow_missing=True)
        except ReadOnlyEntryError as exc:
            raise SkillUploadError(code="E_SKILL_READ_ONLY_ENTRY_INVALID") from exc

    # Heuristic dangerous-shell warnings (never block).
    warnings: list[str] = []
    for kw in _DANGER_KEYWORDS:
        if kw in description.lower() or kw in body.lower():
            warnings.append(f"内容含敏感关键词 '{kw}'，确认是否是描述性提及")

    # Optional scripts/; allowed_ext follows flag: OFF→{'.py'}, byte-identical.
    allowed_ext = entry_extensions()
    # All known entry extensions (flag-independent) distinguish currently disallowed entries
    # from nonentry data files: reject the former, allow latter.
    _all_entry_ext = frozenset(all_entry_extensions())
    scripts_dir = skill_dir / "scripts"
    script_count = 0
    total_size = md_path.stat().st_size
    if scripts_dir.is_dir():
        for p in scripts_dir.rglob("*"):
            if not p.is_file():
                continue
            total_size += p.stat().st_size
            if total_size > MAX_SKILL_SIZE:
                raise SkillUploadError(
                    code="E_SKILL_TOO_LARGE",
                    max_mb=MAX_SKILL_SIZE // 1024 // 1024,
                )
            if p.stat().st_size > MAX_SCRIPT_SIZE:
                raise SkillUploadError(
                    code="E_SKILL_SCRIPT_TOO_LARGE",
                    max_kb=MAX_SCRIPT_SIZE // 1024,
                )
            if p.suffix in _all_entry_ext and p.suffix not in allowed_ext:
                # Known entry suffix, currently flag-disallowed: still reject. Accepting as data would let
                # a future flag enable it **without revalidation**, although upload shebang-consistency
                # gate never ran. This is precisely multiruntime flag semantics.
                raise SkillUploadError(
                    code="E_SKILL_SCRIPT_EXTENSION_INVALID",
                )
            if p.suffix not in allowed_ext:
                # **Data files** (.xsd / .xml / templates / vocabularies…): allow, exclude from script_count.
                #
                # Why requiring entry suffix for all scripts/ files was no security boundary: check affected only
                # scripts/, while arbitrary files elsewhere (references/, templates/, root)
                # are already allowed; moving directory bypasses it. It restricted **layout** alone,
                # specifically Anthropic official docx/pptx/xlsx scripts/office/schemas/
                # containing 44/44/39 .xsd/.xml files. Our vendored copies also fail this gate
                # (builtin disk loading bypasses validator, hiding it).
                #
                # Allowing still protects: individually reject symlinks and executable magic for these new files below,
                # unreachable at runtime too: loader includes only entry_extensions suffixes in
                # `SkillSpec.scripts`, so data files never execute as entries.
                if p.is_symlink():
                    raise SkillUploadError(code="E_SKILL_SYMLINK_NOT_ALLOWED")
                try:
                    head = p.read_bytes()[:4]
                except OSError:
                    head = b""
                if any(head.startswith(m) for m in _EXECUTABLE_MAGICS):
                    raise SkillUploadError(code="E_SKILL_BINARY_NOT_ALLOWED")
                continue
            # AST syntax check (.py only; .sh/.js report sandbox runtime errors, host lacks node).
            if p.suffix == ".py":
                try:
                    ast.parse(p.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError as e:
                    raise SkillUploadError(
                        code="E_SKILL_SCRIPT_SYNTAX_INVALID",
                    ) from e
            script_count += 1

    # skill-multiruntime S1 (D2): flag ON adds full-bundle accounting + executable/symlink rejection
    # + shebang-consistency lint + risk report. OFF skips all (byte-identical).
    total_size = _full_bundle_size(skill_dir)
    if total_size > MAX_SKILL_SIZE:
        raise SkillUploadError(code="E_SKILL_TOO_LARGE", max_mb=MAX_SKILL_SIZE // 1024 // 1024)
    risk_report: dict[str, Any] = {}
    if multiruntime_enabled():
        _reject_executables_and_symlinks(skill_dir)
        if scripts_dir.is_dir():
            _lint_shebangs(scripts_dir)
        risk_report = _build_risk_report(skill_dir, scripts_dir, body)

    return ValidatedSkill(
        name=name,
        source_dir=skill_dir,  # Still temporary; upload replaces it.
        description=description,
        version=version,
        body_md=md_raw,  # Keep full frontmatter/body for SkillCatalog reparsing.
        size_bytes=total_size,
        script_count=script_count,
        warnings=warnings,
        risk_report=risk_report,
        content_hash="sha256:" + bundle_sha256(skill_dir),
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """从 markdown 头部剥 YAML frontmatter. 无则返 ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    # Find second --- delimiter.
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        raise SkillUploadError(code="E_SKILL_MANIFEST_NOT_CLOSED")
    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise SkillUploadError(code="E_SKILL_MANIFEST_PARSE") from e
    if not isinstance(fm, dict):
        raise SkillUploadError(code="E_SKILL_MANIFEST_NOT_OBJECT")
    return fm, body


# ─────────────────────────────────────────────────────────────
# skill-multiruntime S1: multiruntime validation helpers (flag ON only).
# ─────────────────────────────────────────────────────────────
_EXECUTABLE_MAGICS = (
    b"\x7fELF",  # ELF
    b"MZ",  # PE / DOS
    b"\xfe\xed\xfa\xce",  # Mach-O 32 BE
    b"\xfe\xed\xfa\xcf",  # Mach-O 64 BE
    b"\xce\xfa\xed\xfe",  # Mach-O 32 LE
    b"\xcf\xfa\xed\xfe",  # Mach-O 64 LE
    b"\xca\xfe\xba\xbe",  # Mach-O fat / universal
)
# Shebang interpreter family: script extension canonical interpreter (SSOT interpreter_for) must match
# shebang family, blocking valid-extension/unauthorized-interpreter combos (e.g. .sh with #!node).
_INTERP_FAMILY = {
    "python3": {"python", "python3"},
    "bash": {"bash", "sh"},
    "node": {"node", "nodejs"},
}


def _full_bundle_size(skill_dir: Path) -> int:
    """全 bundle 文件字节和（§9-5 记账修复：references/assets 一并入账）."""
    total = 0
    for p in skill_dir.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total


def _reject_executables_and_symlinks(skill_dir: Path) -> None:
    """全 bundle 拒二进制 magic bytes + 拒符号链（P2 兜底：防未来 ingest 路径重开洞）."""
    for p in skill_dir.rglob("*"):
        if p.is_symlink():
            raise SkillUploadError(
                code="E_SKILL_SYMLINK_NOT_ALLOWED",
            )
        if not p.is_file():
            continue
        try:
            head = p.read_bytes()[:4]
        except OSError:
            continue
        if any(head.startswith(m) for m in _EXECUTABLE_MAGICS):
            raise SkillUploadError(
                code="E_SKILL_BINARY_NOT_ALLOWED",
            )


def _shebang_interpreter(first_line: str) -> str | None:
    """从 shebang 行解出解释器 basename（处理 ``#!/usr/bin/env <interp>`` 间接）."""
    if not first_line.startswith("#!"):
        return None
    tokens = first_line[2:].strip().split()
    if not tokens:
        return None
    first = Path(tokens[0]).name
    if first == "env":
        # Skip env options such as -S/-i; take first argument not starting with "-".
        for t in tokens[1:]:
            if not t.startswith("-"):
                return Path(t).name
        return None
    return first


def _lint_shebangs(scripts_dir: Path) -> None:
    """脚本 shebang 解释器必须与扩展名规范解释器同族，否则拒（越权解释器闸）."""
    allowed_ext = entry_extensions()
    for p in scripts_dir.rglob("*"):
        if not p.is_file() or p.suffix not in allowed_ext:
            continue
        try:
            first_line = p.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
        except OSError:
            continue
        interp = _shebang_interpreter(first_line)
        if interp is None:
            continue
        canonical = interpreter_for(p.suffix)
        family = _INTERP_FAMILY.get(canonical or "", set())
        if interp not in family:
            raise SkillUploadError(
                code="E_SKILL_SHEBANG_MISMATCH",
            )


# Persisted risk_report may reach UI (review-P2): attacker-controlled filename/shebang text
# strips nonprintables, truncates, and caps count before persistence, ensuring safe bounded text without FE escaping.
_MAX_RISK_SHEBANGS = 50
_MAX_RISK_STR = 200


def _sanitize_risk_str(s: str) -> str:
    return "".join(ch for ch in s if ch.isprintable())[:_MAX_RISK_STR]


def build_bundle_risk_report(skill_dir: Path) -> dict[str, Any]:
    """The upload risk report for an existing bundle, independent of the multi-runtime switch.

    Agent compilation uses it for Skills uploaded while the switch was off, which
    carry no stored report (agent-skill-gates D3). It always counts the full
    entry-extension set, so the report does not change when the switch flips.
    """
    md_path = skill_dir / "SKILL.md"
    md_raw = md_path.read_text(encoding="utf-8", errors="replace")
    if len(md_raw.encode("utf-8")) > MAX_BODY_SIZE:
        raise ValueError("SKILL.md exceeds the manifest size limit")
    _, body = _split_frontmatter(md_raw)
    return _build_risk_report(skill_dir, skill_dir / "scripts", body, allowed_extensions=frozenset(all_entry_extensions()))


def _build_risk_report(
    skill_dir: Path,
    scripts_dir: Path,
    body: str,
    *,
    allowed_extensions: frozenset[str] | None = None,
) -> dict[str, Any]:
    """结构化 risk report（S1 只落库、不做硬闸）：脚本语言分布 / 危险关键词 / shebang.

    S2 追加 ``vendored_node_modules``：bundle 自带 node_modules 是官方补充通道（walk-up
    先于镜像根 /node_modules 命中），能力维度无提权（沙箱内代码本可 require 任意路径），
    但 team 分享边界的唯一信号是 risk report——vendored JS 必须对它可见；``shadows`` 标出
    与 curated 契约包重名的顶层目录（供 team-share 审看，不 gate 不 block）。
    """
    allowed_ext = entry_extensions() if allowed_extensions is None else allowed_extensions
    langs: dict[str, int] = {}
    shebangs: list[str] = []
    if scripts_dir.is_dir():
        for p in sorted(scripts_dir.rglob("*")):
            if not p.is_file() or p.suffix not in allowed_ext:
                continue
            langs[p.suffix] = langs.get(p.suffix, 0) + 1
            if len(shebangs) >= _MAX_RISK_SHEBANGS:
                continue
            try:
                first_line = p.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
            except OSError:
                continue
            if first_line.startswith("#!"):
                shebangs.append(_sanitize_risk_str(f"{p.name}: {first_line.strip()}"))
    keywords = sorted({kw for kw in _DANGER_KEYWORDS if kw in body.lower()})
    report: dict[str, Any] = {"langs": langs, "keywords": keywords, "shebangs": shebangs}
    vendored = _scan_vendored_node_modules(skill_dir)
    if vendored is not None:
        report["vendored_node_modules"] = vendored
    return report


def _scan_vendored_node_modules(skill_dir: Path) -> dict[str, Any] | None:
    """扫 bundle 内的 ``node_modules`` 目录；无则返 None（report 不带该键）.

    只取 top-most 命中（父路径含 node_modules 的跳过）：嵌套树的文件在外层遍历里
    已计数，且嵌套 ``node_modules/x/node_modules/<pkg>`` 在 walk-up 语义下只影响
    x 自身的解析、对 entry 不构成 shadow。curated 集现无 scoped 包（``@scope/pkg``）；
    若将来加入，shadow 匹配需下探两级（顶层目录名是 ``@scope``）。
    """
    dirs = [p for p in sorted(skill_dir.rglob("node_modules")) if p.is_dir() and not p.is_symlink() and "node_modules" not in p.parent.parts]
    if not dirs:
        return None
    curated = {e["name"] for e in guaranteed_node_packages()}
    file_count = 0
    shadows: set[str] = set()
    for d in dirs:
        file_count += sum(1 for p in d.rglob("*") if p.is_file())
        shadows.update(c.name for c in d.iterdir() if c.is_dir() and c.name in curated)
    return {"present": True, "file_count": file_count, "shadows": sorted(shadows)}
