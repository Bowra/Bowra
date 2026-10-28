"""覆盖上传的配额语义（audit 2026-07-21 #4）.

同名覆盖在 DB 层是 upsert 全量替换：不算净新增条数、旧版字节要从存储中扣除。
历史 bug：count 检查先于 overwrite 判定（到 30 上限后连覆盖都被拒）+ storage
检查不减旧版（双计）。三条调用路（upload/generate/skill_save_generated）共用
validator，这里直接测 validator 两个入口。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from bowhead_core.skill_upload import (
    MAX_USER_SKILLS,
    MAX_USER_STORAGE,
    SkillUploadError,
    validate_and_extract,
    validate_and_save_generated,
)


def _body(name: str, pad: int = 0) -> str:
    return f"---\nname: {name}\ndescription: quota test\nversion: 0.1.0\n---\nbody\n" + "x" * pad


def _zip_bytes(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", _body(name))
    return buf.getvalue()


def _full_map(*, include: str | None = None) -> dict[str, int]:
    """恰好打满 30 个条数上限的 existing 映射；include 指定其中一个名字."""
    names = [f"filler-{i}" for i in range(MAX_USER_SKILLS)]
    if include is not None:
        names[0] = include
    return dict.fromkeys(names, 100)


def test_overwrite_at_count_cap_succeeds(tmp_path):
    vs = validate_and_save_generated(
        body_md=_body("quota-target"),
        scripts=None,
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills=_full_map(include="quota-target"),
        overwrite=True,
    )
    assert vs.name == "quota-target"


def test_new_skill_at_count_cap_rejected_even_with_overwrite(tmp_path):
    with pytest.raises(SkillUploadError) as exc_info:
        validate_and_save_generated(
            body_md=_body("brand-new"),
            scripts=None,
            user_id="alice",
            dest_root=tmp_path,
            existing_user_skills=_full_map(),
            overwrite=True,
        )
    assert exc_info.value.code == "E_SKILL_LIMIT_REACHED"
    assert exc_info.value.params == {"max_count": MAX_USER_SKILLS}


def test_new_name_at_count_cap_without_overwrite_rejected(tmp_path):
    with pytest.raises(SkillUploadError) as exc_info:
        validate_and_save_generated(
            body_md=_body("anything"),
            scripts=None,
            user_id="alice",
            dest_root=tmp_path,
            existing_user_skills=_full_map(),
            overwrite=False,
        )
    assert exc_info.value.code == "E_SKILL_LIMIT_REACHED"


def test_same_name_at_count_cap_without_overwrite_raises_name_exists(tmp_path):
    """review P1：满额+同名+overwrite=False 必须报 E_SKILL_NAME_EXISTS（FE 覆盖确认流
    靠此 code 触发），不能被上限错吞掉——禁止在解析名字前 fail-fast。"""
    existing = _full_map(include="quota-target")
    # First create a real same-name directory (triggering the step-5 name-exists branch).
    validate_and_save_generated(
        body_md=_body("quota-target"),
        scripts=None,
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills=existing,
        overwrite=True,
    )
    with pytest.raises(SkillUploadError) as ei:
        validate_and_save_generated(
            body_md=_body("quota-target"),
            scripts=None,
            user_id="alice",
            dest_root=tmp_path,
            existing_user_skills=existing,
            overwrite=False,
        )
    assert ei.value.code == "E_SKILL_NAME_EXISTS"

    with pytest.raises(SkillUploadError) as ei2:
        validate_and_extract(
            data=_zip_bytes("quota-target"),
            filename="quota-target.zip",
            user_id="alice",
            dest_root=tmp_path,
            existing_user_skills=existing,
            overwrite=False,
        )
    assert ei2.value.code == "E_SKILL_NAME_EXISTS"


def test_overwrite_storage_subtracts_old_version(tmp_path):
    # Existing storage fills exactly 100MB, including a 60MB old version; without subtracting it, any new byte exceeds the quota.
    existing = {"quota-target": 60 * 1024 * 1024, "other": MAX_USER_STORAGE - 60 * 1024 * 1024}
    vs = validate_and_save_generated(
        body_md=_body("quota-target"),
        scripts=None,
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills=existing,
        overwrite=True,
    )
    assert vs.name == "quota-target"


def test_overwrite_storage_still_capped_after_subtraction(tmp_path):
    # Still over quota after subtracting the 10-byte old version → reject.
    existing = {"quota-target": 10, "other": MAX_USER_STORAGE - 100}
    with pytest.raises(SkillUploadError) as exc_info:
        validate_and_save_generated(
            body_md=_body("quota-target", pad=500),
            scripts=None,
            user_id="alice",
            dest_root=tmp_path,
            existing_user_skills=existing,
            overwrite=True,
        )
    assert exc_info.value.code == "E_SKILL_STORAGE_LIMIT_REACHED"
    assert exc_info.value.params == {
        "max_mb": MAX_USER_STORAGE // 1024 // 1024,
    }


def test_upload_zip_overwrite_at_count_cap_succeeds(tmp_path):
    vs = validate_and_extract(
        data=_zip_bytes("quota-target"),
        filename="quota-target.zip",
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills=_full_map(include="quota-target"),
        overwrite=True,
    )
    assert vs.name == "quota-target"


def test_upload_zip_new_name_at_count_cap_rejected(tmp_path):
    with pytest.raises(SkillUploadError) as exc_info:
        validate_and_extract(
            data=_zip_bytes("brand-new"),
            filename="brand-new.zip",
            user_id="alice",
            dest_root=tmp_path,
            existing_user_skills=_full_map(),
            overwrite=True,
        )
    assert exc_info.value.code == "E_SKILL_LIMIT_REACHED"


def test_duplicate_name_without_overwrite_raises_coded_error(tmp_path):
    """重名错误必须带 code=E_SKILL_NAME_EXISTS——FE 覆盖确认流按 code 判定（audit #1）."""
    kwargs = {
        "user_id": "alice",
        "dest_root": tmp_path,
        "existing_user_skills": {"quota-target": 100},
    }
    validate_and_extract(data=_zip_bytes("quota-target"), filename="a.zip", overwrite=True, **kwargs)
    with pytest.raises(SkillUploadError) as ei:
        validate_and_extract(data=_zip_bytes("quota-target"), filename="a.zip", overwrite=False, **kwargs)
    assert ei.value.code == "E_SKILL_NAME_EXISTS"

    with pytest.raises(SkillUploadError) as ei2:
        validate_and_save_generated(
            body_md=_body("quota-target"),
            scripts=None,
            overwrite=False,
            **kwargs,
        )
    assert ei2.value.code == "E_SKILL_NAME_EXISTS"
