"""Team-owned knowledge search defaults and request bounds."""

DEFAULT_TOP_K = 15
MAX_TOP_K = 50


def retrieval_top_k(kb: dict) -> int:
    value = kb.get("retrieval_top_k", DEFAULT_TOP_K)
    if type(value) is not int or not 1 <= value <= MAX_TOP_K:
        raise ValueError(f"kb.retrieval_top_k must be an integer from 1 to {MAX_TOP_K}")
    return value
