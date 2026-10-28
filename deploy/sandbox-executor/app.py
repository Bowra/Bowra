"""Bowhead sandbox-executor — de-DooD trusted docker-run gate.

sandbox-hardening 2026-06-17, Phase 3. See docs/sprints/sandbox-hardening-2026-06-17/
tech-design.md §3.

WHY: today the gateway container mounts /var/run/docker.sock + is in the docker group
(DooD) — a gateway-app RCE can `docker run -v /:/host` → host root. A docker-socket-proxy
does NOT help (it gates API endpoints, not the create-request body, so `-v /:/host` still
goes through). This executor is the correct de-DooD: it is the ONLY component with the
docker socket, exposes a SINGLE narrow endpoint, and HARDCODES every safety flag. The
caller (the gateway's python_sandbox runner) cannot specify arbitrary binds, image, or
flags — it can only ask "run the sandbox image on workdir X (under the allowed root)".

Stdlib-only on purpose: this is the most security-sensitive process in the stack, so it
carries no third-party dependency / transitive-CVE surface and is trivial to audit.

Contract (POST /run, JSON):
  request : {workdir, requirements?, runtime?, tmpfs_mb?, memory_mb?, pids?, timeout?}
  headers : X-Executor-Token: <shared secret == BOWHEAD_SANDBOX_EXECUTOR_TOKEN>
  response: {ok, exit_code, stdout, stderr, duration_ms, error, execution?}

The executor NEVER fetches/uploads artifacts or scans outputs — that stays host-side in
the gateway's runner.py (which holds BOWHEAD_GATEWAY_URL + the artifact-fetch token). The
executor only runs the container. workdir + wheelhouse are shared host volumes mounted at
the SAME path in both gateway and executor (DooD sibling-mount path-alignment rule), so a
host path the gateway writes is resolvable by the docker daemon for the sibling `-v` mount.

Env:
  BOWHEAD_SANDBOX_EXECUTOR_TOKEN   shared secret (REQUIRED; refuses to start without it)
  BOWHEAD_SKILL_WORKDIR_ROOT       allowed workdir root (REQUIRED; workdir must be under it).
                                   The executor also CREATES this dir and grants it 1777 at
                                   startup (see _ensure_workdir_root) — the gateway runs as
                                   uid 1000 and cannot do it itself, and the image must not
                                   depend on operators having run `install -d -m 1777`.
  BOWHEAD_SANDBOX_IMAGE            required versioned image to run
  BOWHEAD_SANDBOX_WHEELHOUSE       host wheelhouse dir (RO mount when reqs present)
  BOWHEAD_SANDBOX_EXECUTOR_PORT    listen port (default 8091; bind 0.0.0.0 on private net)
  BOWHEAD_SANDBOX_MAX_TIMEOUT      hard cap on per-run timeout (default 600)
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── validators (IDENTICAL contract to runner._REQ_RE / sandbox_bootstrap._REQ_RE —
#    the codebase intentionally keeps these three copies in sync; see those files) ──
_REQ_RE = re.compile(r"^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$")
_ALLOWED_RUNTIMES = {"runc", "runsc"}
_MAX_BODY_BYTES = 256 * 1024  # request bodies are tiny; cap to refuse abuse
# /tmp semantics — the shared workdir root is written by three different uids
# (gateway 1000, executor root, sandbox container) with no common group.
_WORKDIR_ROOT_MODE = 0o1777


def _norm_req(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _wheelhouse_lockset(wheelhouse: str | None) -> set[str]:
    if not wheelhouse:
        return set()
    p = Path(wheelhouse) / "requirements.lock"
    if not p.is_file():
        return set()
    names: set[str] = set()
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("--"):
                continue
            head = line.split("\\", 1)[0].split(";", 1)[0].strip()
            m = re.match(r"^([A-Za-z0-9._-]+)", head)
            if m:
                names.add(_norm_req(m.group(1)))
    except Exception:
        return set()
    return names


def _validate_requirements(reqs: list, lockset: set[str]) -> list[str]:
    """Drop anything not matching the strict regex AND present in the lock-set
    (fail closed — never install an unvalidated/unlocked dep). Mirrors the runner
    pre-check + the in-container bootstrap re-validate.
    """
    valid: list[str] = []
    for req in reqs or []:
        r = str(req).strip()
        if not r or not _REQ_RE.match(r):
            continue
        if _norm_req(r.split("==", 1)[0]) in lockset:
            valid.append(r)
    return valid


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _validated_workdir(raw: str, root: str) -> Path:
    """Resolve workdir + assert it is strictly under the allowed root.

    SEC: the executor does NOT trust the caller-supplied path. It re-resolves
    realpath HERE (symlinks followed) and verifies containment, so a compromised
    gateway cannot point the bind at the host root / an escaping symlink. Raises
    ValueError on any violation.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("workdir required")
    root_real = Path(root).resolve(strict=True)
    wd_real = Path(raw).resolve(strict=True)  # strict → must already exist
    if not wd_real.is_dir():
        raise ValueError("workdir is not a directory")
    # strict containment: wd_real must be a PROPER subdirectory of root (never the
    # root itself). Rejecting wd==root stops a compromised gateway from passing the
    # shared workdir root as the mount → which would give one sandbox run RW access
    # to every other invocation's workdir (the gateway only ever creates unique
    # mkdtemp subdirs, so a legit workdir is always a proper child).
    if wd_real == root_real or root_real not in wd_real.parents:
        raise ValueError("workdir must be a proper subdirectory of the allowed root")
    return wd_real


