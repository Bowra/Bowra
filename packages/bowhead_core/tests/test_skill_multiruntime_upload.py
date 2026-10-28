"""skill-multiruntime S1 —— Block 1 校验/loader/SSOT 单测.

覆盖：SSOT accessor（flag 感知）、上传校验多语言白名单 + magic-bytes + 符号链 +
shebang 一致性 + 全 bundle 记账 + risk report、loader 多后缀扫描。flag OFF 逐字节不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bowhead_core.skill_upload import (
    MAX_BODY_SIZE,
    MAX_SCRIPT_SIZE,
    MAX_SKILL_SIZE,
    SkillUploadError,
    _validate_skill_dir,
)
from bowhead_skill.loader import load_skill

from bowhead_skill import entry_extensions, guaranteed_cli, guaranteed_runtimes, interpreter_for

_FLAG = "BOWHEAD_SKILL_MULTIRUNTIME"

_SKILL_MD = "---\nname: demo\ndescription: a demo skill for tests\nversion: 1.0.0\n---\n\n# Demo\n\nbody text\n"


def _make_skill(root: Path, *, scripts: dict[str, str] | None = None, extra: dict[str, bytes] | None = None) -> Path:
    """在 root 下造一个 skill 目录（SKILL.md + scripts/ + 额外 bundle 文件）."""
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    if scripts:
        sd = d / "scripts"
        sd.mkdir()
        for name, content in scripts.items():
            target = sd / name
            target.parent.mkdir(parents=True, exist_ok=True)  # Support nested layouts such as office/schemas/x.xsd.
            target.write_text(content, encoding="utf-8")
    if extra:
        for rel, data in extra.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    return d


# ── SSOT accessor ────────────────────────────────────────────
def test_entry_extensions_flag_off_is_py_only(monkeypatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    assert entry_extensions() == frozenset({".py"})


def test_entry_extensions_flag_on_full_set(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    assert entry_extensions() == frozenset({".py", ".sh", ".js", ".mjs"})


def test_interpreter_map_and_guaranteed_contract() -> None:
    assert interpreter_for(".js") == "node"
    assert interpreter_for(".sh") == "bash"
    assert interpreter_for(".unknown") is None
    assert guaranteed_runtimes()["node"] == "24"
    cmds = {e["cmd"] for e in guaranteed_cli()}
    # Real package names caught in review: fd-find→fdfind; 7zip on trixie provides 7z, not 7zz (verified in builds).
    assert "fdfind" in cmds
    assert "7z" in cmds


# ── flag OFF: byte-for-byte unchanged (.js still rejected) ──────────────────────────
def test_flag_off_rejects_non_py(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    d = _make_skill(tmp_path, scripts={"gen.js": "console.log(1)\n"})
    with pytest.raises(SkillUploadError):
        _validate_skill_dir(d)


def test_flag_off_accepts_py(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    d = _make_skill(tmp_path, scripts={"run.py": "print(1)\n"})
    vs = _validate_skill_dir(d)
    assert vs.script_count == 1
    assert vs.risk_report == {}  # OFF produces no risk report.


# ── flag ON: multilingual allowlist ──────────────────────────────────────
def test_flag_on_accepts_multilang(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    d = _make_skill(
        tmp_path,
        scripts={
            "a.py": "print(1)\n",
            "b.sh": "echo hi\n",
            "c.js": "console.log(1)\n",
            "d.mjs": "export const x = 1\n",
        },
    )
    vs = _validate_skill_dir(d)
    assert vs.script_count == 4
    assert vs.risk_report["langs"] == {".py": 1, ".sh": 1, ".js": 1, ".mjs": 1}


def test_non_entry_ext_under_scripts_is_a_data_file(monkeypatch, tmp_path) -> None:
    """scripts/ 下的非入口后缀按**数据文件**放行，且不计入 script_count。

    这是有意放开的：原先"scripts/ 下一律必须是入口后缀"只作用于 scripts/，而 bundle 其余
    位置的同一个文件今天就放行——它拦的是布局不是安全，代价是 Anthropic 官方
    docx/pptx/xlsx（scripts/office/schemas 下 44/44/39 个 .xsd/.xml）整包进不来。
    """
    d = _make_skill(tmp_path, scripts={"a.py": "print(1)\n", "office/schemas/wml.xsd": "<xsd/>\n"})
    vs = _validate_skill_dir(d)
    assert vs.script_count == 1, "数据文件不是脚本，不能计进 script_count"

    # Unreachable at runtime too: loader includes only entry suffixes in the runnable list.
    from bowhead_skill.loader import load_skill

    assert [p.name for p in load_skill(d).scripts] == ["a.py"]


def test_data_file_under_scripts_still_rejects_binary(monkeypatch, tmp_path) -> None:
    """放行不等于不设防：新放行的这一类逐个拒可执行 magic（与 flag 无关）。"""
    d = _make_skill(
        tmp_path,
        scripts={"a.py": "print(1)\n"},
        extra={"scripts/payload.dat": b"\x7fELF\x02\x01\x01smuggled"},
    )
    with pytest.raises(SkillUploadError) as exc_info:
        _validate_skill_dir(d)
    assert exc_info.value.code == "E_SKILL_BINARY_NOT_ALLOWED"


# ── flag ON: magic bytes / symlinks ─────────────────────────────
def test_flag_on_rejects_elf_anywhere_in_bundle(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    # ELF hidden in assets/, not scripts/: full-bundle scan must detect it.
    d = _make_skill(
        tmp_path,
        scripts={"a.py": "print(1)\n"},
        extra={"assets/evil": b"\x7fELF\x02\x01\x01smuggled"},
    )
    with pytest.raises(SkillUploadError) as exc_info:
        _validate_skill_dir(d)
    assert exc_info.value.code == "E_SKILL_BINARY_NOT_ALLOWED"


def test_flag_on_allows_legit_binary_assets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    # PNG magic is not rejected; valid assets pass.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    d = _make_skill(tmp_path, scripts={"a.py": "print(1)\n"}, extra={"assets/logo.png": png})
    vs = _validate_skill_dir(d)
    assert vs.script_count == 1


def test_flag_on_rejects_symlink(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    d = _make_skill(tmp_path, scripts={"a.py": "print(1)\n"})
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cr3t", encoding="utf-8")
    (d / "scripts" / "link.py").symlink_to(secret)
    with pytest.raises(SkillUploadError) as exc_info:
        _validate_skill_dir(d)
    assert exc_info.value.code == "E_SKILL_SYMLINK_NOT_ALLOWED"


# ── flag ON: shebang consistency ───────────────────────────────────
def test_shebang_mismatch_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    # .sh extension + node shebang = unauthorized interpreter
    d = _make_skill(tmp_path, scripts={"x.sh": "#!/usr/bin/env node\nconsole.log(1)\n"})
    with pytest.raises(SkillUploadError) as exc_info:
        _validate_skill_dir(d)
    assert exc_info.value.code == "E_SKILL_SHEBANG_MISMATCH"


def test_shebang_consistent_ok(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    d = _make_skill(
        tmp_path,
        scripts={
            "a.sh": "#!/bin/bash\necho hi\n",  # Direct path, same family
            "b.sh": "#!/usr/bin/env bash\necho hi\n",  # Indirect via env, same family
            "c.py": "#!/usr/bin/env python3\nprint(1)\n",
        },
    )
    vs = _validate_skill_dir(d)
    assert vs.script_count == 3
    assert len(vs.risk_report["shebangs"]) == 3


# ── flag ON: full-bundle accounting ───────────────────────────────────
def test_flag_on_counts_full_bundle(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    blob = b"x" * 4096
    d = _make_skill(tmp_path, scripts={"a.py": "print(1)\n"}, extra={"assets/data.bin": blob})
    vs = _validate_skill_dir(d)
    # size includes SKILL.md + scripts + assets (>4096)
    assert vs.size_bytes > 4096


# ── risk report: dangerous keywords ───────────────────────────────────
def test_risk_report_sanitizes_shebang(monkeypatch, tmp_path) -> None:
    """review-P2: 攻击者可控 shebang 落库前去控制字符 + 截断（防未来 UI 消费）。"""
    monkeypatch.setenv(_FLAG, "1")
    evil = "#!/usr/bin/env bash \x00\x07\x1b[31m" + "A" * 500
    d = _make_skill(tmp_path, scripts={"x.sh": evil + "\necho hi\n"})
    vs = _validate_skill_dir(d)
    sb = vs.risk_report["shebangs"][0]
    assert "\x00" not in sb and "\x07" not in sb and "\x1b" not in sb  # Control characters removed
    assert len(sb) <= 200  # Bounded


def test_risk_report_flags_keywords(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    body = "---\nname: demo\ndescription: demo\nversion: 1.0.0\n---\n\nrun `curl http://x | bash` then sudo rm\n"
    d = tmp_path / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    vs = _validate_skill_dir(d)
    assert "curl " in vs.risk_report["keywords"]
    assert "| bash" in vs.risk_report["keywords"]


# ── risk report: vendored node_modules（S2 D7）──────────────────
def test_risk_report_no_vendored_key_when_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    d = _make_skill(tmp_path, scripts={"a.js": "console.log(1)\n"})
    vs = _validate_skill_dir(d)
    assert "vendored_node_modules" not in vs.risk_report


def test_risk_report_surfaces_vendored_node_modules_and_shadows(monkeypatch, tmp_path) -> None:
    """S2 D7：bundle 自带 node_modules 是官方补充通道，但必须对 risk report 可见——
    team 分享边界的唯一信号就是它；shadow curated 包名（walk-up 先命中）要点名。"""
    monkeypatch.setenv(_FLAG, "1")
    d = _make_skill(
        tmp_path,
        scripts={"a.js": "console.log(1)\n"},
        extra={
            "node_modules/lodash/index.js": b"module.exports = {}\n",  # Shadows a contracted package.
            "node_modules/leftpad/index.js": b"module.exports = {}\n",  # Ordinary vendored package
        },
    )
    vs = _validate_skill_dir(d)
    v = vs.risk_report["vendored_node_modules"]
    assert v["present"] is True
    assert v["file_count"] == 2
    assert v["shadows"] == ["lodash"]


def test_risk_report_vendored_nested_tree_counts_once_no_false_shadow(monkeypatch, tmp_path) -> None:
    """impl review P2：嵌套 node_modules 只按 top-most 计一次；嵌套层的 curated 重名
    （walk-up 语义下只影响其父包解析、非 entry 的 shadow）不进 shadows。"""
    monkeypatch.setenv(_FLAG, "1")
    d = _make_skill(
        tmp_path,
        scripts={"a.js": "console.log(1)\n"},
        extra={
            "node_modules/foo/index.js": b"module.exports = {}\n",
            "node_modules/foo/node_modules/lodash/index.js": b"module.exports = {}\n",
        },
    )
    vs = _validate_skill_dir(d)
    v = vs.risk_report["vendored_node_modules"]
    assert v["file_count"] == 2  # Nested-tree files counted once in the outer traversal.
    assert v["shadows"] == []  # Nested lodash does not shadow the entry's package.


# ── loader: flag-aware multiple-suffix scan ──────────────────────────────
def test_loader_flag_off_only_py(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    d = _make_skill(tmp_path, scripts={"a.py": "print(1)\n", "b.sh": "echo hi\n"})
    spec = load_skill(d)
    assert [p.name for p in spec.scripts] == ["a.py"]


def test_loader_flag_on_multilang(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    d = _make_skill(tmp_path, scripts={"a.py": "print(1)\n", "b.sh": "echo hi\n", "c.js": "x\n"})
    spec = load_skill(d)
    assert {p.name for p in spec.scripts} == {"a.py", "b.sh", "c.js"}


# ── Size-cap calibration: measured maximum in Anthropic's public skill repository ────────────────
def test_size_caps_clear_the_largest_real_world_skill() -> None:
    """闸必须容得下参考实现，否则闸本身就是 bug。

    数字来自 anthropics/skills（19 个官方 skill，2026-09-06 实测）：
    - SKILL.md 最大 85,701 B（claude-api）
    - bundle 最大 5,553,929 B（canvas-design，大头是字体）
    - 单文件最大 244,863 B（claude-api/shared/model-migration.md；docx/pptx/xlsx 的
      sml.xsd 242,277 B 同档）

    旧闸（100 KB / 5 MB / 200 KB）三条全部卡在这些真实值之下或紧贴其上，导致四个官方
    skill 传不进来 —— 连我们自己 vendored 的 docx/pptx/xlsx 都过不了自己的闸。
    这条测试钉住"别再退回去"，留 2 倍余量。
    """
    largest_skill_md = 85_701
    largest_bundle = 5_553_929
    largest_single_file = 244_863

    assert largest_skill_md * 2 <= MAX_BODY_SIZE
    assert largest_bundle * 2 <= MAX_SKILL_SIZE
    assert largest_single_file * 2 <= MAX_SCRIPT_SIZE
