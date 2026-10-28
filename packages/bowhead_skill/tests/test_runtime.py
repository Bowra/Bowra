"""SkillRuntime 沙箱子进程测试."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_skill import RunLimits, SkillRuntime, load_skill

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _mk_skill(root: Path, name: str, script_body: str) -> Path:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\nbody\n",
        encoding="utf-8",
    )
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "entry.py").write_text(script_body, encoding="utf-8")
    return d


ENTRY_ECHO = """
import argparse, json, sys
p = argparse.ArgumentParser()
p.add_argument("--input"); p.add_argument("--workdir"); p.add_argument("--output")
a = p.parse_args()
data = json.loads(open(a.input).read())
out = {"got": data, "argv": sys.argv[1:]}
open(a.workdir + "/result.json", "w").write(json.dumps(out))
open(a.workdir + "/hello.txt", "w").write("hello")
print("done")
"""


ENTRY_HANG = """
import time
while True: time.sleep(1)
"""


ENTRY_CRASH = """
import sys
sys.exit(2)
"""


async def test_run_happy_path(tmp_path: Path) -> None:
    d = _mk_skill(tmp_path, "hello", ENTRY_ECHO)
    spec = load_skill(d)
    rt = SkillRuntime()
    r = await rt.run(spec, entry_script="entry.py", input_data={"x": 1})
    assert r.ok
    assert r.exit_code == 0
    assert r.result_json["got"] == {"x": 1}
    assert any(p.name == "hello.txt" for p in r.outputs)
    assert "done" in r.stdout


async def test_run_crash(tmp_path: Path) -> None:
    d = _mk_skill(tmp_path, "crash", ENTRY_CRASH)
    spec = load_skill(d)
    r = await SkillRuntime().run(spec, entry_script="entry.py", input_data={})
    assert not r.ok
    assert r.exit_code == 2
    assert r.error and "exit_code=2" in r.error


async def test_run_wall_timeout(tmp_path: Path) -> None:
    d = _mk_skill(tmp_path, "hang", ENTRY_HANG)
    spec = load_skill(d)
    rt = SkillRuntime(limits=RunLimits(cpu_seconds=5, max_memory_mb=256, wall_timeout_sec=2))
    r = await rt.run(spec, entry_script="entry.py", input_data={})
    assert not r.ok
    assert r.error and "wall_timeout" in r.error


async def test_sandbox_wall_timeout_does_not_claim_remote_execution_stopped(tmp_path: Path) -> None:
    d = _mk_skill(tmp_path, "python_sandbox", ENTRY_HANG)
    rt = SkillRuntime(limits=RunLimits(cpu_seconds=5, max_memory_mb=256, wall_timeout_sec=1))
    result = await rt.run(load_skill(d), entry_script="entry.py", input_data={})
    assert not result.ok
    assert result.result_json["cause"] == "skill_wall_timeout"
    assert result.result_json["execution_state"] == "unknown"
    assert result.result_json["retry_safe"] is False
    assert "Check the original run" in result.error


async def test_entry_path_traversal_blocked(tmp_path: Path) -> None:
    d = _mk_skill(tmp_path, "safe", ENTRY_ECHO)
    spec = load_skill(d)
    rt = SkillRuntime()
    r = await rt.run(spec, entry_script="../../../etc/passwd", input_data={})
    assert not r.ok
    assert r.error and "escapes skill dir" in r.error


async def test_env_isolation(tmp_path: Path) -> None:
    """skill 不能看到 ANTHROPIC_API_KEY 等敏感变量."""
    import os

    os.environ["ANTHROPIC_API_KEY"] = "leakme"
    os.environ["BOWHEAD_SECRET"] = "leakme2"
    try:
        script = (
            "import os, json, argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--input'); p.add_argument('--workdir')\n"
            "a = p.parse_args()\n"
            "open(a.workdir + '/result.json', 'w').write(json.dumps({\n"
            "  'has_anth': 'ANTHROPIC_API_KEY' in os.environ,\n"
            "  'has_bow': 'BOWHEAD_SECRET' in os.environ,\n"
            "}))\n"
        )
        d = _mk_skill(tmp_path, "env", script)
        spec = load_skill(d)
        r = await SkillRuntime().run(spec, entry_script="entry.py", input_data={})
        assert r.ok
        assert r.result_json["has_anth"] is False
        assert r.result_json["has_bow"] is False
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("BOWHEAD_SECRET", None)


def test_build_env_passes_runner_config_but_not_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner gets its gateway/dependency configuration; credentials stay private.

    Runtime/resource limits belong to the executor. The old forwarding assertions
    were already failing on HEAD after that ownership change.
    """
    monkeypatch.setenv("BOWHEAD_ENV", "prod")
    monkeypatch.setenv("BOWHEAD_SANDBOX_WHEELHOUSE", "/var/lib/bowhead/wheelhouse")
    monkeypatch.setenv("BOWHEAD_GATEWAY_URL", "http://gw:8080")
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", "topsecret")
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "topsecret2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")

    env = SkillRuntime._build_env("python_sandbox")

    # Retained runner configuration reaches only the trusted transport.
    assert env.get("BOWHEAD_ENV") == "prod"  # Review P2: otherwise runner _is_prod is always False.
    assert env.get("BOWHEAD_SANDBOX_WHEELHOUSE") == "/var/lib/bowhead/wheelhouse"
    assert env.get("BOWHEAD_GATEWAY_URL") == "http://gw:8080"
    # Secrets are still stripped; no security regression.
    assert "BOWHEAD_SANDBOX_EXECUTOR_TOKEN" not in env
    assert "BOWHEAD_JWT_SECRET" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_build_env_uses_installed_compose_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "bowhead"
    (install_dir / "wheelhouse").mkdir(parents=True)
    monkeypatch.setenv("BOWHEAD_INSTALL_DIR", str(install_dir))
    monkeypatch.delenv("BOWHEAD_SANDBOX_WHEELHOUSE", raising=False)

    env = SkillRuntime._build_env("python_sandbox")

    assert env["BOWHEAD_SANDBOX_WHEELHOUSE"] == str(install_dir / "wheelhouse")


