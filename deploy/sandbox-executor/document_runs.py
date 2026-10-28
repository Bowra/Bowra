"""Durable, idempotent execution of the baked network-isolated document worker.

Only this service owns Docker. Clients cannot select images, mounts, commands,
requirements, resource limits or host paths. The server-owned journal is never
mounted into a worker; a worker can write only its own output directory.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

PROFILE = "ordinary-pptx-worker-v1"
TIMEOUT = 120
_HEX = re.compile(r"[a-f0-9]{64}\Z")


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError("document_docker_unavailable")
    return result.stdout.strip()


def _id(value: object) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise ValueError("document_run_identity_invalid")
    return value


class DocumentRuns:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if "," in str(self.root):
            raise ValueError("document_directory_invalid")
        self.journal = self.root / ".document-executions"
        if self.journal.is_symlink():
            raise ValueError("document_journal_invalid")
        self.journal.mkdir(mode=0o700, exist_ok=True)
        if self.journal.stat().st_uid != os.geteuid():
            raise ValueError("document_journal_invalid")
        self.journal.chmod(0o700)
        self._sweep_cursor = ""

    @contextmanager
    def _lock(self):
        path = self.journal / "lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _read(self, run_id: str) -> dict:
        path = self.journal / (_id(run_id) + ".json")
        if path.is_symlink():
            raise ValueError("document_journal_invalid")
        return json.loads(path.read_text())

    def _save(self, record: dict) -> None:
        path = self.journal / (_id(record["run_id"]) + ".json")
        temporary = self.journal / (record["run_id"] + ".pending")
        # The private directory is executor-owned and writes are serialized.
        with temporary.open("w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fd = os.open(self.journal, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def readiness(self) -> dict:
        image = os.environ.get("BOWHEAD_SANDBOX_IMAGE", "")
        runtime = os.environ.get("BOWHEAD_SANDBOX_RUNTIME", "runc")
        if not image or runtime not in {"runc", "runsc"}:
            raise RuntimeError("document_runtime_unavailable")
        if int(os.environ.get("BOWHEAD_SANDBOX_MEMORY_MB", "512")) < 2048 or int(os.environ.get("BOWHEAD_SANDBOX_TMPFS_MB", "64")) < 256:
            raise RuntimeError("document_runtime_resources_insufficient")
        if int(os.environ.get("BOWHEAD_SANDBOX_MAX_TIMEOUT", "120")) < TIMEOUT or int(os.environ.get("BOWHEAD_SANDBOX_PIDS", "50")) < 128:
            raise RuntimeError("document_runtime_resources_insufficient")
        info = json.loads(_docker("image", "inspect", image))[0]
        labels = info.get("Config", {}).get("Labels", {}) or {}
        if labels.get("bowhead.document.profile") != PROFILE:
            raise RuntimeError("document_worker_profile_unavailable")
        planners = labels.get("bowhead.document.planners", "pptx-grounded-plan-v4")
        if not isinstance(planners, str):
            raise RuntimeError("document_worker_profile_unavailable")
        planners = planners.split(",")
        if not planners or len(set(planners)) != len(planners) or any(item not in {"pptx-grounded-plan-v4", "pptx-grounded-plan-v5"} for item in planners):
            raise RuntimeError("document_worker_profile_unavailable")
        return {"profile": PROFILE, "planner_profiles": planners, "image_id": info["Id"], "runtime": runtime, "timeout_seconds": TIMEOUT}

    def _inputs(self, run_id: str, digest: str) -> tuple[Path, Path]:
        directory = self.root / ("document-" + run_id)
        for path in (directory, directory / "input", directory / "output"):
            if path.is_symlink() or not path.is_dir() or path.resolve() != path:
                raise ValueError("document_directory_invalid")
        inputs, outputs = directory / "input", directory / "output"
        entries = list(inputs.iterdir())
        if not 2 <= len(entries) <= 9 or any(
            path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
            or (path.name != "control.json" and re.fullmatch(r"input-[0-7]", path.name) is None)
            or path.stat().st_size > 32 * 1024 * 1024 for path in entries
        ):
            raise ValueError("document_inputs_invalid")
        control = inputs / "control.json"
        if not control.is_file() or control.stat().st_size > 256_000 or hashlib.sha256(control.read_bytes()).hexdigest() != digest:
            raise ValueError("document_control_mismatch")
        if any(outputs.iterdir()):
            raise ValueError("document_output_not_empty")
        return inputs, outputs

    def start(self, payload: dict) -> dict:
        if set(payload) != {"run_id", "control_sha256", "expected_image_id"}:
            raise ValueError("document_run_control_invalid")
        run_id, digest = _id(payload["run_id"]), _id(payload["control_sha256"])
        with self._lock():
            try:
                previous = self._read(run_id)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                if previous["control_sha256"] != digest or previous["expected_image_id"] != payload["expected_image_id"]:
                    raise ValueError("document_run_conflict")
                return previous
            runtime = self.readiness()
            if payload["expected_image_id"] not in (None, runtime["image_id"]):
                raise ValueError("document_runtime_changed")
            inputs, outputs = self._inputs(run_id, digest)
            active = _docker("ps", "-q", "--filter", "label=bowhead.document.profile=" + PROFILE).splitlines()
            if len(active) >= 2:
                raise RuntimeError("document_runtime_busy")
            record = {**payload, **runtime, "status": "starting", "created_at": time.time(), "container": "bowhead-document-" + run_id}
            self._save(record)  # An unknown create/start outcome never starts a second container.
            argv = [
                "create", "--name", record["container"], "--label", "bowhead.document.run=" + run_id,
                "--label", "bowhead.document.control=" + digest, "--label", "bowhead.document.profile=" + PROFILE,
                "--runtime=" + runtime["runtime"], "--network=none", "--read-only", "--user", "1000:1000",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=2048m", "--memory-swap=2048m",
                "--cpus=1.0", "--pids-limit=128", "--tmpfs", "/tmp:size=256m,exec", "--ulimit", "fsize=268435456:268435456",
                "--mount", f"type=bind,src={inputs},dst=/input,readonly",
                "--mount", f"type=bind,src={outputs},dst=/output",
                "--workdir", "/tmp", "--env", "LANG=C.UTF-8", "--env", "LC_ALL=C.UTF-8", "--env", "TZ=UTC",
                "--env", "HOME=/tmp", "--env", "XDG_CACHE_HOME=/tmp/.cache",
                runtime["image_id"], "/usr/bin/timeout", "--kill-after=5s", str(TIMEOUT) + "s",
                "python", "-I", "/opt/bowhead/bowhead_core/documents/worker_entry.py", "--control-sha256", digest,
            ]
            try:
                _docker(*argv)
                _docker("start", record["container"])
                record["status"] = "running"
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                record["status"] = "unknown"
            self._save(record)
            return record

    def _inspect(self, record: dict) -> dict:
        info = json.loads(_docker("inspect", record["container"]))[0]
        labels = info.get("Config", {}).get("Labels", {})
        if labels.get("bowhead.document.run") != record["run_id"] or labels.get("bowhead.document.control") != record["control_sha256"] or info["Image"] != record["image_id"]:
            raise RuntimeError("document_container_identity_mismatch")
        return info["State"]

    def get(self, run_id: str) -> dict:
        with self._lock():
            record = self._read(run_id)
            if record["status"] in {"completed", "failed", "cancelled"}:
                if record.get("removal_pending"):
                    try:
                        self._inspect(record)
                        _docker("rm", "-f", record["container"])
                        record.pop("removal_pending", None)
                        self._save(record)
                    except (RuntimeError, OSError, subprocess.TimeoutExpired):
                        try:
                            if not _docker("ps", "-aq", "--filter", "name=^/" + record["container"] + "$"):
                                record.pop("removal_pending", None)
                                self._save(record)
                        except (RuntimeError, OSError, subprocess.TimeoutExpired):
                            pass
                return record
            try:
                state = self._inspect(record)
                if state["Status"] in {"exited", "dead"}:
                    record.update(status="completed" if state["ExitCode"] == 0 else "failed", exit_code=state["ExitCode"])
                    record["removal_pending"] = True
                    self._save(record)
                    # Persist the result before removing this verified container.
                    try:
                        _docker("rm", record["container"])
                        record.pop("removal_pending", None)
                    except (RuntimeError, OSError, subprocess.TimeoutExpired):
                        record["removal_pending"] = True
                elif time.time() > record["created_at"] + TIMEOUT + 30:
                    # Reconcile after executor restart. The container's own
                    # timeout is the primary bound while the executor is down.
                    _docker("rm", "-f", record["container"])
                    record.update(status="failed", error_code="document_worker_timeout")
                else:
                    record["status"] = "running" if state["Status"] == "running" else "unknown"
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                record["status"] = "unknown"
                if time.time() > record["created_at"] + TIMEOUT + 30:
                    try:
                        if not _docker("ps", "-aq", "--filter", "name=^/" + record["container"] + "$"):
                            record.update(status="failed", error_code="document_worker_outcome_unavailable")
                    except (RuntimeError, OSError, subprocess.TimeoutExpired):
                        pass
            self._save(record)
            return record

    def sweep(self) -> None:
        """Bounded reconciliation and 24-hour staging cleanup, never journal deletion.

        Published/reviewable bytes live in the application artifact store. This
        removes only terminal executor scratch space; retained journal identity
        prevents a later replay from dispatching another worker.
        """
        paths = sorted(self.journal.glob("*.json"))
        pending = [path for path in paths if path.name > self._sweep_cursor][:5]
        if not pending:
            self._sweep_cursor = ""
            return
        for path in pending:
            self._sweep_cursor = path.name
            try:
                record = self.get(path.stem)
                if record["status"] not in {"completed", "failed", "cancelled"} or record.get("removal_pending"):
                    continue
                if time.time() < record["created_at"] + 86400:
                    continue
                with self._lock():
                    directory = self.root / ("document-" + _id(record["run_id"]))
                    if directory.is_dir() and not directory.is_symlink() and directory.resolve() == directory:
                        shutil.rmtree(directory)
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired):
                # A failed reconciliation keeps both bytes and durable identity.
                continue

    def cancel(self, run_id: str) -> dict:
        with self._lock():
            record = self._read(run_id)
            if record["status"] in {"completed", "failed"}:
                return record
            record["status"] = "cancelled"
            self._save(record)
            try:
                self._inspect(record)
                _docker("rm", "-f", record["container"])
                record.pop("removal_pending", None)
                self._save(record)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                # Retain identity; a repeated cancel can retry exact removal.
                record["removal_pending"] = True
                self._save(record)
            return record
