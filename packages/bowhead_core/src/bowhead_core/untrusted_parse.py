"""Bounded, resource-limited parsing of untrusted uploaded files.

skill-multiruntime S3 follow-up (scope A+C). Untrusted PDF/OOXML uploads are
parsed on the gateway host by pypdf / python-pptx / python-docx / openpyxl.
Today those libs run with **no CPU/memory bound**, and several call sites run
the parse **synchronously inside `async def`** — so a malicious or merely
pathological file can wedge the single-process uvicorn event loop (a DoS that
takes down every tenant). This module is the shared hardening primitive that all
untrusted-parse call sites route through:

- **Tier-1 (flag-independent)**: a cheap OOXML zip decompression-ratio pre-check
  (rejects honest zip-bombs before any parse) + callers offload the parse to a
  worker thread so the event loop keeps turning.
- **Tier-2 (`BOWHEAD_BOUNDED_ATTACH_PARSE=1`)**: run the parse in a one-shot
  child process (`python -m bowhead_core.untrusted_parse_worker`) that caps its
  own `RLIMIT_CPU`/`RLIMIT_AS` and is wall-timeout-killed by the parent, gated
  by a **process-wide concurrency semaphore** (so the fix can't become its own
  fork-bomb / memory-exhaustion DoS).

Threat model (see `docs/sprints/skill-multiruntime/s3-followup-untrusted-parse-design.md`):
**DoS-primary, not exfil.** XXE / external-entity is neutralized upstream
(`resolve_entities=False` + stdlib expat defaults; empirically verified), and
billion-laughs is bounded by libxml2/expat amplification caps (a pinned-version
property). Tier-2's RLIMIT_AS is the authoritative memory bound; the zip
pre-check is only a cheap early-out for honest bombs (nested/quine bombs and
pathological PDFs are caught by the killed child).

Why a child process rather than `multiprocessing.Process`: forking a process
that already runs an asyncio loop + threadpool inherits locks held by other
threads (malloc/logging) → deadlock footgun, and copies the multi-tenant address
space (secrets) into the process that then parses attacker bytes. `exec` of a
fresh interpreter drops all inherited locks/threads/address-space at exec; the
child additionally runs with a scrubbed `env=` (`_child_env`) so gateway secrets
(JWT/Fernet/DB) never reach the process parsing attacker bytes via its
environment.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FLAG_ENV = "BOWHEAD_BOUNDED_ATTACH_PARSE"

# OOXML (pptx/docx/xlsx) container mimes + the extensions we treat as zip-based.
_OOXML_EXTS = (".pptx", ".docx", ".xlsx", ".xlsm")
_OOXML_MIME_MARKERS = ("openxmlformats", "presentationml", "wordprocessingml", "spreadsheetml")
_PDF_MARKERS = ("application/pdf",)

# Zip-bomb pre-check budget. Mirrors routes/kb.py's _BUNDLE_MAX_UNCOMPRESSED so a
# legitimately large deck (well under 2 GB uncompressed) is never rejected; this
# only trips on genuinely bomb-scale declared expansion. Env-overridable.
_ZIP_MAX_UNCOMPRESSED = int(os.environ.get("BOWHEAD_PARSE_ZIP_MAX_UNCOMPRESSED", str(2 * 1024 * 1024 * 1024)))
# Ratio guard: declared-uncompressed / compressed. Legit OOXML rarely exceeds a
# few dozen ×; a naive bomb declares a huge ratio. Generous default = 200×.
_ZIP_MAX_RATIO = int(os.environ.get("BOWHEAD_PARSE_ZIP_MAX_RATIO", "200"))
# Only apply the RATIO guard once the absolute uncompressed size is itself
# non-trivial — a small dense OOXML (repetitive XML compresses >200×) is harmless
# regardless of ratio, and the absolute cap above already catches true bombs.
# Avoids false-rejecting legit small-but-dense spreadsheets/decks.
_ZIP_RATIO_FLOOR = int(os.environ.get("BOWHEAD_PARSE_ZIP_RATIO_FLOOR", str(100 * 1024 * 1024)))

# Env keys the extraction child needs to run (`-m bowhead_core...` import + locale).
# Everything else (JWT/Fernet/DB/OAuth secrets) is deliberately withheld: the
# child parses attacker bytes and must not carry gateway secrets in its env.
_CHILD_ENV_ALLOW = (
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)

# Only route to the (heavier) subprocess for genuinely risky inputs: PDF/OOXML
# above a size floor. Small files, text/csv, and images (base64, no parser) stay
# on the fast in-thread path even with the flag on — bounds latency regression.
_SUBPROCESS_MIN_BYTES = int(os.environ.get("BOWHEAD_PARSE_SUBPROCESS_MIN_BYTES", str(256 * 1024)))

# Process-wide cap on concurrent extraction children (not per-turn) — the
# authoritative backstop against a fork-bomb / RLIMIT_AS × N memory blow-up.
_MAX_CONCURRENCY = max(1, int(os.environ.get("BOWHEAD_PARSE_MAX_CONCURRENCY", "4")))


@dataclass(frozen=True)
class ExtractLimits:
    """Resource caps for the extraction child. Tighter than skill RunLimits —
    preview/template parsing should be short. Env-overridable."""

    cpu_seconds: int = int(os.environ.get("BOWHEAD_PARSE_CPU_S", "30"))
    max_memory_mb: int = int(os.environ.get("BOWHEAD_PARSE_MEM_MB", "1536"))
    wall_timeout_sec: int = int(os.environ.get("BOWHEAD_PARSE_WALL_S", "45"))


DEFAULT_LIMITS = ExtractLimits()

# Keyed by running-loop id: an asyncio.Semaphore binds to the loop that first
# awaits it, so a process-global one would raise "bound to a different event
# loop" under a second loop (tests). Prod runs a single uvicorn loop; the cap is
# process-wide within that loop.
_sems: dict[int, asyncio.Semaphore] = {}


def bounded_parse_enabled() -> bool:
    """Tier-2 (subprocess isolation) flag. Default off → today's in-process path
    (callers still get Tier-1 to_thread + the zip pre-check)."""
    return os.environ.get(_FLAG_ENV, "0").strip() == "1"


def _semaphore() -> asyncio.Semaphore:
    key = id(asyncio.get_running_loop())
    sem = _sems.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        _sems[key] = sem
    return sem


def _child_env() -> dict[str, str]:
    """Scrubbed environment for the extraction child — only what `-m` import +
    locale need; gateway secrets are withheld (defense-in-depth: the child parses
    attacker bytes)."""
    env = {k: os.environ[k] for k in _CHILD_ENV_ALLOW if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def is_ooxml(mime: str, filename: str) -> bool:
    m = (mime or "").lower()
    fn = (filename or "").lower()
    return any(k in m for k in _OOXML_MIME_MARKERS) or fn.endswith(_OOXML_EXTS)


def is_pdf(mime: str, filename: str) -> bool:
    m = (mime or "").lower()
    fn = (filename or "").lower()
    return m in _PDF_MARKERS or fn.endswith(".pdf")


def should_subprocess(mime: str, filename: str, size: int) -> bool:
    """Whether an attachment parse warrants the isolated child (Tier-2). Only
    PDF/OOXML above the size floor — cheap/parserless inputs stay in-thread."""
    if size is not None and size >= _SUBPROCESS_MIN_BYTES:
        if is_pdf(mime, filename) or is_ooxml(mime, filename):
            return True
    return False


def _zip_bomb_from(zf: zipfile.ZipFile, compressed: int) -> str | None:
    """Core check: sum declared uncompressed sizes (central-dir metadata only,
    no decompression); reject on absolute cap, or on ratio once the absolute
    size is non-trivial."""
    total = 0
    for zi in zf.infolist():
        total += zi.file_size
        if total > _ZIP_MAX_UNCOMPRESSED:
            return f"zip uncompressed too large ({total // 1024 // 1024}MB > {_ZIP_MAX_UNCOMPRESSED // 1024 // 1024}MB) — possible zip bomb"
    if total > _ZIP_RATIO_FLOOR and total // max(1, compressed) > _ZIP_MAX_RATIO:
        return f"zip decompression ratio too high ({total // max(1, compressed)}× > {_ZIP_MAX_RATIO}×) — possible zip bomb"
    return None


def zip_bomb_reason(data: bytes, mime: str, filename: str) -> str | None:
    """Cheap OOXML zip decompression-ratio pre-check over in-memory bytes. Returns
    a reason string if the declared expansion looks bomb-scale, else None.
    Honest-bomb early-out only — the authoritative bound is the child's
    RLIMIT_AS. NOTE: run this OFF the event loop (`to_thread`) — opening the
    central directory of a crafted many-entry zip is untrusted-input work."""
    if not is_ooxml(mime, filename):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return _zip_bomb_from(zf, len(data))
    except zipfile.BadZipFile:
        return None  # not a valid zip → let the real parser report it
    except Exception as e:  # defensive: never let the guard itself raise
        logger.warning("untrusted_parse.zip_precheck_failed fn=%s err=%s", filename, e)
        return None


def zip_bomb_reason_path(path: str, filename: str) -> str | None:
    """Same check reading the zip central directory from a file path (avoids
    loading the whole file into memory). For cluster-C templates on disk. Run
    off the event loop (`to_thread`)."""
    if not is_ooxml("", filename):
        return None
    try:
        compressed = Path(path).stat().st_size
        with zipfile.ZipFile(path) as zf:
            return _zip_bomb_from(zf, compressed)
    except zipfile.BadZipFile:
        return None
    except Exception as e:
        logger.warning("untrusted_parse.zip_precheck_failed fn=%s err=%s", filename, e)
        return None


async def run_worker_json(
    kind: str,
    control: dict[str, Any],
    *,
    data: bytes | None = None,
    limits: ExtractLimits = DEFAULT_LIMITS,
) -> tuple[dict[str, Any] | None, str]:
    """Drive the one-shot extraction child: write `control` (+ any raw `data`
    bytes) into a private temp dir, exec the worker (scrubbed env), wall-timeout-
    kill it, read back the JSON result. Returns ``(result, "ok")`` on success or
    ``(None, reason)`` where reason ∈ {"timeout","oom","failed"} so the caller can
    surface a differentiated fail-soft error record. Gated by the process-wide
    concurrency semaphore.

    The child sets its OWN RLIMIT_CPU/RLIMIT_AS at startup (before importing the
    heavy parsers) — safer than a fork-context `preexec_fn` (no fork-time
    logging/lock footgun) and un-escapable by the untrusted DATA (worker code is
    trusted; only the parsed bytes are not). It runs with a scrubbed env so no
    gateway secret is inherited.
    """
    async with _semaphore():
        tmpdir = Path(tempfile.mkdtemp(prefix="bow-parse-"))
        in_path = tmpdir / "control.json"
        out_path = tmpdir / "result.json"
        try:
            ctl = {**control, "kind": kind}
            if data is not None:
                data_path = tmpdir / "data.bin"
                await asyncio.to_thread(data_path.write_bytes, data)
                ctl["data_path"] = str(data_path)
            in_path.write_text(json.dumps(ctl), encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "bowhead_core.untrusted_parse_worker",
                str(in_path),
                str(out_path),
                str(limits.cpu_seconds),
                str(limits.max_memory_mb),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
            )
            try:
                _out, err = await asyncio.wait_for(proc.communicate(), timeout=limits.wall_timeout_sec)
            except TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass  # child exited exactly at the deadline
                logger.warning("untrusted_parse.worker_timeout kind=%s wall=%ss", kind, limits.wall_timeout_sec)
                return None, "timeout"
            err_txt = (err or b"").decode("utf-8", "replace")
            if proc.returncode != 0 or not out_path.is_file():
                logger.warning("untrusted_parse.worker_failed kind=%s rc=%s stderr=%s", kind, proc.returncode, err_txt[:500])
                return None, ("oom" if proc.returncode == 3 else "failed")
            if err_txt.strip():
                # e.g. a setrlimit failure that still parsed → surface the lost cap (D5).
                logger.warning("untrusted_parse.worker_stderr_on_success kind=%s stderr=%s", kind, err_txt[:500])
            try:
                result: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("untrusted_parse.worker_bad_result kind=%s err=%s", kind, e)
                return None, "failed"
            return result, "ok"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
