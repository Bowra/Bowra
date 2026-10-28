"""pptx-from-template M2 (wheelhouse) — in-container offline install pre-step.

Baked into the sandbox image at ``/opt/bowhead/sandbox_bootstrap.py`` (Dockerfile
``COPY python_sandbox/sandbox_bootstrap.py``, before ``USER sandbox``). The runner
swaps the container cmd from ``python user.py`` to ``python
/opt/bowhead/sandbox_bootstrap.py`` ONLY when the invoke declares
``_sandbox_requirements`` AND a wheelhouse is mounted RO at ``/wheelhouse``.

Flow:
    read /workdir/input.json → _sandbox_requirements
    empty   → runpy.run_path('/workdir/user.py')  (no-op; identical to bare run)
    present → regex + wheelhouse-lockset validate (IDENTICAL to the runner
              pre-check), then
                pip install --no-index --find-links=/wheelhouse
                            --require-hashes -r /wheelhouse/requirements.lock
                            --target=/tmp/site <validated reqs>
              sys.path.insert(0, '/tmp/site'); runpy.run_path('/workdir/user.py')

Fail closed: anything that does not match the strict regex AND appear in the
wheelhouse lock-set is DROPPED (never installed). ``--no-index`` guarantees no
network reach; ``--require-hashes`` enforces wheel integrity against the shipped
lock. ``--target=/tmp/site`` because rootfs is ``--read-only`` (/tmp is the
writable tmpfs). ``--network=none`` is enforced by the runner on every path and
is NEVER relaxed.
"""

from __future__ import annotations

import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

# Strict requirement grammar — IDENTICAL to runner._REQ_RE. Only a bare name or
# `name==version`; the charset blocks argv injection / extra pip flags even
# though deps originate from a SKILL.md on the trusted gateway FS (a SKILL.md
# can be a user-uploaded skill).
_REQ_RE = re.compile(r"^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$")

_USER_PY = "/workdir/user.py"
_INPUT_JSON = "/workdir/input.json"
_WHEELHOUSE = "/wheelhouse"
_LOCK = "/wheelhouse/requirements.lock"
_SITE = "/tmp/site"


def _lockset(lock_path: str) -> set[str]:
    """Project-name set from requirements.lock (the reviewed/hash-locked names).

    Reads names from the leading ``name==version`` of each non-comment,
    non-continuation line (pip-compile / --generate-hashes format: hashes hang
    on continuation lines starting with whitespace or ``--hash``). Absent/empty
    lock → empty set (everything fails closed). Names are lower-cased and
    ``_``↔``-`` normalized (PEP 503) so a requirement matches its locked form.
    """
    p = Path(lock_path)
    if not p.is_file():
        return set()
    names: set[str] = set()
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "--")):
                continue
            # strip a trailing line-continuation backslash + inline hash
            head = line.split("\\", 1)[0].split(";", 1)[0].strip()
            m = re.match(r"^([A-Za-z0-9._-]+)", head)
            if m:
                names.add(_norm(m.group(1)))
    except Exception:
        return set()
    return names


def _norm(name: str) -> str:
    """PEP 503 normalize a project name for lock-set membership."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _validate(reqs: list[str], lockset: set[str]) -> tuple[list[str], list[str]]:
    """(valid, errors). IDENTICAL contract to the runner pre-check: each req must
    match the regex AND its project name must be in the wheelhouse lock-set, else
    it is DROPPED + an error recorded (fail closed — never install an
    unvalidated/unlocked dep).
    """
    valid: list[str] = []
    errors: list[str] = []
    for req in reqs:
        r = str(req).strip()
        if not r:
            continue
        if not _REQ_RE.match(r):
            errors.append(f"sandbox_requirement {r!r} fails strict regex — dropped")
            continue
        proj = _norm(r.split("==", 1)[0])
        if proj not in lockset:
            errors.append(f"sandbox_requirement {r!r} not in wheelhouse lock-set — dropped (fail closed)")
            continue
        valid.append(r)
    return valid, errors


def _run_user() -> None:
    # excel RC-6: runpy.run_path does not add /workdir to sys.path, so user code importing
    # `from dirty_table import ...` (a helper staged by runner into workdir) raises
    # ModuleNotFoundError. The common path (python user.py, cwd=/workdir) adds it naturally,
    # but the wheelhouse path through here must add it explicitly (review P2-1).
    if "/workdir" not in sys.path:
        sys.path.insert(0, "/workdir")
    runpy.run_path(_USER_PY, run_name="__main__")


def main() -> int:
    try:
        payload = json.loads(Path(_INPUT_JSON).read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    reqs = payload.get("_sandbox_requirements") or []
    if not isinstance(reqs, list):
        reqs = []

    # No declared deps → straight to user.py (byte-for-byte the bare run).
    if not reqs:
        _run_user()
        return 0

    lockset = _lockset(_LOCK)
    valid, errors = _validate(reqs, lockset)
    for err in errors:
        sys.stderr.write(f"[bootstrap] {err}\n")

    if valid:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            f"--find-links={_WHEELHOUSE}",
            "--require-hashes",
            "-r",
            _LOCK,
            "--target",
            _SITE,
            *valid,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if proc.stdout:
            sys.stderr.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            sys.stderr.write(f"[bootstrap] offline pip install failed (exit {proc.returncode}) — running user.py without the declared deps\n")
        else:
            sys.path.insert(0, _SITE)

    _run_user()
    return 0


if __name__ == "__main__":
    sys.exit(main())