def test_build_env_sandbox_config_scoped_to_python_sandbox_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review P2: _build_env 服务所有 skill；sandbox 编排配置**只对 python_sandbox** 放行
    （其它 skill 的子进程 = skill 代码本身，含外部/不受信）。别把网关 URL/沙箱拓扑漏给它们。"""
    monkeypatch.setenv("BOWHEAD_ENV", "prod")
    monkeypatch.setenv("BOWHEAD_GATEWAY_URL", "http://gw:8080")

    other = SkillRuntime._build_env("docx")  # Any non-sandbox skill.
    assert "BOWHEAD_GATEWAY_URL" not in other
    assert "BOWHEAD_ENV" not in other

    sandbox = SkillRuntime._build_env("python_sandbox")
    assert sandbox.get("BOWHEAD_GATEWAY_URL") == "http://gw:8080"
    assert sandbox.get("BOWHEAD_ENV") == "prod"


async def test_cancelled_sandbox_keeps_executor_marker_outside_its_bind(tmp_path, monkeypatch):
    import asyncio

    import pytest

    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(root))
    skill = load_skill(_mk_skill(tmp_path, "python_sandbox", ENTRY_HANG))
    task = asyncio.create_task(SkillRuntime(keep_workdir=False).run(skill, "entry.py", {}))
    for _ in range(100):
        if list(root.glob("bow-skill-*/input.json")):
            break
        await asyncio.sleep(0.01)
    workdir = next(root.glob("bow-skill-*"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert workdir.exists()
    assert workdir.with_name(f".{workdir.name}.cancelled").is_file()
    assert not (workdir / f".{workdir.name}.cancelled").exists()
