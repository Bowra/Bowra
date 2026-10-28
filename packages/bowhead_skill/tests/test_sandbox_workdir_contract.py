"""feedback-0713 #4b — 沙箱路径契约 + workdir reaper.

① python_sandbox 的 tool result 不再回显宿主一次性绝对路径（outputs 相对名 +
   workdir=None）——每次调用 mkdtemp 新目录, 模型跨调用复用旧路径必
   FileNotFoundError（prod t_296fb09131ad seq5）; artifact 引用（output_artifacts）
   才是跨调用复用数据的正道。
② 其它 skill（consultant-deck 等）Lead 侧仍按宿主路径消费 outputs（读 .pptx 上传
   deliverable）——绝对路径保持不变, 别把它们一起剥。
③ workdir reaper: keep_workdir=True + BOWHEAD_SKILL_WORKDIR_ROOT 从不清理曾把
   116 的盘打满——按 TTL 清扫 bow-skill-* 目录。
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_skill.runtime import (
    SkillRuntime,
    _reap_stale_workdirs,
    _resolve_workdir_root,
    _workdir_root_unusable_reason,
)
from bowhead_skill.tools import build_skill_tools

from bowhead_skill import SkillCatalog

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeResult:
    ok: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    result_json: dict = field(default_factory=dict)
    outputs: list = field(default_factory=list)
    workdir: Any = None
    duration_ms: int = 0
    error: str | None = None


class _FixedRuntime:
    def __init__(self, result: _FakeResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, spec, *, entry_script, input_data, extra_args):
        self.calls.append(dict(input_data))
        return self._result


def _mk_skill(root: Path, name: str, script: str = "run.py") -> None:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nbody\n", encoding="utf-8")
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / script).write_text("print('x')\n", encoding="utf-8")


def _invoke_tool(cat: SkillCatalog, rt: Any):
    return next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")


def _flat_tool(cat: SkillCatalog, rt: Any):
    return next(t for t in build_skill_tools(cat, rt) if t.name == "python_exec")


@pytest.mark.parametrize("entry", ["skill_invoke", "python_exec"])
@pytest.mark.parametrize("script_ok", [True, False], ids=["completed-script", "partial-data-after-failure"])
async def test_python_sandbox_result_separates_script_facts_artifacts_and_file_listing(tmp_path: Path, entry: str, script_ok: bool) -> None:
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    wd = tmp_path / "bow-skill-python_sandbox-xyz"
    rt = _FixedRuntime(
        _FakeResult(
            stdout="Runner wrapper output",
            outputs=[wd / "rev_monthly.csv", wd / "nested" / "debug.txt"],
            workdir=wd,
            result_json={
                "ok": script_ok,
                "exit_code": 0 if script_ok else 1,
                "stdout": "Computed total: 42",
                "stderr": "" if script_ok else "ValueError: the final step failed",
                "outputs": [{"name": "rev_monthly.csv", "artifact_id": "art_a1b2c3", "mime": "text/csv", "size": 9}],
            },
        )
    )
    tool = _invoke_tool(cat, rt) if entry == "skill_invoke" else _flat_tool(cat, rt)
    arguments = (
        {"name": "python_sandbox", "script": "runner.py", "input": {"code": "print('done')"}}
        if entry == "skill_invoke"
        else {"code": "print('done')"}
    )
    res = await tool.handler(arguments, object())
    # Relative names, without host paths.
    assert res["outputs"] == ["rev_monthly.csv", "nested/debug.txt"]
    assert res["workdir"] is None
    assert str(tmp_path) not in str(res["outputs"])
    # Artifact refs remain exposed (the supported way to reuse data across calls).
    assert res["output_artifacts"][0]["artifact_id"] == "art_a1b2c3"
    assert len(res["output_artifacts"]) == 1
    assert res["stdout"] == "Runner wrapper output"
    assert res["result_json"]["stdout"] == "Computed total: 42"
    assert res["result_json"]["exit_code"] == (0 if script_ok else 1)
    assert res["ok"] is script_ok


async def test_python_sandbox_threads_explicit_deliverable_purpose_as_control(
    tmp_path: Path,
) -> None:
    """用户点名交付的文件由受控 runner 信号定型，purpose 不进入用户代码输入。"""
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _FixedRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)

    await tool.handler(
        {
            "name": "python_sandbox",
            "script": "runner.py",
            "input": {
                "code": "open('final.mp4', 'wb').write(b'video')",
                "purpose": "deliverable",
                "deliverable_outputs": ["final.mp4"],
                # The LLM must not bypass the normal purpose field by writing server control keys directly.
                "_output_purpose": "analysis",
                "_deliverable_outputs": ["helper.json"],
            },
        },
        object(),
    )

    assert rt.calls[0]["_output_purpose"] == "deliverable"
    assert rt.calls[0]["_deliverable_outputs"] == ["final.mp4"]
    assert "purpose" not in rt.calls[0]
    assert "deliverable_outputs" not in rt.calls[0]


async def test_flat_python_exec_threads_the_same_deliverable_control(
    tmp_path: Path,
) -> None:
    """compute 主路的扁平入口必须复用同一校验与 runner 控制信号。"""
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _FixedRuntime(_FakeResult())

    await _flat_tool(cat, rt).handler(
        {
            "code": "open('final.mp4', 'wb').write(b'video')",
            "purpose": "deliverable",
            "deliverable_outputs": ["final.mp4"],
        },
        object(),
    )

    assert rt.calls[0]["_output_purpose"] == "deliverable"
    assert rt.calls[0]["_deliverable_outputs"] == ["final.mp4"]
    assert "purpose" not in rt.calls[0]
    assert "deliverable_outputs" not in rt.calls[0]


async def test_python_sandbox_defaults_output_purpose_to_analysis(
    tmp_path: Path,
) -> None:
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _FixedRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)

    await tool.handler(
        {
            "name": "python_sandbox",
            "script": "runner.py",
            "input": {"code": "open('scratch.mp4', 'wb').write(b'video')"},
        },
        object(),
    )

    assert rt.calls[0]["_output_purpose"] == "analysis"
    assert rt.calls[0]["_deliverable_outputs"] == []


async def test_python_sandbox_rejects_ambiguous_deliverable_without_filename(
    tmp_path: Path,
) -> None:
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _FixedRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)

    result = await tool.handler(
        {
            "name": "python_sandbox",
            "script": "runner.py",
            "input": {
                "code": "open('final.mp4', 'wb').write(b'video')",
                "purpose": "deliverable",
            },
        },
        object(),
    )

    assert result["error_kind"] == "invalid_args"
    assert "deliverable_outputs" in result["error"]
    assert rt.calls == []


async def test_python_sandbox_rejects_deliverable_filename_with_path(
    tmp_path: Path,
) -> None:
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _FixedRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)

    for filename in ("nested/final.mp4", "nested\\final.mp4", 123):
        result = await tool.handler(
            {
                "name": "python_sandbox",
                "script": "runner.py",
                "input": {
                    "code": "print('done')",
                    "purpose": "deliverable",
                    "deliverable_outputs": [filename],
                },
            },
            object(),
        )

        assert result["error_kind"] == "invalid_args"
        assert "top-level output filenames" in result["error"]
    assert rt.calls == []


async def test_other_skill_keeps_host_paths(tmp_path: Path) -> None:
    """consultant-deck 等 Lead 侧宿主路径消费者不受影响（subagent.py 读 .pptx 上传）。"""
    _mk_skill(tmp_path, "consultant_deck", script="render_deck.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    wd = tmp_path / "bow-skill-consultant_deck-xyz"
    rt = _FixedRuntime(_FakeResult(outputs=[wd / "deck.pptx"], workdir=wd))
    tool = _invoke_tool(cat, rt)
    res = await tool.handler({"name": "consultant_deck", "script": "render_deck.py", "input": {}}, object())
    assert res["outputs"] == [str(wd / "deck.pptx")]
    assert res["workdir"] == str(wd)


def test_workdir_root_falls_back_to_install_dir_sandbox(tmp_path: Path, monkeypatch) -> None:
    """Use an existing installation sandbox directory when the explicit root is absent."""
    monkeypatch.delenv("BOWHEAD_SKILL_WORKDIR_ROOT", raising=False)
    monkeypatch.setenv("BOWHEAD_INSTALL_DIR", str(tmp_path))
    # Missing directory → do not guess (local development still uses system /tmp).
    assert _resolve_workdir_root() is None
    (tmp_path / "sandbox").mkdir()
    assert _resolve_workdir_root() == str(tmp_path / "sandbox")
    # Explicit configuration always takes precedence.
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", "/explicit/root")
    assert _resolve_workdir_root() == "/explicit/root"


async def test_sandbox_fails_closed_when_no_shared_workdir_root(tmp_path: Path, monkeypatch) -> None:
    """④ 共享根解析不出来 + executor 路启用 → 就地 fail closed，别把 executor 的
    `workdir rejected: [Errno 2] /tmp/...` 甩到用户脸上（客户实测到的就是这条）。"""
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    spec = cat.get("python_sandbox")
    assert spec is not None
    monkeypatch.delenv("BOWHEAD_SKILL_WORKDIR_ROOT", raising=False)
    monkeypatch.delenv("BOWHEAD_INSTALL_DIR", raising=False)
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", "http://sandbox-executor:8091")

    res = await SkillRuntime().run(spec, entry_script="runner.py", input_data={})
    assert res.ok is False
    assert res.error is not None
    assert "sandbox 未就绪" in res.error
    # User-facing output must not contain host paths / errno noise.
    assert "/tmp/" not in res.error
    assert "Errno" not in res.error


async def test_non_sandbox_skill_unaffected_by_executor_gate(tmp_path: Path, monkeypatch) -> None:
    """②的另一面：其余 skill 跑在 app 容器内、不经 executor，别被这条闸误伤。"""
    _mk_skill(tmp_path, "consultant_deck", script="render_deck.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    spec = cat.get("consultant_deck")
    assert spec is not None
    monkeypatch.delenv("BOWHEAD_SKILL_WORKDIR_ROOT", raising=False)
    monkeypatch.delenv("BOWHEAD_INSTALL_DIR", raising=False)
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", "http://sandbox-executor:8091")

    res = await SkillRuntime().run(spec, entry_script="render_deck.py", input_data={})
    # The script actually ran (print('x') → exit 0); the gate did not block it.
    assert res.ok is True
    assert res.workdir is not None


def test_reap_stale_workdirs_removes_only_old_prefixed_dirs(tmp_path: Path) -> None:
    old = tmp_path / "bow-skill-python_sandbox-old"
    old.mkdir()
    (old / "input.json").write_text("{}", encoding="utf-8")
    old_marker = old.with_name(f".{old.name}.cancelled")
    old_marker.touch()
    stale_ts = time.time() - 48 * 3600
    os.utime(old, (stale_ts, stale_ts))

    fresh = tmp_path / "bow-skill-python_sandbox-fresh"
    fresh.mkdir()
    fresh_marker = fresh.with_name(f".{fresh.name}.cancelled")
    fresh_marker.touch()

    unrelated = tmp_path / "not-a-workdir"
    unrelated.mkdir()
    os.utime(unrelated, (stale_ts, stale_ts))

    removed = _reap_stale_workdirs(tmp_path, ttl_hours=24)
    assert removed == 1
    assert not old.exists()
    assert not old_marker.exists()
    assert fresh.exists()
    assert fresh_marker.exists()
    assert unrelated.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Shared workdir existence does not imply usability. A root-owned 0755 directory
# passes is_dir() but remains unwritable by uid 1000; reject it before exposing
# raw permission errors or host paths to end users.
#
# These tests guard the promise that users never see errno, not merely ok is False.
# **Do not use permission bits** (chmod is ineffective when tests run as root, yielding false passes);
# monkeypatch mkdtemp to raise PermissionError, independent of uid and effective in every environment.
# ─────────────────────────────────────────────────────────────────────────────


def test_workdir_root_unusable_reason_distinguishes_writable_from_present(tmp_path: Path, monkeypatch) -> None:
    """可写 → None；建不出来 → 带 errno 的原因串（原因只进日志，不进用户话术）。"""
    assert _workdir_root_unusable_reason(str(tmp_path)) is None
    # Remove probe directories; leave no debris in the shared root (reaper recognizes only the bow-skill-* prefix).
    assert not list(tmp_path.iterdir())

    def _boom(*_a: Any, **_kw: Any) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)
    reason = _workdir_root_unusable_reason(str(tmp_path))
    assert reason is not None
    assert "PermissionError" in reason


async def test_sandbox_fails_closed_when_shared_root_present_but_unwritable(tmp_path: Path, monkeypatch) -> None:
    """存在但不可写的共享根 → 统一话术，且**不得**泄漏 errno / 宿主路径。"""
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    spec = cat.get("python_sandbox")
    assert spec is not None
    root = tmp_path / "shared"
    root.mkdir()
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(root))
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", "http://sandbox-executor:8091")

    def _boom(*_a: Any, **_kw: Any) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)

    res = await SkillRuntime().run(spec, entry_script="runner.py", input_data={})

    assert res.ok is False
    assert res.error is not None
    # Assert the **preflight gate** wording, not the mkdtemp fallback; removing the gate would otherwise falsely pass via fallback.
    assert "共享工作目录不可用" in res.error
    # These three checks are the regressions to prevent.
    assert "Errno" not in res.error
    assert "PermissionError" not in res.error
    assert str(root) not in res.error


async def test_non_sandbox_skill_workdir_failure_does_not_leak_errno(tmp_path: Path, monkeypatch) -> None:
    """mkdtemp 兜底不限 skill：其余 skill 也不许把 errno 甩给用户（话术中性）。"""
    _mk_skill(tmp_path, "consultant_deck", script="run.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    spec = cat.get("consultant_deck")
    assert spec is not None
    monkeypatch.delenv("BOWHEAD_SKILL_WORKDIR_ROOT", raising=False)
    monkeypatch.delenv("BOWHEAD_INSTALL_DIR", raising=False)

    def _boom(*_a: Any, **_kw: Any) -> str:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)

    res = await SkillRuntime().run(spec, entry_script="run.py", input_data={})

    assert res.ok is False
    assert res.error is not None
    assert "Errno" not in res.error
    assert "No space left" not in res.error
    # The python_sandbox-specific "需手动计算" wording must not appear for other skills.
    assert "手动计算" not in res.error


@pytest.mark.skipif(os.geteuid() == 0, reason="root 绕过权限位，chmod 0o555 对它无效 → 会假绿")
async def test_sandbox_fails_closed_on_real_eacces(tmp_path: Path, monkeypatch) -> None:
    """Exercise a real read-only directory when the test process is not root."""
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    spec = cat.get("python_sandbox")
    assert spec is not None
    root = tmp_path / "ro-shared"
    root.mkdir()
    root.chmod(0o555)
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(root))
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_URL", "http://sandbox-executor:8091")
    try:
        res = await SkillRuntime().run(spec, entry_script="runner.py", input_data={})
    finally:
        root.chmod(0o755)  # Allow tmp_path cleanup.

    assert res.ok is False
    assert res.error is not None
    assert "共享工作目录不可用" in res.error
    assert "Errno" not in res.error
