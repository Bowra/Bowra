"""SkillRuntime — 在受限子进程里跑 skill scripts.

production 约束：
- 子进程跑 python3，不走 shell（防命令注入）
- CPU + 地址空间 rlimit（SIGKILL 防暴走）
- 硬 timeout（subprocess 到时 terminate → kill）
- 工作目录隔离（tempdir，跑完清理）
- 环境变量白名单（屏蔽 AWS_* 等 cred）
- 输入通过 input.json 文件（而不是 stdin，方便 skill 脚本 debug）
- 输出通过 workdir 里的新文件扫描 + entry 脚本返回 JSON (result.json)

调用约定（entry script 约定）：
    python3 <skill_dir>/scripts/render_from_canvas.py \\
            --input <workdir>/input.json \\
            --workdir <workdir> \\
            --output <workdir>/output.<ext>

Bowhead 自带的 entry 脚本遵循这个约定；外部 skill 不遵循时调用方自己 wrap。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_skill.loader import SkillSpec

logger = logging.getLogger(__name__)


# Block sensitive environment variable prefixes.
_ENV_DENY_PREFIXES = (
    "AWS_",
    "GCP_",
    "AZURE_",
    "ANTHROPIC_",
    "ALIYUN_",
    "OPENAI_",
    "DASHSCOPE",
    "BOWHEAD_",  # Skills must not see our own configuration either.
)
# Minimum set that must be retained.
_ENV_KEEP = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}
# python_sandbox runner.py is a trusted orchestrator, not user code; it needs only the gateway address,
# explicit development backend, and offline dependency directory. sandbox-executor owns container images, runtimes, and resource limits;
# do not pass them to the application-side runner. EXECUTOR_URL/TOKEN remain blocked; tools.py instead injects them as one-shot
# control fields that are never written into the container-visible input.json.
# **Allow only for the python_sandbox skill** (self-review P2): _build_env serves all skills, and other skills' subprocesses
# **are the skill code itself** (including external/untrusted skills). Only python_sandbox launches the trusted
# runner.py. Unconditional allowance exposes the gateway URL + sandbox topology to all skill code, weakening the BOWHEAD_ deny
# policy. Gate by skill name instead (see _build_env).
_SANDBOX_SKILL_NAME = "python_sandbox"
_ENV_ALLOW_EXACT = {
    "BOWHEAD_ENV",  # Production detection; otherwise runner _is_prod stays False and production stdio scrubbing fails (review P2).
    "BOWHEAD_GATEWAY_URL",  # The runner calls back to the gateway to fetch artifacts.
    "BOWHEAD_SANDBOX_BACKEND",  # executor | subprocess (development/CI only)
    "BOWHEAD_SANDBOX_WHEELHOUSE",  # Offline, hash-pinned long-tail dependencies.
}


def _deployment_sandbox_defaults() -> dict[str, str]:
    """Expose the installed wheelhouse to the trusted runner when it exists."""
    install_dir = (os.environ.get("BOWHEAD_INSTALL_DIR") or "").strip()
    if not install_dir:
        return {}

    defaults: dict[str, str] = {}
    wheelhouse = Path(install_dir) / "wheelhouse"
    if wheelhouse.is_dir():
        defaults["BOWHEAD_SANDBOX_WHEELHOUSE"] = str(wheelhouse)
    return defaults


# feedback-0713 #4b (related): workdir reaper. keep_workdir=True + a new mkdtemp directory on every call
# makes BOWHEAD_SKILL_WORKDIR_ROOT (production shared bind mount) grow without bound; it once filled the disk on 116.
# Clean by age: default TTL 24h (far longer than wall_timeout and the post-delivery output consumption
# window; host-path consumers such as consultant-deck read within the same turn). Throttle each process
# to one scan every 10min; delete only this runtime's `bow-skill-` directories. Set TTL to 0 to disable.
_REAP_INTERVAL_SEC = 600.0
_reap_last_ts = 0.0


def _reap_stale_workdirs(root: Path, ttl_hours: float) -> int:
    """删除 root 下超过 ttl 的 bow-skill-* workdir。best-effort, 永不 raise。"""
    import time as _time

    removed = 0
    try:
        cutoff = _time.time() - ttl_hours * 3600
        for d in root.iterdir():
            try:
                if not d.is_dir() or not d.name.startswith("bow-skill-"):
                    continue
                if d.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(d, ignore_errors=True)
                if not d.exists():
                    d.with_name(f".{d.name}.cancelled").unlink(missing_ok=True)
                removed += 1
            except OSError:  # noqa: PERF203
                continue
    except OSError:
        return removed
    if removed:
        logger.info("skill_runtime.workdir_reaper removed=%d root=%s", removed, root)
    return removed


def _maybe_reap_workdirs(root: Path) -> None:
    """节流入口——每进程最多每 _REAP_INTERVAL_SEC 扫一次。"""
    global _reap_last_ts
    import time as _time

    try:
        ttl = float(os.environ.get("BOWHEAD_SKILL_WORKDIR_TTL_HOURS", "24") or "24")
    except ValueError:
        ttl = 24.0
    if ttl <= 0:
        return
    # review nit: minimum TTL 1h prevents an accidental minute-scale setting from deleting active workdirs
    # (wall_timeout is 120s; 1h still leaves ample margin).
    ttl = max(ttl, 1.0)
    now = _time.monotonic()
    if now - _reap_last_ts < _REAP_INTERVAL_SEC:
        return
    _reap_last_ts = now
    # review P2: offload scanning/rmtree to the thread pool; a first deployment scan with weeks of backlog would otherwise
    # block the gateway event loop for seconds (stalling SSE). Fire-and-forget; retry failures on the next pass.
    try:
        loop = asyncio.get_running_loop()
        _task = loop.run_in_executor(None, _reap_stale_workdirs, root, ttl)
    except RuntimeError:
        # No event loop (synchronous callers/tests) → run inline.
        _reap_stale_workdirs(root, ttl)


def _resolve_workdir_root() -> str | None:
    """Resolve a workdir root visible at the same absolute path to app and executor.

    The app creates the workdir before passing its host path to the executor. A
    container-private temporary path cannot satisfy that contract. Prefer the
    explicit BOWHEAD_SKILL_WORKDIR_ROOT; otherwise use an existing sandbox directory
    under BOWHEAD_INSTALL_DIR. Return None when no supported shared root resolves."""
    root = (os.environ.get("BOWHEAD_SKILL_WORKDIR_ROOT") or "").strip()
    if root:
        return root
    install_dir = (os.environ.get("BOWHEAD_INSTALL_DIR") or "").strip()
    if install_dir:
        candidate = Path(install_dir) / "sandbox"
        if candidate.is_dir():
            logger.info(
                "skill_runtime.workdir_root_fallback using %s from BOWHEAD_INSTALL_DIR",
                candidate,
            )
            return str(candidate)
    return None


def _workdir_root_unusable_reason(root: str) -> str | None:
    """Return None for a usable shared root, otherwise a diagnostic reason.

    Directory existence does not imply write access. The application runs as an
    unprivileged user and cannot repair a root-owned bind mount; executor startup
    owns that repair. Creating and deleting a temporary directory is the reliable
    check across containers, NFS, and read-only mounts."""
    try:
        probe = tempfile.mkdtemp(prefix=".bow-probe-", dir=root)
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    with contextlib.suppress(OSError):
        Path(probe).rmdir()
    return None


def _executor_configured() -> bool:
    """de-DooD executor 路是否启用（app 不持 docker.sock，只能请 executor 跑）。"""
    return bool((os.environ.get("BOWHEAD_SANDBOX_EXECUTOR_URL") or "").strip())


def _wall_timeout_default() -> int:
    """Use the deployment override, otherwise the sandbox ceiling plus I/O margin."""
    from bowhead_skill.execution_policy import skill_wall_timeout_s

    return skill_wall_timeout_s()


@dataclass
class RunLimits:
    cpu_seconds: int = 60  # Each skill gets at most 60s of CPU time.
    max_memory_mb: int = 1024  # 1 GB of address space.
    wall_timeout_sec: int = field(default_factory=_wall_timeout_default)  # Wall-clock timeout (configurable through the environment; see above).
    max_output_bytes: int = 50 * 1024 * 1024  # Max 50 MB/file for large datasets; 10 MB blocked a 23MB EA document CSV.


@dataclass
class RunResult:
    skill: str
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    result_json: dict[str, Any] = field(default_factory=dict)
    outputs: list[Path] = field(default_factory=list)  # Newly generated files.
    workdir: Path | None = None  # When keep_workdir=True.
    duration_ms: int = 0
    error: str | None = None


def _request_sandbox_cancel(workdir: Path) -> None:
    """Signal the executor outside the container's writable mount.

    Keep the marker and workdir until normal retention cleanup so a delayed
    executor request cannot lose the cancellation fence.
    """
    try:
        workdir.with_name(f".{workdir.name}.cancelled").touch(exist_ok=True)
    except OSError:
        logger.warning("skill_runtime.remote_cancellation_unconfirmed")


@dataclass
class SkillRuntime:
    python_bin: str = field(default_factory=lambda: sys.executable)
    limits: RunLimits = field(default_factory=RunLimits)
    # True = retain workdir so callers can read outputs; False = clean up automatically.
    keep_workdir: bool = True

    async def run(
        self,
        spec: SkillSpec,
        entry_script: str,
        input_data: dict[str, Any],
        extra_args: list[str] | None = None,
    ) -> RunResult:
        """在沙箱里跑 skill 的一个 entry script.

        entry_script: 相对于 spec.script_dir 的 .py 文件名（防止路径穿越）
        input_data: 序列化到 workdir/input.json 供脚本读取
        extra_args: 追加 CLI args（调用方可按 skill 约定传 --output 等）
        """
        import time

        script_path = (spec.script_dir / entry_script).resolve()
        preserve_cancelled_workdir = False
        try:
            script_path.relative_to(spec.script_dir.resolve())
        except ValueError:
            return RunResult(
                skill=spec.name,
                ok=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=f"entry_script '{entry_script}' escapes skill dir",
            )
        if not script_path.is_file():
            return RunResult(
                skill=spec.name,
                ok=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=f"entry_script not found: {script_path}",
            )
        # skill-multiruntime S1 (D6): the host path executes only .py. Non-.py (.sh/.js/.mjs) user
        # skills are rewritten by tools.py to execute in python_sandbox containers; host-side Node dispatch was removed.
        # All built-in skills are .py and remain unaffected.
        if script_path.suffix.lower() != ".py":
            return RunResult(
                skill=spec.name,
                ok=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=(f"host runtime executes only .py entry scripts (got {script_path.suffix}); non-python skills run in the sandbox"),
            )

        # DooD fix (2026-04-28): bowhead-app is a container in production; python_sandbox uses
        # docker-outside-of-docker to spawn a sibling container. The host Docker daemon interprets the sibling's -v {workdir}:/workdir
        # as a host path. The default /tmp in bowhead-app is a different mount from the host's
        # → the sibling cannot see user.py → exit_code=2.
        # Solution: point BOWHEAD_SKILL_WORKDIR_ROOT to a shared bind mount visible to both host and container.
        # (compose: /var/lib/bowhead/sandbox:/var/lib/bowhead/sandbox).
        _workdir_root = _resolve_workdir_root()
        # An unresolved shared root + an enabled executor path means this call **will fail** (the executor accepts only
        # real directories under its own allowed root; the container's private /tmp does not exist there). Rather than
        # waste an HTTP round trip and expose `workdir rejected: [Errno 2] ...` to end users,
        # fail closed locally: users receive consistent "sandbox not ready" wording; operations logs identify actionable configuration keys.
        # Apply only to python_sandbox: other skills run subprocesses inside the app container (consultant-deck
        # and others still consume outputs via host paths), never use the executor, and must not be blocked by this gate.
        # A present root-owned 0755 directory remains unusable by uid 1000; reject it before exposing raw filesystem errors.
        if spec.name == _SANDBOX_SKILL_NAME and _executor_configured():
            _unusable = (
                "BOWHEAD_SKILL_WORKDIR_ROOT 未配置且 BOWHEAD_INSTALL_DIR/sandbox 不存在"
                if _workdir_root is None
                else _workdir_root_unusable_reason(_workdir_root)
            )
            if _unusable is not None:
                logger.error(
                    "skill_runtime.workdir_root_unusable skill=%s root=%s reason=%s — executor 路要求 "
                    "workdir 位于 app 与 executor 同路径挂载、且 app（uid 1000）可写的共享 bind mount 上。"
                    "该目录由 sandbox-executor 启动时建立并置 1777；若此处报权限错，通常是 executor "
                    "镜像过旧（缺 _ensure_workdir_root）或其 chmod 被宿主策略拒绝，请一并升级 "
                    "BOWHEAD_SANDBOX_EXECUTOR_VERSION 后重建两个容器。",
                    spec.name,
                    _workdir_root,
                    _unusable,
                )
                return RunResult(
                    skill=spec.name,
                    ok=False,
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    error="暂不支持代码执行 (sandbox 未就绪: 共享工作目录不可用), 该问题需手动计算",
                )
        # feedback-0713 #4b: reap old workdirs under the shared bind-mount root by TTL
        # (keep_workdir=True never cleaned them, once filling the disk on 116). Leave system /tmp to the OS.
        if _workdir_root:
            _maybe_reap_workdirs(Path(_workdir_root))
        # Final defense: this should not fail after the two gates above, but it is the only place that ensures
        # errno + host paths never reach end users under any circumstances. Apply to all skills: inability to create a workdir
        # is a hard failure for any skill; only the wording differs.
        try:
            workdir = Path(
                tempfile.mkdtemp(prefix=f"bow-skill-{spec.name}-", dir=_workdir_root),
            )
        except OSError as e:
            logger.error(
                "skill_runtime.workdir_create_failed skill=%s root=%s err=%s",
                spec.name,
                _workdir_root,
                e,
            )
            return RunResult(
                skill=spec.name,
                ok=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=(
                    "暂不支持代码执行 (sandbox 未就绪: 工作目录创建失败), 该问题需手动计算"
                    if spec.name == _SANDBOX_SKILL_NAME
                    else "skill 运行失败: 工作目录创建失败"
                ),
            )
        try:
            (workdir / "input.json").write_text(
                json.dumps(input_data, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            # skill-multiruntime S1 (D6): the host runs only .py (ensured by the guard above); Node dispatch was removed.
            runner_bin = self.python_bin
            args = [
                runner_bin,
                str(script_path),
                "--input",
                str(workdir / "input.json"),
                "--workdir",
                str(workdir),
            ]
            if extra_args:
                args.extend(extra_args)

            env = self._build_env(spec.name)

            t0 = time.perf_counter()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workdir),
                    env=env,
                    preexec_fn=_apply_rlimits(self.limits) if sys.platform != "win32" else None,
                )
            except FileNotFoundError as e:
                return RunResult(
                    skill=spec.name,
                    ok=False,
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    error=f"spawn_failed: {e}",
                    workdir=workdir if self.keep_workdir else None,
                )

            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.limits.wall_timeout_sec,
                )
            except asyncio.CancelledError:
                # A call-scoped source copy cannot be removed while its host
                # process is still using it. Reap the child before propagating.
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                await asyncio.shield(proc.wait())
                raise
            except TimeoutError:
                timed_out = True
                if spec.name == _SANDBOX_SKILL_NAME:
                    preserve_cancelled_workdir = True
                    _request_sandbox_cancel(workdir)
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                stdout_b, stderr_b = b"", b"(killed by wall_timeout)"
            dur_ms = int((time.perf_counter() - t0) * 1000)

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")

            # result.json (optional; skill scripts can provide a custom return structure).
            result_json: dict[str, Any] = {}
            rj = workdir / "result.json"
            if rj.is_file():
                try:
                    result_json = json.loads(rj.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    logger.warning("skill_runtime.result_json_parse err=%s", e)

            outputs = _scan_outputs(workdir, self.limits.max_output_bytes)

            exit_code = proc.returncode if proc.returncode is not None else -1
            ok = exit_code == 0 and not timed_out
            err: str | None = None
            if timed_out:
                err = f"wall_timeout after {self.limits.wall_timeout_sec}s"
                if spec.name == _SANDBOX_SKILL_NAME:
                    err += "; cancellation requested but executor cleanup is unconfirmed. Check the original run before retrying."
                    result_json = {
                        **result_json,
                        "ok": False,
                        "error": err,
                        "cause": "skill_wall_timeout",
                        "execution_state": "unknown",
                        "retry_safe": False,
                    }
            elif exit_code != 0:
                err = f"exit_code={exit_code}"

            logger.info(
                "skill_runtime.run skill=%s ok=%s exit=%d dur=%dms outputs=%d",
                spec.name,
                ok,
                exit_code,
                dur_ms,
                len(outputs),
            )

            return RunResult(
                skill=spec.name,
                ok=ok,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                result_json=result_json,
                outputs=outputs,
                workdir=workdir if self.keep_workdir else None,
                duration_ms=dur_ms,
                error=err,
            )
        except asyncio.CancelledError:
            # Include cancellation during process creation: a runner that has
            # already spawned must not start a delayed remote request.
            if spec.name == _SANDBOX_SKILL_NAME:
                preserve_cancelled_workdir = True
                _request_sandbox_cancel(workdir)
            raise
        finally:
            if not self.keep_workdir and not preserve_cancelled_workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _build_env(skill_name: str = "") -> dict[str, str]:
        """环境变量白名单 + 去敏感.

        skill_name: F6——sandbox 编排配置只对 python_sandbox（其子进程=受信 runner.py）放行；
        其它 skill 的子进程是 skill 代码本身，保持 BOWHEAD_ 全 deny。
        """
        allow_sandbox_cfg = skill_name == _SANDBOX_SKILL_NAME
        env = {}
        for k, v in os.environ.items():
            if k in _ENV_KEEP:
                env[k] = v
                continue
            # F6: explicitly allow sandbox orchestration configuration before the BOWHEAD_ deny check, **only for python_sandbox** (see comments).
            if allow_sandbox_cfg and k in _ENV_ALLOW_EXACT:
                env[k] = v
                continue
            if any(k.startswith(p) for p in _ENV_DENY_PREFIXES):
                continue
            # Retain nonsensitive values (e.g. HOME, SHELL are already in KEEP or pass naturally).
            if k.isupper() and "_" in k:
                # Permissive policy: pass non-DENY variables only when uppercase and containing an underscore.
                env[k] = v
        # Also retain PATH (some skills depend on external binaries such as LibreOffice).
        env.setdefault("PATH", os.environ.get("PATH", ""))
        if allow_sandbox_cfg:
            for key, value in _deployment_sandbox_defaults().items():
                env.setdefault(key, value)
        return env


def _apply_rlimits(limits: RunLimits):
    """preexec_fn — 子进程起来后立即设 rlimit."""

    def _set():
        try:
            import resource

            resource.setrlimit(
                resource.RLIMIT_CPU,
                (limits.cpu_seconds, limits.cpu_seconds),
            )
            # executor runs containers in a separate service, so the runner never
            # needs to launch a Docker CLI. Keep both limits bounded: the explicit
            # subprocess development fallback must not be able to raise its own
            # address-space limit.
            resource.setrlimit(
                resource.RLIMIT_AS,
                (limits.max_memory_mb * 1024 * 1024, limits.max_memory_mb * 1024 * 1024),
            )
        except Exception:  # noqa: S110
            pass

    return _set


def _scan_outputs(workdir: Path, max_bytes: int) -> list[Path]:
    """workdir 里的新文件（排除 input.json / result.json）."""
    out: list[Path] = []
    for p in sorted(workdir.rglob("*")):
        if not p.is_file():
            continue
        if p.name in ("input.json", "result.json"):
            continue
        # skill-multiruntime (review-P2): staged skill bundles + control/scaffold files are not
        # outputs; do not leak the entire skill/ tree + user.py + skill_manifest/skill_result into output metadata.
        rel = p.relative_to(workdir)
        if (rel.parts and rel.parts[0] == "skill") or p.name in ("user.py", "skill_manifest.json", "skill_result.json"):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            logger.warning(
                "skill_runtime.output_too_large path=%s size=%d cap=%d",
                p,
                size,
                max_bytes,
            )
            continue
        out.append(p)
    return out
