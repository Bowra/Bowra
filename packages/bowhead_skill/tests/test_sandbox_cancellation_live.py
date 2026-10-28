"""Opt-in real Docker acceptance; requires an explicitly configured local image."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from contextlib import suppress
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from bowhead_skill.runtime import _request_sandbox_cancel
from test_sandbox_executor_bootstrap import _load_executor

from bowhead_skill import RunLimits, SkillRuntime, load_skill
from bowhead_skill import runtime as skill_runtime

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


@pytest.mark.parametrize("scenario", ["success", "error", "timeout", "cancel", "forged_marker"])
async def test_real_container_lifecycle(tmp_path, monkeypatch, scenario):
    image = os.environ.get("BOWHEAD_LIVE_SANDBOX_IMAGE")
    if not image:
        pytest.skip("Set BOWHEAD_LIVE_SANDBOX_IMAGE to an existing local image with Python")
    docker = shutil.which("docker")
    assert docker is not None
    inspected = subprocess.run([docker, "image", "inspect", image], check=True, capture_output=True)
    image_id = json.loads(inspected.stdout)[0]["Id"]
    mod = _load_executor()
    workdir = tmp_path / "run-1"
    workdir.mkdir(mode=0o777)
    workdir.chmod(0o777)
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(tmp_path))
    monkeypatch.setenv("BOWHEAD_SANDBOX_IMAGE", image)
    monkeypatch.setenv("BOWHEAD_SANDBOX_RUNTIME", "runc")
    code = "from pathlib import Path\nimport time,sys\nPath('started.txt').write_text('started')\n"
    if scenario in {"cancel", "timeout"}:
        code += "time.sleep(20)\n"
    elif scenario == "error":
        code += "sys.exit(7)\n"
    elif scenario == "forged_marker":
        code += "Path('.run-1.cancelled').touch()\n"
    code += "Path('answer.txt').write_text('completed')\n"
    (workdir / "user.py").write_text(code)
    task = asyncio.create_task(asyncio.to_thread(mod._run, {"workdir": str(workdir), "timeout": 5 if scenario == "timeout" else 15}))
    if scenario == "cancel":
        for _ in range(200):
            if (workdir / "started.txt").exists() or task.done():
                break
            await asyncio.sleep(0.05)
        assert (workdir / "started.txt").exists(), await task
        _request_sandbox_cancel(workdir)
    result = await asyncio.wait_for(task, 25)
    if scenario in {"success", "forged_marker"}:
        assert result["ok"] is True, result
        assert (workdir / "answer.txt").read_text() == "completed"
        assert result["execution"] == {
            "contract": "sandbox-execution.v1",
            "id": result["execution"]["id"],
            "image_id": image_id,
            "runtime": "runc",
            "network": "none",
            "settled": True,
        }
        assert result["execution"]["id"].startswith("bowhead-sandbox-")
    else:
        assert result["ok"] is False, result
        assert result["error"] == {"cancel": "cancelled", "timeout": "timeout after 5s", "error": "exit_code=7"}[scenario]
        assert not (workdir / "answer.txt").exists()
        assert "execution" not in result
    if scenario == "cancel":
        assert result["duration_ms"] < 5000
        assert mod._run({"workdir": str(workdir)})["error"] == "cancelled"
    # The accepted invocation must not leave a running container or a created
    # container that a delayed start could execute later.
    containers = subprocess.run([docker, "ps", "-aq", "--filter", f"volume={workdir}"], check=True, capture_output=True, text=True)
    assert not containers.stdout.strip()


@pytest.mark.parametrize("stop", ["cancel", "host_timeout"])
async def test_real_runner_disconnect_stops_container(tmp_path, monkeypatch, stop):
    image = os.environ.get("BOWHEAD_LIVE_SANDBOX_IMAGE")
    if not image:
        pytest.skip("Set BOWHEAD_LIVE_SANDBOX_IMAGE to an existing local image with Python")
    monkeypatch.setenv("BOWHEAD_SKILL_WORKDIR_ROOT", str(tmp_path))
    monkeypatch.setenv("BOWHEAD_SANDBOX_IMAGE", image)
    monkeypatch.setenv("BOWHEAD_SANDBOX_RUNTIME", "runc")
    monkeypatch.setenv("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", "local-acceptance-only")
    mod = _load_executor()
    finished = threading.Event()
    results = []
    real_run = mod._run

    def observed_run(payload):
        try:
            result = real_run(payload)
            results.append(result)
            return result
        finally:
            finished.set()

    monkeypatch.setattr(mod, "_run", observed_run)
    # Docker Desktop maps this host's UID differently from the production
    # app/sandbox UID 1000. Only this test's isolated workdir needs adjustment.
    real_mkdtemp = skill_runtime.tempfile.mkdtemp

    def writable_workdir(*args, **kwargs):
        directory = real_mkdtemp(*args, **kwargs)
        Path(directory).chmod(0o777)
        return directory

    monkeypatch.setattr(skill_runtime.tempfile, "mkdtemp", writable_workdir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    spec = load_skill(Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox")
    runtime = SkillRuntime(keep_workdir=False, limits=RunLimits(wall_timeout_sec=3 if stop == "host_timeout" else 30))
    task = asyncio.create_task(
        runtime.run(
            spec,
            "runner.py",
            {
                "code": "from pathlib import Path\nimport time\nPath('started.txt').touch()\ntime.sleep(20)\nPath('after-stop.txt').touch()",
                "timeout_sec": 25,
                "_sandbox_executor_url": f"http://127.0.0.1:{server.server_port}",
                "_sandbox_executor_token": "local-acceptance-only",
            },
        )
    )
    try:
        started = []
        for _ in range(200):
            started = list(tmp_path.glob("bow-skill-*/started.txt"))
            if started or task.done():
                break
            await asyncio.sleep(0.05)
        assert len(started) == 1, task.result() if task.done() else "Container did not start"
        workdir = started[0].parent
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, 10)
            assert result.ok is False and result.error == "wall_timeout after 3s"
        assert await asyncio.to_thread(finished.wait, 10)
        assert results[0]["error"] == "cancelled", results
        assert workdir.exists()
        assert workdir.with_name(f".{workdir.name}.cancelled").exists()
        assert not (workdir / "after-stop.txt").exists()
        docker = shutil.which("docker")
        assert docker is not None
        containers = subprocess.run([docker, "ps", "-aq", "--filter", f"volume={workdir}"], check=True, capture_output=True, text=True)
        assert not containers.stdout.strip()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
