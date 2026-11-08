"""Provider objects keep credentials out of repr, so logs and error messages cannot print them."""

from __future__ import annotations

from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter

from bowhead_router import ProviderConfig

API_KEY = "sk-repr-test-key-must-not-appear"
HEADER_VALUE = "header-credential-must-not-appear"


def test_provider_objects_keep_credentials_out_of_repr() -> None:
    headers = {"X-Api-Key": HEADER_VALUE}
    objects = [
        ProviderConfig(name="p", type="openai_compatible", base_url="https://example.com", api_key=API_KEY, default_headers=headers),
        AnthropicCompatibleAdapter(base_url="https://example.com", api_key=API_KEY, default_headers=headers),
        OpenAICompatibleAdapter(base_url="https://example.com", api_key=API_KEY, default_headers=headers),
    ]
    for obj in objects:
        text = repr(obj)
        assert API_KEY not in text, type(obj).__name__
        assert HEADER_VALUE not in text, type(obj).__name__
        # Non-secret configuration stays visible for diagnosis.
        assert "https://example.com" in text, type(obj).__name__
