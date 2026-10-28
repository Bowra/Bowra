"""workspace-datarc-decouple P2a — DatarcClientFactory pool tests."""

from __future__ import annotations

import pytest

from bowhead_bi_bridge import DatarcClient, DatarcClientFactory


async def test_caches_one_client_per_connection() -> None:
    f = DatarcClientFactory(timeout_sec=10)
    c1 = f.get_client("https://demo.datarc.cn", "key-a")
    c1_again = f.get_client("https://demo.datarc.cn", "key-a")
    assert c1 is c1_again  # same connection → reused instance
    assert isinstance(c1, DatarcClient)

    c2 = f.get_client("https://demo.datarc.cn", "key-b")  # different key → different client
    c3 = f.get_client("https://other.datarc.cn", "key-a")  # different base_url → different client
    assert c2 is not c1
    assert c3 is not c1
    assert f.size() == 3
    await f.aclose()


def test_pool_key_is_a_hash_not_the_raw_secret() -> None:
    key = DatarcClientFactory._key("https://demo.datarc.cn", "super-secret-key")
    assert "super-secret-key" not in key
    assert len(key) == 64  # sha256 hex digest


async def test_aclose_clears_pool() -> None:
    f = DatarcClientFactory()
    f.get_client("https://d", "k")
    assert f.size() == 1
    await f.aclose()
    assert f.size() == 0


async def test_factory_clients_block_metadata_at_runtime_in_saas(monkeypatch: pytest.MonkeyPatch) -> None:
    """[review P1-b] Factory clients (untrusted per-team base_urls) re-validate the target on every
    request — a base_url resolving to the cloud metadata address is blocked at call time, not
    just at create. (DatarcClient created directly, ssrf_guard off, would just connect.)"""
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    f = DatarcClientFactory()
    client = f.get_client("https://169.254.169.254", "k")  # metadata → must be blocked at request time
    with pytest.raises(Exception) as exc:  # SsrfBlocked (propagates through the guarded transport)
        await client.list_workspaces()
    msg = str(exc.value).lower()
    assert "public ip" in msg
    await f.aclose()
