"""reserved-floor: user create 不可撞内置 skill / agent 名（workflow-solidify §5.2 / §10-A item①）.

per-request 后 shadow 已结构不可能；floor 是 hygiene（防困惑 + 防撞 PROFILES）+ 堵
standalone /upload+/generate 两路径（共用 _validate_skill_dir chokepoint）.
"""

import pytest
from bowhead_core.skill_upload import (
    SkillUploadError,
    _reserved_skill_names,
    validate_and_save_generated,
)


def _gen(name: str, dest, desc: str = "ok"):
    body = f"---\nname: {name}\ndescription: {desc}\nversion: 0.1.0\n---\nbody for {name}\n"
    return validate_and_save_generated(
        body_md=body,
        scripts=None,
        user_id="alice",
        dest_root=dest,
        existing_user_skills={},
    )


def test_reserved_set_includes_builtin_and_profiles():
    reserved = _reserved_skill_names()
    # Include at least core built-ins that actually exist in the catalog.
    assert "python_sandbox" in reserved
    # Also reserve PROFILES agent names, such as lead.
    assert "lead" in reserved


def test_generate_rejects_builtin_name(tmp_path):
    """撞 builtin（python_sandbox）→ SkillUploadError."""
    with pytest.raises(SkillUploadError):
        _gen("python_sandbox", tmp_path)


def test_generate_rejects_agent_name(tmp_path):
    """撞 PROFILES agent 名（lead）→ SkillUploadError."""
    with pytest.raises(SkillUploadError):
        _gen("lead", tmp_path)


def test_generate_allows_non_reserved_name(tmp_path):
    """正常自定义名（不撞）→ 通过."""
    vs = _gen("my-custom-channel-margin", tmp_path)
    assert vs.name == "my-custom-channel-margin"
