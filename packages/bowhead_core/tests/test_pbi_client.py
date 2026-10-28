"""Power BI Service Principal client contracts."""

import bowhead_core.pbi_client as module
import pytest
from bowhead_core.pbi_client import PBI_SCOPE, PbiClient, PbiClientError, PbiTokenProvider


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body or {"access_token": "token", "expires_in": 3600}

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class TokenHttp:
    def __init__(self):
        self.calls = []

    async def post(self, url, data):
        self.calls.append((url, data))
        return Response()


async def test_client_credentials_token_is_cached_and_uses_fixed_scope() -> None:
    http = TokenHttp()
    provider = PbiTokenProvider({"tenant_id": "tenant", "client_id": "client", "client_secret": "secret"})
    assert await provider.get_token(http=http) == "token"
    assert await provider.get_token(http=http) == "token"
    assert len(http.calls) == 1
    url, form = http.calls[0]
    assert url.endswith("/tenant/oauth2/v2.0/token")
    assert form == {"grant_type": "client_credentials", "client_id": "client", "client_secret": "secret", "scope": PBI_SCOPE}


async def test_expired_token_is_renewed_before_next_request() -> None:
    http = TokenHttp()
    provider = PbiTokenProvider({"tenant_id": "tenant", "client_id": "client", "client_secret": "secret"})
    assert await provider.get_token(http=http) == "token"
    provider._refresh_at = 0  # noqa: SLF001 - prove expiry re-enters the client-credential flow.
    assert await provider.get_token(http=http) == "token"
    assert len(http.calls) == 2


@pytest.mark.parametrize(
    "status, code",
    [
        (400, "pbi_token_refresh_rejected"),
        (401, "pbi_token_refresh_rejected"),
        (403, "pbi_token_refresh_rejected"),
        (500, "pbi_token_refresh_failed"),
    ],
)
async def test_token_errors_are_safe(status, code) -> None:
    class Http:
        async def post(self, url, data):
            return Response(status, {"error_description": "secret provider text"})

    with pytest.raises(PbiClientError, match=code):
        await PbiTokenProvider({"tenant_id": "tenant", "client_id": "client", "client_secret": "secret"}).get_token(http=Http())


class StaticToken:
    async def get_token(self, *, http):
        return "token"


class QueryHttp:
    responses: list[Response] = []

    def __init__(self, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return self.responses.pop(0)


async def test_dax_retries_429_three_times_and_uses_30_second_timeout(monkeypatch) -> None:
    QueryHttp.responses = [Response(429), Response(429), Response(429)]
    monkeypatch.setattr(module.httpx2, "AsyncClient", QueryHttp)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    result = await PbiClient(workspace_id="w", dataset_id="d", token_provider=StaticToken()).execute_dax("EVALUATE X")
    assert result.error_code == "pbi_query_rate_limited"
    assert sleeps == [0.25, 0.5]


@pytest.mark.parametrize(
    "status, code",
    [(400, "pbi_query_invalid_request"), (401, "pbi_query_auth_rejected"), (403, "pbi_query_access_denied"), (404, "pbi_query_not_found")],
)
async def test_dax_maps_statuses(monkeypatch, status, code) -> None:
    QueryHttp.responses = [Response(status)]
    monkeypatch.setattr(module.httpx2, "AsyncClient", QueryHttp)
    result = await PbiClient(workspace_id="w", dataset_id="d", token_provider=StaticToken()).execute_dax("EVALUATE X")
    assert result.error_code == code
