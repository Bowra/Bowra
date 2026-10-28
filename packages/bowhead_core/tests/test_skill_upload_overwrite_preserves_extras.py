"""★覆盖保存不许吃掉 references/ —— 「改一次正文删一次参考资料」的回归闸.

`validate_and_save_generated` 原本是给**新建**写的：自己搭一个只含 SKILL.md + scripts/ 的
临时目录，validate 完 move 到最终位置。后来加了 `overwrite` 开关，同一段
`rmtree(final_dir) + move(tmp_dir)` 对「已存在且带 references 的 bundle」就变成了
破坏性替换——旧目录里**调用方没提供的一切**（references/ evals/）永久消失，而 overwrite
的语义从没被重新定义过。

promote 那条路当年是**绕开**它（改用 `copy_validated_bundle`，见 `_promote_skill` 注释 +
`test_promote_l1_first_submission_preserves_references`），函数本身一直没修，于是
`POST /skills/generate?overwrite=true` 与 `skill_save_generated` 工具照踩——后者的失败提示
还直接教 LLM「重调本工具并传 overwrite=true」。

正确语义：overwrite = 「用这次提供的内容替换**这些部分**」，不是「删掉整个 bundle」。
"""

from __future__ import annotations

import pytest
from bowhead_core.skill_upload import validate_and_save_generated


def _body(name: str, marker: str) -> str:
    return f"---\nname: {name}\ndescription: overwrite extras test\nversion: 0.1.0\n---\n正文 {marker}\n"


def _seed(tmp_path, name: str = "gm-recipe"):
    """先建一版，再往盘上补 references/ 与 evals/（它们只会经 zip 上传/promote 进来）。"""
    vs = validate_and_save_generated(
        body_md=_body(name, "v1"),
        scripts=[{"name": "step_1.py", "content": "x = 1\n"}],
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills={},
    )
    d = vs.source_dir
    (d / "references").mkdir()
    (d / "references" / "notes.md").write_text("口径备注", encoding="utf-8")
    (d / "references" / "second.md").write_text("第二份", encoding="utf-8")
    (d / "evals").mkdir()
    (d / "evals" / "evals.json").write_text('{"evals": [{"q": "a"}]}', encoding="utf-8")
    return d


@pytest.mark.unit
def test_overwrite_keeps_references_and_evals(tmp_path):
    d = _seed(tmp_path)

    vs = validate_and_save_generated(
        body_md=_body("gm-recipe", "v2"),
        scripts=[{"name": "step_1.py", "content": "x = 2\n"}],
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills={"gm-recipe": 100},
        overwrite=True,
    )

    assert vs.source_dir == d
    assert "正文 v2" in (d / "SKILL.md").read_text(encoding="utf-8")  # Body replaced.
    assert (d / "scripts" / "step_1.py").read_text(encoding="utf-8") == "x = 2\n"  # Scripts replaced.
    # ★ Preserve unspecified parts verbatim.
    assert sorted(p.name for p in (d / "references").iterdir()) == ["notes.md", "second.md"]
    assert (d / "references" / "notes.md").read_text(encoding="utf-8") == "口径备注"
    assert (d / "evals" / "evals.json").exists()


@pytest.mark.unit
def test_overwrite_replaces_scripts_wholesale(tmp_path):
    """传了 scripts ⇒ 那是完整集合，整体替换，旧脚本不许残留（否则删不掉脚本）。"""
    d = _seed(tmp_path)
    (d / "scripts" / "step_2.py").write_text("y = 1\n", encoding="utf-8")

    validate_and_save_generated(
        body_md=_body("gm-recipe", "v2"),
        scripts=[{"name": "step_1.py", "content": "x = 2\n"}],
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills={"gm-recipe": 100},
        overwrite=True,
    )

    assert sorted(p.name for p in (d / "scripts").iterdir()) == ["step_1.py"]
    assert (d / "references" / "notes.md").exists()  # References remain too.


