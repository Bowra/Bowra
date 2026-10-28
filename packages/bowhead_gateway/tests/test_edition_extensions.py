"""Edition composition must not fall back across the Business boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from bowhead_gateway import edition_extensions


def _manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: object) -> None:
    path = tmp_path / "build_manifest.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setattr(edition_extensions, "BUILD_MANIFEST_PATH", path)


@pytest.mark.parametrize(
    "content",
    [
        {},
        {"schema_version": 2, "edition": "business"},
        {"schema_version": True, "edition": "community"},
        {"schema_version": 1, "edition": "automatic"},
        {"schema_version": 1, "edition": "community", "module": "arbitrary_module"},
    ],
    ids=["missing-fields", "unsupported-version", "boolean-version", "unknown-edition", "arbitrary-module"],
)
def test_invalid_build_identity_stops_composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: object) -> None:
    _manifest(tmp_path, monkeypatch, content)
    with pytest.raises(ValueError, match="Unsupported Gateway build manifest"):
        edition_extensions.load_gateway_extensions()


def test_missing_manifest_does_not_choose_an_edition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(edition_extensions, "BUILD_MANIFEST_PATH", tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        edition_extensions.load_gateway_extensions()


@pytest.mark.parametrize("message", ["Business package is absent", "Business dependency failed"])
def test_business_import_failure_stops_composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    _manifest(tmp_path, monkeypatch, {"schema_version": 1, "edition": "business"})

    def fail_import(name: str) -> None:
        assert name == "bowhead_enterprise.gateway"
        raise ImportError(message)

    monkeypatch.setattr(edition_extensions, "import_module", fail_import)
    with pytest.raises(ImportError, match=message):
        edition_extensions.load_gateway_extensions()


def test_incompatible_business_adapter_stops_composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _manifest(tmp_path, monkeypatch, {"schema_version": 1, "edition": "business"})
    monkeypatch.setattr(edition_extensions, "import_module", lambda _: SimpleNamespace(gateway_extensions=lambda: object()))
    with pytest.raises(TypeError, match="incompatible Gateway contract"):
        edition_extensions.load_gateway_extensions()


def test_community_composition_and_audit_recording_without_private_imports(tmp_path: Path) -> None:
    """Exercise a copied shared package with private imports unavailable."""
    target = tmp_path / "bowhead_gateway"
    shutil.copytree(Path(edition_extensions.__file__).parent, target, ignore=shutil.ignore_patterns("__pycache__"))
    (target / "build_manifest.json").write_text('{"schema_version": 1, "edition": "community"}', encoding="utf-8")
    script = textwrap.dedent(
        """
        import importlib.abc
        import os
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        sys.path.insert(0, sys.argv[1])

        class NoPrivateImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'bowhead_enterprise' or fullname.startswith('bowhead_enterprise.'):
                    raise ImportError('Private modules are unavailable in the Community candidate')
                return None

        sys.meta_path.insert(0, NoPrivateImports())
        import anyio
        import aiosqlite
        from bowhead_gateway import edition_extensions
        from bowhead_gateway.main import create_app
        from bowhead_core.user_store import UserStore
        from bowhead_core.artifact_store import ArtifactStore, LocalBackend
        from bowhead_gateway.public_branding import resolve_public_branding
        from bowhead_gateway.public_entry_context import PublicAccessContext
        from bowhead_gateway.routes.team_openapi import documentation

        assert Path(edition_extensions.__file__).parent == Path(sys.argv[1]) / 'bowhead_gateway'
        os.environ["BOWHEAD_DEPLOYMENT_MODE"] = "local"
        app = create_app()
        assert app.state.gateway_extensions.audit_csv_exporter is None

        async def record():
            artifacts = ArtifactStore(
                db_path=str(Path(sys.argv[1]) / 'recording.db'),
                backend=LocalBackend(root_dir=Path(sys.argv[1]) / 'artifacts'),
            )
            await edition_extensions.initialize_branding_services(app, artifacts)
            for prefix in ('', '/bowhead'):
                context = PublicAccessContext(
                    base_url='https://community.example' + prefix,
                    origin='https://community.example', base_path=prefix,
                    app_root='https://community.example' + prefix + '/',
                    match_source='entry_id', entry_id='pe_community', team_id='team_community',
                )
                payload = await resolve_public_branding(app, context)
                assert payload['brand']['applicationName'] == 'Bowra'
                assert payload['effective']['colorScheme']['light']['accent'] == '#1754DB'
            reference = await documentation(SimpleNamespace(app=app), caller=None)
            assert reference['scopes'] == ['agent_versions:read', 'agent_versions:write']
            assert not any(endpoint['path'].startswith(('/users', '/spaces')) for endpoint in reference['endpoints'])
            store = UserStore(Path(sys.argv[1]) / 'recording.db')
            await store.initialize()
            await edition_extensions.initialize_entitlement_services(app, store, "local")
            team = await store.create_team(name="Community")
            snapshot = await app.state.entitlements.get(team.id)
            assert snapshot.source == "community" and snapshot.max_seats is None

            # Exports render with the product-default print theme; no Business import is needed.
            from bowhead_core.print_theme import current_print_theme_snapshot
            from bowhead_gateway.routes.theme import resolve_export_print_theme

            class TeamPolicy:
                async def get(self, team_id, key):
                    return '{"version": 1, "defaults": {"colorProfile": {"type": "preset", "id": "forest"}}}'

            print_theme = await resolve_export_print_theme(
                SimpleNamespace(app=app), team_store=TeamPolicy(), team_id=team.id,
            )
            assert print_theme == current_print_theme_snapshot()
            await store.write_admin_audit_log(
                actor_user_id='usr_community', team_id='team_community',
                action='community_write', scope='team', payload={'recorded': True},
            )
            async with aiosqlite.connect(store.db_path) as db:
                async with db.execute('SELECT action FROM admin_audit_log') as cursor:
                    assert await cursor.fetchone() == ('community_write',)

        anyio.run(record)
        """
    )
    result = subprocess.run([sys.executable, "-I", "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
