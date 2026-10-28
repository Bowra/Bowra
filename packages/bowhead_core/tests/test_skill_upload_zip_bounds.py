"""skill-audit 批C（#10）—— zip 解压前有界预检.

压缩 ≤5MB 的 zip bomb（高压缩比零块）在旧代码会先 extractall 解出 GB 级临时文件
（磁盘 DoS）再才撞 size 校验；现在按中央目录声明量在落盘前拒绝。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from bowhead_core.skill_upload import (
    SkillUploadError,
    validate_and_extract,
)


def _zip_with(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_zip_bomb_rejected_before_extraction(tmp_path, monkeypatch):
    # review P2: pin flag-off semantics (with the flag ON, old code raises the same code in post-extraction _full_bundle_size,
    # so failure is uncertain; delenv ensures this test fails on old code).
    monkeypatch.delenv("BOWHEAD_SKILL_MULTIRUNTIME", raising=False)
    # 60MB of zeros compress to ~60KB; declared 60MB exceeds the 5MB bundle cap → precheck rejects.
    bomb = _zip_with({"SKILL.md": b"---\nname: bomb\ndescription: d\nversion: 1.0\n---\nx", "references/huge.md": b"\x00" * (60 * 1024 * 1024)})
    assert len(bomb) < 5 * 1024 * 1024  # Compressed size passes the size precheck; decompression precheck rejects.
    with pytest.raises(SkillUploadError) as ei:
        validate_and_extract(
            data=bomb,
            filename="bomb.zip",
            user_id="alice",
            dest_root=tmp_path,
            existing_user_skills={},
        )
    assert ei.value.code == "E_SKILL_TOO_LARGE"
    # Reject before disk writes: no extracted large files should remain in the tmp tree.
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftovers == []


def test_zip_entry_count_capped(tmp_path):
    many = _zip_with({f"references/f{i:04d}.md": b"x" for i in range(2001)})
    with pytest.raises(SkillUploadError) as exc_info:
        validate_and_extract(
            data=many,
            filename="many.zip",
            user_id="alice",
            dest_root=tmp_path,
            existing_user_skills={},
        )
    assert exc_info.value.code == "E_SKILL_ZIP_TOO_MANY_ENTRIES"
    assert exc_info.value.params == {"max_entries": 2000}


def test_normal_zip_passes_precheck(tmp_path):
    ok = _zip_with({"SKILL.md": b"---\nname: normal-skill\ndescription: d\nversion: 1.0\n---\nbody"})
    vs = validate_and_extract(
        data=ok,
        filename="ok.zip",
        user_id="alice",
        dest_root=tmp_path,
        existing_user_skills={},
    )
    assert vs.name == "normal-skill"
