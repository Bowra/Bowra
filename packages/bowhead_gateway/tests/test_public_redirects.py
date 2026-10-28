from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin

import httpx2
import pytest
from bowhead_core.public_entries import PublicEntryStore
from bowhead_gateway.main import create_app
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
@pytest.mark.parametrize("path,target", [("/probe", "/probe/"), ("/probe/", "/probe")])
def test_framework_slash_redirect_stays_on_browser_entry(monkeypatch: pytest.MonkeyPatch, prefix: str, path: str, target: str) -> None:
    monkeypatch.delenv("BOWHEAD_FRONTEND_STATIC_DIR", raising=False)
    app = create_app()

    @app.get(target)
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app, base_url="http://internal.example:8000", follow_redirects=False)
    response = client.get(f"{path}?token=a%2Fb&next=%2F", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "public.example"})
    assert response.status_code == 307
    assert urljoin(f"https://public.example{prefix}{path}", response.headers["location"]) == (
        f"https://public.example{prefix}{target}?token=a%2Fb&next=%2F"
    )
    assert client.get(response.headers["location"]).json() == {"ok": True}


def test_framework_slash_redirect_without_entry_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_FRONTEND_STATIC_DIR", raising=False)
    client = TestClient(create_app(), base_url="http://internal.example:8000", follow_redirects=False)
    response = client.get("/health/")
    assert response.status_code == 307
    assert urljoin("https://public.example/bowhead/health/", response.headers["location"]) == "https://public.example/bowhead/health"


def test_external_redirect_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_FRONTEND_STATIC_DIR", raising=False)
    app = create_app()

    @app.get("/provider")
    async def provider() -> RedirectResponse:
        return RedirectResponse("https://provider.example:8443/authorize?state=opaque")

    response = TestClient(app, follow_redirects=False).get("/provider")
    assert response.headers["location"] == "https://provider.example:8443/authorize?state=opaque"


@pytest.mark.asyncio
async def test_direct_registered_prefix_keeps_post_method_and_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed_community_user_store
) -> None:
    monkeypatch.delenv("BOWHEAD_FRONTEND_STATIC_DIR", raising=False)
    app = create_app()
    app.state.user_store = completed_community_user_store
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    pending, token = await entries.create_pending(team_id="team_a", base_url="http://public.example:10010/bowhead", actor_id="usr_owner")
    await entries.verify_challenge(entry_id=pending.id, token=token)
    app.state.public_entry_store = entries

    @app.post("/probe/")
    async def probe(body: dict[str, str]) -> dict[str, str]:
        return body

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://public.example:10010", follow_redirects=True) as client:
        response = await client.post("/bowhead/probe?next=%2F", json={"message": "keep this body"})
    assert response.status_code == 200
    assert str(response.url) == "http://public.example:10010/bowhead/probe/?next=%2F"
    assert response.history[0].status_code == 307
    assert response.json() == {"message": "keep this body"}


@pytest.mark.parametrize("name", ["caf%C3%A9", "report:2026"])
def test_slash_redirect_preserves_encoded_route_and_cannot_be_a_scheme(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from urllib.parse import unquote

    monkeypatch.delenv("BOWHEAD_FRONTEND_STATIC_DIR", raising=False)
    app = create_app()

    @app.get(f"/{unquote(name)}/")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    response = TestClient(app, base_url="http://internal.example:8000", follow_redirects=False).get(f"/{name}")
    assert response.status_code == 307
    assert urljoin(f"https://public.example/bowhead/{name}", response.headers["location"]) == f"https://public.example/bowhead/{name}/"
