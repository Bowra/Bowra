"""Image input by target: which catalog models read images, and withholding for the others.

Visual self-check (docs/sprints/visual-self-check-2026-11-16/tech-design.md §3) shows a
rendered deliverable only to a model that declares image input. A request can still be
served by a fallback target without it; that target gets a one-line notice instead of each
render, so it neither receives bytes it silently drops nor believes it saw them. User
attachments are sent as before: many catalogs do not declare image input (D13, 2026-11-18).

This leaf module depends only on router types and the model directory, so the switch and
the capability reading have one definition shared by the router and ``bowhead_core``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from bowhead_router.model_capabilities import effective_model_capabilities
from bowhead_router.types import Message

VISUAL_SELF_CHECK_SWITCH = "FEATURE_VISUAL_SELF_CHECK"
WITHHELD_IMAGE_NOTICE = "[image withheld: this model does not accept images]"
_FALSY = frozenset({"0", "false", "no", "off"})


def visual_self_check_enabled() -> bool:
    """Default on since 2026-11-18 (maintainer); ``0`` turns it off. Read on each call."""

    return os.environ.get(VISUAL_SELF_CHECK_SWITCH, "1").strip().lower() not in _FALSY


def image_input_targets(providers: Any) -> frozenset[tuple[str, str]]:
    """Every catalog ``(provider, model)`` whose effective capabilities include ``vision``.

    Legacy string models inherit the provider's ``supports``; rich model entries use their
    own ``capabilities`` (``model_capabilities.effective_model_capabilities``).
    """

    if not isinstance(providers, dict):
        return frozenset()
    targets: set[tuple[str, str]] = set()
    for provider_name, provider in providers.items():
        models = provider.get("models") if isinstance(provider, dict) else None
        for model in models if isinstance(models, list) else []:
            model_id = model if isinstance(model, str) else str(model.get("id") or "") if isinstance(model, dict) else ""
            if not model_id:
                continue
            capabilities = effective_model_capabilities(providers, str(provider_name), model_id)
            if capabilities is not None and "vision" in capabilities:
                targets.add((str(provider_name), model_id))
    return frozenset(targets)


def withhold_images(messages: Iterable[Message], *, names: frozenset[str] | None = None) -> tuple[list[Message], int]:
    """Replace images with the notice; return the new messages and the count removed.

    ``names`` limits withholding to messages with those names (the router passes the render
    message only). Other messages are returned as the same objects, so callers that compare
    by identity (supplement removal, coordination rounds) keep working.
    """

    out: list[Message] = []
    withheld = 0
    for message in messages:
        if not message.images or (names is not None and message.name not in names):
            out.append(message)
            continue
        count = len(message.images)
        withheld += count
        notice = "\n".join([WITHHELD_IMAGE_NOTICE] * count)
        content = f"{message.content}\n{notice}" if message.content else notice
        out.append(replace(message, content=content, images=[]))
    return out, withheld