@pytest.mark.unit
def test_omitting_scripts_keeps_them(tmp_path):
    """★★只改正文（不传 scripts）⇒ 旧脚本原样留着。

    116 真机（2026-09-02）：agent 用对话改正文，只传了 body_md，落盘后 `script_count` 归零。
    它**不是忘了**——`skill_read` 只回脚本文件名、不回内容，agent 结构上拿不到脚本正文，
    "原样带回"是不可能完成的动作。所以「没提 scripts」只能解释成「我没动脚本」。
    """
    d = _seed(tmp_path)

    validate_and_save_generated(
        body_md=_body("gm-recipe", "v2"),
        scripts=None,  # ← Not specified at all.
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills={"gm-recipe": 100},
        overwrite=True,
    )

    assert "正文 v2" in (d / "SKILL.md").read_text(encoding="utf-8")
    assert [p.name for p in (d / "scripts").iterdir()] == ["step_1.py"]
    assert (d / "scripts" / "step_1.py").read_text(encoding="utf-8") == "x = 1\n"  # Content unchanged too.
    assert (d / "references" / "notes.md").exists()


@pytest.mark.unit
def test_explicit_empty_scripts_clears_them(tmp_path):
    """负对照：显式传空表 = 「删光所有脚本」，这条语义不能被上面那条吃掉。"""
    d = _seed(tmp_path)

    validate_and_save_generated(
        body_md=_body("gm-recipe", "v2"),
        scripts=[],  # ← Explicitly empty.
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills={"gm-recipe": 100},
        overwrite=True,
    )

    assert not (d / "scripts").exists() or not any((d / "scripts").iterdir())
    assert (d / "references" / "notes.md").exists()  # References remain unaffected.


@pytest.mark.unit
def test_size_accounts_for_the_adopted_files(tmp_path):
    """接过来的文件要算进 size/配额——校验发生在合并之后，算的是最终形态。"""
    _seed(tmp_path)
    vs = validate_and_save_generated(
        body_md=_body("gm-recipe", "v2"),
        scripts=None,
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills={"gm-recipe": 100},
        overwrite=True,
    )
    body_only = len(_body("gm-recipe", "v2").encode("utf-8"))
    assert vs.size_bytes >= body_only


@pytest.mark.unit
def test_new_skill_path_unchanged(tmp_path):
    """负对照：目标目录不存在时行为与改动前完全一致（新建不受影响）。"""
    vs = validate_and_save_generated(
        body_md=_body("brand-new", "v1"),
        scripts=None,
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills={},
        overwrite=True,
    )
    assert sorted(p.name for p in vs.source_dir.iterdir()) == ["SKILL.md"]


@pytest.mark.parametrize(
    "scripts,files,code",
    [
        ([{"name": "../outside.py", "content": "print(1)"}], None, "skill_filename_invalid"),
        (None, [{"path": "references/../outside.md", "content": "private"}], "skill_filename_invalid"),
        (None, [{"path": "unknown/file.md", "content": "private"}], "skill_file_kind_invalid"),
    ],
)
def test_generated_bundle_path_errors_are_finite_and_keep_existing(tmp_path, monkeypatch, scripts, files, code):
    from bowhead_core.skill_upload import SkillUploadError, bundle_sha256

    saved = validate_and_save_generated(body_md=_body("bounded", "old"), scripts=None, user_id="u1", dest_root=tmp_path, existing_user_skills={})
    before = bundle_sha256(saved.source_dir)
    with pytest.raises(SkillUploadError) as error:
        validate_and_save_generated(
            body_md=_body("bounded", "new"), scripts=scripts, files=files, user_id="u1", dest_root=tmp_path, existing_user_skills={}, overwrite=True
        )
    assert error.value.code == code
    assert error.value.params == {}
    assert bundle_sha256(saved.source_dir) == before


@pytest.mark.parametrize("path_kind", ["scripts", "files"])
def test_generated_bundle_does_not_publish_unknown_provider_error(tmp_path, monkeypatch, path_kind):
    from bowhead_skill.bundle_files import BundleFileError

    from bowhead_core import skill_upload

    def fail_provider(*args):
        raise BundleFileError("private-provider-code")

    monkeypatch.setattr(skill_upload, "bundle_file_path", fail_provider)
    kwargs = (
        {"scripts": [{"name": "run.py", "content": "print(1)"}]}
        if path_kind == "scripts"
        else {"scripts": None, "files": [{"path": "references/file.md", "content": "body"}]}
    )
    with pytest.raises(skill_upload.SkillUploadError) as error:
        validate_and_save_generated(body_md=_body("bounded", "new"), user_id="u1", dest_root=tmp_path, existing_user_skills={}, **kwargs)
    assert error.value.code == "skill_filename_invalid"
    assert "private-provider-code" not in str(error.value)
