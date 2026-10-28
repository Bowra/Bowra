"""配置页与 Agent 共用的相对文件路径和文本读取边界。"""

import pytest
from bowhead_skill.bundle_files import BundleFileError, bundle_file_path, list_bundle_files, read_bundle_text


def test_nested_text_roundtrip(tmp_path):
    path = tmp_path / "references" / "metrics" / "rules.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"margin": 0.2}', encoding="utf-8")
    assert list_bundle_files(tmp_path, "references") == [("metrics/rules.json", path.stat().st_size)]
    assert read_bundle_text(tmp_path, "references", "metrics/rules.json") == path.read_text()


@pytest.mark.parametrize("name", ["../secret", "/secret", "a/../../secret", "a\\secret", "a//b", "a/./b", "a/.secret", "a\x00b"])
def test_invalid_paths_rejected(tmp_path, name):
    with pytest.raises(BundleFileError):
        bundle_file_path(tmp_path, "references", name)


def test_symlink_file_and_directory_rejected(tmp_path):
    root = tmp_path / "skill"
    refs = root / "references"
    refs.mkdir(parents=True)
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (refs / "link.txt").symlink_to(outside)
    (refs / "nested").symlink_to(tmp_path, target_is_directory=True)
    assert list_bundle_files(root, "references") == []
    for name in ("link.txt", "nested/private.txt"):
        with pytest.raises(BundleFileError):
            read_bundle_text(root, "references", name)
