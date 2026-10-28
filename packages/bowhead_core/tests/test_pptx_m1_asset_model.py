"""Sprint pptx-from-template M1 — unified asset model.

Covers RD-6 (kind/inject/trust typing + backfill), CR-1 (manifest helper),
derived_from version-chain integrity (write-time validation + bounded walk),
and AC-compat (legacy rows read with safe defaults).
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.artifact_store import (
    _SOURCE_TO_KIND,
    ArtifactStore,
    LocalBackend,
    _defaults_for_kind,
    _kind_for_source,
)


@pytest.fixture
async def store() -> ArtifactStore:
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "test.db"
    backend = LocalBackend(root_dir=tmp / "files")
    s = ArtifactStore(backend=backend, db_path=db_path)
    await s.initialize()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS session_attachments ("
            "session_id TEXT NOT NULL, artifact_id TEXT NOT NULL, "
            "pinned_at TEXT NOT NULL, detached_at TEXT, "
            "PRIMARY KEY (session_id, artifact_id))",
        )
        await db.commit()
    return s


async def _put(store: ArtifactStore, **kw) -> object:
    base = {"content": b"x", "filename": "f.txt", "mime": "text/plain", "created_by": None}
    base.update(kw)
    return await store.put_bytes(**base)


# ── kind/inject/trust resolution + round-trip ────────────────────────────────


def _literal_strings(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _literal_strings(node.body) | _literal_strings(node.orelse)
    return set()


def _local_literal_strings(function: ast.AST | None, name: ast.Name) -> set[str]:
    """Resolve a local variable only when every assignment to it in the enclosing function is literal."""
    if function is None:
        return set()
    assigned = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name.id for target in node.targets)
    ]
    other_bindings = [
        target
        for node in ast.walk(function)
        for target in (
            [node.target]
            if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr, ast.comprehension))
            else [item.optional_vars for item in node.items if item.optional_vars is not None]
            if isinstance(node, (ast.With, ast.AsyncWith))
            else []
        )
    ]
    rebound = any(isinstance(n, ast.Name) and n.id == name.id for target in other_bindings for n in ast.walk(target)) or any(
        isinstance(node, ast.arg) and node.arg == name.id for node in ast.walk(function)
    )
    resolved = [_literal_strings(value) for value in assigned]
    if rebound or not resolved or not all(resolved):
        return set()
    return set().union(*resolved)


def test_core_artifact_call_sources_fully_mapped() -> None:
    """Every literal source at a real core put call must use the registry."""
    src_root = Path(__file__).resolve().parents[1] / "src"
    produced: set[str] = set()
    unresolved: list[str] = []
    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        enclosing: dict[ast.AST, ast.AST] = {}
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for child in ast.walk(function):
                enclosing[child] = function  # inner functions are walked later and win
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else ""
            if name not in {"put_bytes", "put_file"}:
                continue
            source = next((kw.value for kw in call.keywords if kw.arg == "source"), None)
            if source is None:
                produced.add("upload" if name == "put_bytes" else "canvas_export")
                continue
            values = _literal_strings(source)
            if not values and isinstance(source, ast.Name):
                # A local bound only to literal sources is as reviewable as an inline literal.
                values = _local_literal_strings(enclosing.get(call), source)
            if values:
                produced.update(values)
            elif path.name != "artifact_store.py":
                unresolved.append(f"{path.relative_to(src_root)}:{call.lineno}")

    assert not unresolved, f"dynamic artifact sources require a registry-backed allowlist: {unresolved}"
    missing = produced - set(_SOURCE_TO_KIND)
    assert not missing, f"real core call sources missing from _SOURCE_TO_KIND: {missing}"


def test_source_to_kind_map() -> None:
    assert _kind_for_source("upload") == "user_upload"
    assert _kind_for_source("kb_upload") == "user_upload"
    assert _kind_for_source("bi_fetch_rows") == "tool_data"
    assert _kind_for_source("template_clone_section_data") == "tool_data"
    assert _kind_for_source("python_sandbox") == "sandbox_output"
    assert _kind_for_source("canvas_export_clone") == "canvas_export"
    assert _kind_for_source("canvas_export_skill") == "deliverable"
    assert _kind_for_source("web_search") == "web_clip"
    assert _kind_for_source("web_fetch") == "web_clip"
    assert _kind_for_source("image_generate") == "tool_data"
    assert _kind_for_source("design_flatten") == "canvas_export"
    assert _kind_for_source("consultant_deck") == "deliverable"
    assert _kind_for_source("kb_bundle_upload") == "user_upload"
    # unknown/new source → list-only tool_data (never floods)
    assert _kind_for_source("brand_new_source") == "tool_data"
    assert _defaults_for_kind("user_upload") == ("full", "trusted")
    assert _defaults_for_kind("web_clip") == ("stub", "unverified")


@pytest.mark.asyncio
async def test_put_bytes_types_from_source(store: ArtifactStore) -> None:
    art = await _put(store, source="bi_fetch_rows")
    got = await store.get(art.id)
    assert got.kind == "tool_data"
    assert got.inject == "list"
    assert got.trust == "trusted"
    assert got.scope == "session"
    assert got.producer_kind == "bi_fetch_rows"


@pytest.mark.asyncio
async def test_put_bytes_explicit_kind_overrides(store: ArtifactStore) -> None:
    art = await _put(
        store,
        source="python_sandbox",
        kind="deliverable",
        created_by_turn="t_abc",
        manifest_artifact_id="art_manifest",
    )
    got = await store.get(art.id)
    assert got.kind == "deliverable"
    assert got.inject == "stub"  # deliverable default
    assert got.trust == "trusted"
    assert got.created_by_turn == "t_abc"
    assert got.manifest_artifact_id == "art_manifest"


@pytest.mark.asyncio
async def test_web_clip_unverified(store: ArtifactStore) -> None:
    art = await _put(store, source="web_search")
    got = await store.get(art.id)
    assert got.kind == "web_clip"
    assert got.inject == "stub"
    assert got.trust == "unverified"


# ── derived_from validation (RD-6) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_derived_from_valid_parent_kept(store: ArtifactStore) -> None:
    parent = await _put(store, source="upload")
    child = await _put(store, source="python_sandbox", kind="deliverable", derived_from=parent.id)
    got = await store.get(child.id)
    assert got.derived_from == parent.id


@pytest.mark.asyncio
async def test_derived_from_dangling_parent_dropped(store: ArtifactStore) -> None:
    child = await _put(store, source="python_sandbox", kind="deliverable", derived_from="art_does_not_exist")
    got = await store.get(child.id)
    assert got.derived_from is None


@pytest.mark.asyncio
async def test_derived_from_soft_deleted_parent_dropped(store: ArtifactStore) -> None:
    parent = await _put(store, source="upload")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE artifacts SET deleted_at=? WHERE id=?",
            ("2026-05-29T00:00:00+00:00", parent.id),
        )
        await db.commit()
    child = await _put(store, source="python_sandbox", kind="deliverable", derived_from=parent.id)
    got = await store.get(child.id)
    assert got.derived_from is None


# ── walk_version_chain bounded + cycle-safe (CR-1) ───────────────────────────


@pytest.mark.asyncio
async def test_walk_version_chain(store: ArtifactStore) -> None:
    v1 = await _put(store, source="python_sandbox", kind="deliverable")
    v2 = await _put(store, source="python_sandbox", kind="deliverable", derived_from=v1.id)
    v3 = await _put(store, source="python_sandbox", kind="deliverable", derived_from=v2.id)
    chain = await store.walk_version_chain(v3.id)
    assert [a.id for a in chain] == [v3.id, v2.id, v1.id]


@pytest.mark.asyncio
async def test_walk_version_chain_cycle_safe(store: ArtifactStore) -> None:
    a = await _put(store, source="python_sandbox", kind="deliverable")
    b = await _put(store, source="python_sandbox", kind="deliverable", derived_from=a.id)
    # Force a cycle directly in the DB (bypassing write-time validation).
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET derived_from=? WHERE id=?", (b.id, a.id))
        await db.commit()
    chain = await store.walk_version_chain(b.id, max_depth=50)
    assert len(chain) <= 2  # revisit-set breaks the cycle, no hang


# ── get_manifest_for_deck (CR-1) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_manifest_for_deck(store: ArtifactStore) -> None:
    manifest = await _put(store, source="python_sandbox", filename="manifest.json", mime="application/json")
    deck = await _put(store, source="python_sandbox", kind="deliverable", manifest_artifact_id=manifest.id)
    got = await store.get_manifest_for_deck(deck.id)
    assert got is not None and got.id == manifest.id
    # deck without a manifest → None
    deck2 = await _put(store, source="python_sandbox", kind="deliverable")
    assert await store.get_manifest_for_deck(deck2.id) is None


# ── backfill behind user_version marker (RD-6) ───────────────────────────────


@pytest.mark.asyncio
async def test_backfill_does_not_clobber_explicit_kind(store: ArtifactStore) -> None:
    """Review P1: a re-run backfill (after a partial failure that didn't bump
    user_version) must NOT overwrite a row whose kind was explicitly set away
    from the source default (e.g. a deliverable written as source=python_sandbox
    but kind=deliverable). The producer_kind IS NULL guard protects it."""
    art = await _put(store, source="python_sandbox", kind="deliverable")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("PRAGMA user_version = 0")  # simulate un-bumped re-run
        await db.commit()
        await store._maybe_backfill_kinds(db)
    got = await store.get(art.id)
    assert got.kind == "deliverable"  # NOT clobbered to sandbox_output
    assert got.inject == "stub"


@pytest.mark.asyncio
async def test_backfill_refines_legacy_rows(store: ArtifactStore) -> None:
    # Simulate a legacy row: present columns but un-refined (producer_kind NULL,
    # default kind), and reset the version marker so backfill runs again.
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO artifacts(id, filename, mime, size, source, path, "
            "created_at, kind, inject, trust) VALUES "
            "('art_legacy1','a.csv','text/csv',3,'bi_fetch_rows','/tmp/a.csv',"
            "'2026-01-01','tool_data','list','trusted')",
        )
        await db.execute(
            "INSERT INTO artifacts(id, filename, mime, size, source, path, "
            "created_at) VALUES "
            "('art_legacy2','d.pptx','application/octet-stream',3,'web_search',"
            "'/tmp/d','2026-01-01')",
        )
        await db.execute("PRAGMA user_version = 0")
        await db.commit()
        await store._maybe_backfill_kinds(db)
    got = await store.get("art_legacy2")
    assert got.kind == "web_clip"
    assert got.inject == "stub"
    assert got.trust == "unverified"
    assert got.producer_kind == "web_search"
    assert store.backfill_degraded is False