def _build_docker_argv(
    *, workdir: Path, image: str, runtime: str, timeout: int,
    tmpfs_mb: int, memory_mb: int, pids: int,
    wheelhouse: str | None, requirements: list[str], container_name: str,
) -> list[str]:
    """Build the sandbox `docker create` argv. The safety flags are HARDCODED here —
    this is the whole point of the executor. The gateway has no corresponding
    Docker execution path.
    """
    cmd = [
        "docker", "create", "--name", container_name,
        f"--runtime={runtime}",
        "--network=none",
        "--read-only",
        "--tmpfs", f"/tmp:size={tmpfs_mb}m,exec",
        f"--memory={memory_mb}m",
        f"--memory-swap={memory_mb}m",
        "--cpus=1.0",
        f"--pids-limit={pids}",
        "--user", "1000:1000",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "-v", f"{workdir}:/workdir:rw",
    ]
    if requirements and wheelhouse:
        cmd += ["-v", f"{wheelhouse}:/wheelhouse:ro"]
        entry = ["python", "/opt/bowhead/sandbox_bootstrap.py"]
    else:
        entry = ["python", "user.py"]
    cmd += ["--workdir", "/workdir", image, *entry]
    return cmd


def _run(payload: dict) -> dict:
    root = os.environ.get("BOWHEAD_SKILL_WORKDIR_ROOT", "")
    if not root:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "error": "executor misconfigured: BOWHEAD_SKILL_WORKDIR_ROOT unset",
                "duration_ms": 0}
    try:
        workdir = _validated_workdir(payload.get("workdir", ""), root)
    except (ValueError, OSError) as e:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "error": f"workdir rejected: {e}", "duration_ms": 0}

    # SEC (executor is authoritative): runtime + image + resource limits come from
    # the EXECUTOR's OWN env — NEVER the request. A compromised gateway therefore
    # cannot downgrade runsc→runc, swap the image (container-escape), or relax the
    # safety flags. Only `timeout` (per-invoke, the LLM's timeout_sec) and the
    # validated workdir/requirements come from the caller. This also incidentally
    # fixes a latent gateway bug: the runner subprocess never received BOWHEAD_*
    # (stripped by SkillRuntime._build_env), so the prod tmpfs/mem/pids/runtime
    # bumps only actually take effect HERE, in the executor's full env.
    runtime = os.environ.get("BOWHEAD_SANDBOX_RUNTIME", "runc").strip() or "runc"
    if runtime not in _ALLOWED_RUNTIMES:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "",
                "error": f"executor BOWHEAD_SANDBOX_RUNTIME={runtime!r} not allowed",
                "duration_ms": 0}

    image = (os.environ.get("BOWHEAD_SANDBOX_IMAGE") or "").strip()
    if not image:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "error": "executor BOWHEAD_SANDBOX_IMAGE is required",
            "duration_ms": 0,
        }
    wheelhouse = (os.environ.get("BOWHEAD_SANDBOX_WHEELHOUSE") or "").strip() or None

    # re-validate requirements against the wheelhouse lock-set (caller is the
    # gateway, but defense-in-depth: never trust the list as-is)
    raw_reqs = payload.get("requirements") or []
    if not isinstance(raw_reqs, list):
        raw_reqs = []
    requirements = _validate_requirements(raw_reqs, _wheelhouse_lockset(wheelhouse))

    configured_max_timeout = _env_int("BOWHEAD_SANDBOX_MAX_TIMEOUT", 600)
    max_timeout = max(5, configured_max_timeout) if configured_max_timeout > 0 else 600
    try:
        req_timeout = int(payload.get("timeout") or 120)
    except (ValueError, TypeError):
        req_timeout = 120
    timeout = max(5, min(req_timeout, max_timeout))
    tmpfs_mb = _env_int("BOWHEAD_SANDBOX_TMPFS_MB", 64)
    memory_mb = _env_int("BOWHEAD_SANDBOX_MEMORY_MB", 512)
    pids = _env_int("BOWHEAD_SANDBOX_PIDS", 50)

    container_name = f"bowhead-sandbox-{secrets.token_hex(12)}"
    argv = _build_docker_argv(
        workdir=workdir, image=image, runtime=runtime, timeout=timeout,
        tmpfs_mb=tmpfs_mb, memory_mb=memory_mb, pids=pids,
        wheelhouse=wheelhouse, requirements=requirements,
        container_name=container_name,
    )
    # The sibling marker is outside the only writable sandbox bind mount.
    # Sandbox code can neither forge nor remove the application's cancellation.
    cancel_marker = workdir.with_name(f".{workdir.name}.cancelled")
    t0 = time.perf_counter()
    result = {"ok": False, "exit_code": -1, "stdout": "", "stderr": "", "error": None, "execution_state": "not_started", "timeout_sec": timeout}
    proc = None
    execution = None
    create_attempted = False
    try:
        if cancel_marker.exists():
            result["error"] = "cancelled"
        else:
            create_attempted = True
            created = subprocess.run(  # noqa: S603 — fixed container policy, no shell
                argv, capture_output=True, timeout=min(timeout, 10), text=True, check=False,
            )
            if created.returncode != 0:
                result.update(exit_code=created.returncode, stderr=created.stderr, error="container_create_failed", cause="container_create_failed")
            elif cancel_marker.exists():
                result["error"] = "cancelled"
            else:
                # Inspect the created container, not a mutable image tag. A
                # missing identity leaves legacy execution usable but cannot
                # certify a retained continuation.
                try:
                    inspected = subprocess.run(
                        ["docker", "inspect", container_name], capture_output=True,
                        timeout=10, text=True, check=False,
                    )
                    metadata = json.loads(inspected.stdout)[0] if inspected.returncode == 0 else {}
                    image_id = metadata.get("Image", "")
                    host = metadata.get("HostConfig", {})
                    if (re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
                            and host.get("Runtime") == runtime and host.get("NetworkMode") == "none"):
                        execution = {"contract": "sandbox-execution.v1", "id": container_name,
                                     "image_id": image_id, "runtime": runtime, "network": "none"}
                except (ValueError, KeyError, IndexError, TypeError, FileNotFoundError, subprocess.TimeoutExpired):
                    execution = None
                # Creation cannot execute user code. A cancellation between this
                # check and start is caught by the monitor, which removes the
                # already-created exact container; no late create can escape it.
                if not cancel_marker.exists():
                    proc = subprocess.Popen(  # noqa: S603 — server-generated name
                        ["docker", "start", "--attach", container_name],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    result["execution_state"] = "running"
                while True:
                    remaining = timeout - (time.perf_counter() - t0)
                    if cancel_marker.exists():
                        result["error"] = "cancelled"
                        break
                    if remaining <= 0:
                        result.update(error=f"timeout after {timeout}s", cause="execution_timeout")
                        break
                    try:
                        stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                    result.update(ok=proc.returncode == 0, exit_code=proc.returncode,
                                  stdout=stdout or "", stderr=stderr or "",
                                  error=None if proc.returncode == 0 else f"exit_code={proc.returncode}")
                    if proc.returncode != 0:
                        result["cause"] = "execution_failed"
                    break
    except subprocess.TimeoutExpired:
        result.update(error=f"timeout after {timeout}s", cause="execution_timeout")
    except FileNotFoundError:
        result.update(error="docker CLI not found in executor", cause="executor_unavailable")
    finally:
        if create_attempted:
            try:
                removed = subprocess.run(  # noqa: S603 — remove only this server-generated name
                    ["docker", "rm", "-f", container_name], capture_output=True,
                    timeout=10, text=True, check=False,
                )
                if removed.returncode and "No such container" not in (removed.stderr or ""):
                    result.update(ok=False, error="container_cleanup_unconfirmed", cause="execution_state_unknown", execution_state="unknown")
                elif result["execution_state"] == "running":
                    result["execution_state"] = "stopped"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                result.update(ok=False, error="container_cleanup_unconfirmed", cause="execution_state_unknown", execution_state="unknown")
        if proc is not None:
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    proc.kill()
                proc.communicate()
        # Keep the tombstone until workdir retention cleanup. A delayed request
        # for the same invocation must not start after cancellation was observed.
    result["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    if result["ok"] and execution is not None:
        result["execution"] = {**execution, "settled": True}
    return result


_TOKEN = os.environ.get("BOWHEAD_SANDBOX_EXECUTOR_TOKEN", "")


class _Handler(BaseHTTPRequestHandler):
    server_version = "bowhead-sandbox-executor/1"

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        # A cancelled runner disconnects before the executor finishes cleanup.
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(data)

    def _authed(self) -> bool:
        tok = self.headers.get("X-Executor-Token", "")
        # constant-time compare; empty configured token → always reject
        return bool(_TOKEN) and hmac.compare_digest(tok, _TOKEN)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/document-runs"):
            self._document_request()
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/document-runs"):
            self._document_request()
            return
        if self.path != "/run":
            self._json(404, {"error": "not found"})
            return
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            length = 0
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._json(400, {"error": "bad content-length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": f"bad json: {e}"})
            return
        self._json(200, _run(payload))

    def _document_request(self) -> None:
        from document_runs import DocumentRuns

        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            runs = DocumentRuns(Path(os.environ["BOWHEAD_SKILL_WORKDIR_ROOT"]))
            parts = self.path.strip("/").split("/")
            if self.command == "GET" and parts == ["document-runs", "capabilities"]:
                result = runs.readiness()
            elif self.command == "GET" and len(parts) == 2 and parts[0] == "document-runs":
                result = runs.get(parts[1])
            elif self.command == "POST" and len(parts) == 3 and parts[0] == "document-runs" and parts[2] == "cancel":
                result = runs.cancel(parts[1])
            elif self.command == "POST" and parts == ["document-runs"]:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError("document_run_control_invalid")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("document_run_control_invalid")
                result = runs.start(payload)
            else:
                self._json(404, {"error": "not_found"})
                return
            self._json(200, result)
        except FileNotFoundError:
            self._json(404, {"error": "document_run_not_found"})
        except ValueError:
            self._json(409, {"error": "document_run_conflict"})
        except (RuntimeError, OSError, subprocess.TimeoutExpired, KeyError):
            self._json(503, {"error": "document_runtime_unavailable"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802 — quieter logs
        sys.stderr.write("[executor] " + (fmt % args) + "\n")


def _ensure_workdir_root(root: str) -> None:
    """Own the shared workdir root from the image — create it and grant 1777.

    The gateway runs as uid 1000 (`USER bowhead`, deploy/Dockerfile.app) and cannot
    create or re-mode a root-owned bind mount, so a host dir that Docker auto-created
    (root:root 0755) makes every `mkdtemp` there EACCES — python_sandbox then fails
    on every call, exposing the raw permission error to end users.

    The executor is the only component that can fix it: it runs as root (for
    docker.sock), mounts the SAME host path at the SAME container path, and compose's
    `depends_on: condition: service_healthy` guarantees it completes before the
    gateway starts. chmod on a bind mount hits the shared inode, so this is exactly
    what `install -d -m 1777` does in the deploy docs — except the image now does it
    itself, which is the only form that survives a deployment where operators only
    ever pull new images.

    1777 (= /tmp semantics: world-writable + sticky) is required because the writers
    have different uids (gateway 1000, executor root, sandbox container its own) with
    no shared group.

    Never fatal: the sandbox is one capability among many, and refusing to start would
    drag down BI/chat through depends_on. On failure the gateway-side usability gate
    turns this into a readable "sandbox not ready" instead of an errno.
    """
    try:
        os.makedirs(root, exist_ok=True)
        current = stat.S_IMODE(os.stat(root).st_mode)
        if current != _WORKDIR_ROOT_MODE:
            os.chmod(root, _WORKDIR_ROOT_MODE)
            sys.stderr.write(f"[executor] workdir root {root} mode {current:04o} -> {_WORKDIR_ROOT_MODE:04o}\n")
    except OSError as e:
        sys.stderr.write(
            f"[executor] WARN workdir root {root} not prepared ({e}) — python_sandbox will fail closed until resolved\n"
        )


def main() -> int:
    if not _TOKEN:
        sys.stderr.write(
            "FATAL: BOWHEAD_SANDBOX_EXECUTOR_TOKEN unset — refusing to start "
            "(would accept unauthenticated docker-run requests).\n"
        )
        return 2
    if not os.environ.get("BOWHEAD_SKILL_WORKDIR_ROOT"):
        sys.stderr.write("FATAL: BOWHEAD_SKILL_WORKDIR_ROOT unset.\n")
        return 2
    _ensure_workdir_root(os.environ["BOWHEAD_SKILL_WORKDIR_ROOT"])
    port = _env_int("BOWHEAD_SANDBOX_EXECUTOR_PORT", 8091)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)  # noqa: S104 — private net only
    stop_documents = threading.Event()

    def maintain_documents():
        from document_runs import DocumentRuns

        runs = DocumentRuns(Path(os.environ["BOWHEAD_SKILL_WORKDIR_ROOT"]))
        while not stop_documents.wait(60):
            try:
                runs.sweep()
            except Exception:
                sys.stderr.write("[executor] document maintenance deferred\n")

    threading.Thread(target=maintain_documents, daemon=True, name="document-maintenance").start()
    sys.stderr.write(f"[executor] listening on :{port} root={os.environ['BOWHEAD_SKILL_WORKDIR_ROOT']}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_documents.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
