"""chat-v4 W1.5 — memory_notes endpoint verification stub.

WHY (PRD §1.6 GAP item 7 / PRD §2 W1.5):
  V4 memory-notes was shipped in Bowhead 0.5.0 (V3.5 PRD-C). The chat-v4 PRD r2
  initially proposed cutting `.v4-fu-chip.is-note` ('+') and `.v4-er-btn` NOTE
  variant because it grep'd in the wrong direction. PM correction: verify the
  endpoint exists, then wire the chat affordance into chat-v4 follow-up chips.

ENDPOINT FINDINGS (verified 2026-05-17 against repo HEAD):
  Path prefix: /api/v1/memory  (registered in packages/bowhead_gateway/src/
    bowhead_gateway/main.py:1125-1127)
  Implementation: packages/bowhead_gateway/src/bowhead_gateway/routes/memory_notes.py
  Auth: Session cookie / bearer — extracted via `_user_id(request)` from request
    state. Routes mounted behind the gateway auth middleware (same pattern as
    other /api/v1/* routes).

  Verbs:
    GET    /api/v1/memory/notes                      → MemoryNoteDTO
    PATCH  /api/v1/memory/notes                      → MemoryNoteDTO (save content)
    POST   /api/v1/memory/notes/append               → MemoryNoteDTO (append bullet)
    DELETE /api/v1/memory/notes/bullet?bullet_index= → MemoryNoteDTO
    POST   /api/v1/memory/notes/lock                 → NotesLockResultDTO
    POST   /api/v1/memory/notes/unlock               → {ok: true}
    POST   /api/v1/memory/notes/save-from-canvas     → MemoryNoteDTO

  Storage: MemoryNotesStore (packages/bowhead_core/src/bowhead_core/memory_notes.py)
    attached to app.state.memory_notes_store (main.py:766).

WHAT chat-v4 NOTE CHIPS NEED:
  `.v4-fu-chip.is-note` ('+') onClick      → POST /api/v1/memory/notes/append
                                              body {bullet, source_turn_id, source}
  `.v4-er-btn` NOTE variant onClick        → POST /api/v1/memory/notes/append
                                              (same; pre-fill bullet from turn payload)

CONCLUSION: Endpoint EXISTS — chat-v4 F4 (follow-ups) and ER (error recovery)
should wire to /api/v1/memory/notes/append. NOT a 0.26.0 follow-up.

This test stub is intentionally lightweight (smoke-level only) — full route
behavior is already covered by:
  packages/bowhead_gateway/tests/test_memory_notes_routes.py
  packages/bowhead_gateway/tests/test_memory_anchor_e2e.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROUTES_FILE = _REPO_ROOT / "packages" / "bowhead_gateway" / "src" / "bowhead_gateway" / "routes" / "memory_notes.py"
_GATEWAY_MAIN = _REPO_ROOT / "packages" / "bowhead_gateway" / "src" / "bowhead_gateway" / "main.py"
_STORE_FILE = _REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "memory_notes.py"


def test_memory_notes_route_file_exists() -> None:
    """W1.5: routes/memory_notes.py exists in gateway — endpoint is real."""
    assert _ROUTES_FILE.is_file(), (
        f"chat-v4 W1.5 verification: expected route file missing → {_ROUTES_FILE}. "
        f"PRD r2 must cut .v4-fu-chip.is-note + .v4-er-btn NOTE if this is gone."
    )


def test_memory_notes_store_module_exists() -> None:
    """W1.5: memory_notes storage layer exists."""
    assert _STORE_FILE.is_file(), f"memory_notes store missing: {_STORE_FILE}"


def test_memory_notes_router_mounted_in_gateway() -> None:
    """W1.5: gateway main.py mounts memory_notes router at /api/v1/memory."""
    if not _GATEWAY_MAIN.is_file():
        pytest.skip(f"gateway main.py not found: {_GATEWAY_MAIN}")
    content = _GATEWAY_MAIN.read_text(encoding="utf-8")
    assert "memory_notes" in content, "gateway main.py never imports memory_notes routes"
    assert "/api/v1/memory" in content, "gateway main.py does not mount memory_notes at /api/v1/memory"


def test_append_endpoint_declared() -> None:
    """W1.5: /notes/append POST route exists (target for chat-v4 follow-up chips)."""
    if not _ROUTES_FILE.is_file():
        pytest.skip("routes file missing — covered by other test")
    content = _ROUTES_FILE.read_text(encoding="utf-8")
    assert "/notes/append" in content, "POST /notes/append endpoint not declared"
    assert "append_bullet" in content or "router.post" in content, "memory_notes router declarations look broken"
