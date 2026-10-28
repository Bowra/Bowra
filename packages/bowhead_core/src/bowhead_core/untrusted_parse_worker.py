"""One-shot child process that parses a single untrusted file under rlimits.

Driven by `untrusted_parse.run_worker_json` via
`python -m bowhead_core.untrusted_parse_worker <control.json> <result.json> <cpu_s> <mem_mb>`.

Contract:
- argv[1] = control JSON path: {"kind": "attachment"|"template", ...}
    attachment: {mime, filename, artifact_id, size, data_path}
    template:   {path}
- argv[2] = result JSON path to write on success (parent reads it back).
- argv[3] = RLIMIT_CPU seconds, argv[4] = RLIMIT_AS megabytes.

The worker sets its OWN rlimits FIRST (before importing pypdf/python-pptx/etc.)
so a pathological file is CPU/memory-bounded; the parent additionally
wall-timeout-kills it. Exit non-zero (or write nothing) → parent maps to a
fail-soft error record. This is a security boundary in a fresh interpreter: exec
dropped the parent's asyncio locks + address space, and the parent launches it
with a scrubbed env (`_child_env`) so no gateway secret is inherited.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


def _err(msg: str) -> None:
    # The worker's diagnostics channel is stderr (the parent captures it). Safe
    # to write here: main thread of a fresh interpreter, no fork/lock context.
    sys.stderr.write(f"untrusted_parse_worker: {msg}\n")


def _cap(limit_id: int, value: int) -> None:
    """Set a resource cap, preferring a BINDING hard limit (untrusted parse can't
    lift it — Linux). If the platform rejects lowering the hard limit (macOS
    RLIMIT_AS: "current limit exceeds maximum"), fall back to a soft cap within
    the existing hard ceiling — still enforced, just liftable in theory (and we
    run no untrusted code, only parse untrusted data)."""
    import resource

    _soft, hard = resource.getrlimit(limit_id)
    soft_only = value if hard == resource.RLIM_INFINITY else min(value, hard)
    for target in ((value, value), (soft_only, hard)):
        try:
            resource.setrlimit(limit_id, target)
            return
        except (ValueError, OSError):
            continue
    _err(f"setrlimit({limit_id}) failed (binding + soft) — parse runs uncapped, wall-timeout still bounds")


def _apply_self_rlimits(cpu_seconds: int, mem_mb: int) -> None:
    """Cap this child's CPU + address space before importing the heavy parsers."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-unix; wall-timeout still bounds
        return
    _cap(resource.RLIMIT_CPU, cpu_seconds)
    # macOS/Darwin does not enforce RLIMIT_AS (setrlimit returns EINVAL) — skip it
    # silently there (CPU + parent wall-timeout still bound). On Linux (prod) the
    # AS cap is the authoritative memory bound and any failure is surfaced.
    if sys.platform != "darwin":
        _cap(resource.RLIMIT_AS, mem_mb * 1024 * 1024)


def _run_attachment(control: dict[str, Any]) -> dict[str, Any]:
    from bowhead_core.attachments_injector import extract_parts_to_dict

    data = Path(control["data_path"]).read_bytes()
    return extract_parts_to_dict(
        mime=control.get("mime", ""),
        filename=control.get("filename", ""),
        artifact_id=control.get("artifact_id", ""),
        size=int(control.get("size", 0) or 0),
        data=data,
    )


def _run_template(control: dict[str, Any]) -> dict[str, Any]:
    from bowhead_core.template_clone import parse_template

    parsed = parse_template(control["path"])
    return asdict(parsed)


def main() -> int:
    if len(sys.argv) != 5:
        _err("usage: untrusted_parse_worker <control.json> <result.json> <cpu_s> <mem_mb>")
        return 2
    control_path, result_path, cpu_s, mem_mb = sys.argv[1:5]
    _apply_self_rlimits(int(cpu_s), int(mem_mb))

    try:
        control = json.loads(Path(control_path).read_text(encoding="utf-8"))
    except Exception as e:
        _err(f"bad control json: {e}")
        return 2

    kind = control.get("kind")
    try:
        if kind == "attachment":
            result = _run_attachment(control)
        elif kind == "template":
            result = _run_template(control)
        else:
            _err(f"unknown kind {kind!r}")
            return 2
    except MemoryError:
        _err("MemoryError (RLIMIT_AS)")
        return 3
    except Exception as e:
        _err(f"parse failed: {type(e).__name__}: {e}")
        return 1

    try:
        Path(result_path).write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception as e:
        _err(f"write result failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
