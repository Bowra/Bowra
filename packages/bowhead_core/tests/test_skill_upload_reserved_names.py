from __future__ import annotations

import pytest
from bowhead_core.skill_upload import SkillUploadError, validate_and_save_generated


def test_generated_skill_rejects_reserved_name(tmp_path) -> None:
    body = "---\nname: public\ndescription: user attempt to shadow public\nversion: 1.0.0\n---\n\nbody\n"

    with pytest.raises(SkillUploadError) as exc_info:
        validate_and_save_generated(
            body_md=body,
            scripts=[],
            user_id="u1",
            dest_root=tmp_path / "user_skills",
            existing_user_skills={},
            reserved_skill_names={"public"},
        )
    assert exc_info.value.code == "E_SKILL_RESERVED_NAME"
    assert exc_info.value.params == {"name": "public"}
