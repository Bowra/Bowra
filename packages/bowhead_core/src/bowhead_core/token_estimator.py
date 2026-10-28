"""Compatibility exports for the shared dependency-safe token estimator."""

from bowhead_router.token_estimator import (
    _enc,
    count_tokens_of_obj,
    estimate_tokens,
    get_encoder,
    truncate_to_tokens,
)

__all__ = ["_enc", "count_tokens_of_obj", "estimate_tokens", "get_encoder", "truncate_to_tokens"]
