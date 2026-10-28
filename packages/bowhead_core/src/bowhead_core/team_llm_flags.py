"""Runtime feature flags for team LLM configuration.

Keep Gateway routes, health reporting and startup logs on the same parser so
operators never see a state different from the one that gates execution.
"""

from __future__ import annotations

import os
from typing import Any

_TRUE_VALUES = frozenset({"1", "true", "yes"})


def model_runtime_parameters_enabled() -> bool:
    """Gate admission and editing, never silently undo a saved sending policy."""
    return os.environ.get("FEATURE_MODEL_RUNTIME_PARAMETERS", "1").strip().lower() in _TRUE_VALUES


def runtime_parameter_changes_admitted(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Allow unchanged stored policies and removals when admission is closed."""
    if model_runtime_parameters_enabled():
        return True
    old_providers = old.get("providers") or {}
    new_providers = new.get("providers") or {}
    if not isinstance(old_providers, dict):
        old_providers = {}
    if not isinstance(new_providers, dict):
        return False
    previous = {
        (key, model.get("id")): model.get("runtime_parameters")
        for key, provider in old_providers.items()
        if isinstance(provider, dict)
        for model in provider.get("models") or []
        if isinstance(model, dict)
    }
    return all(
        not model.get("runtime_parameters") or model.get("runtime_parameters") == previous.get((key, model.get("id")))
        for key, provider in new_providers.items()
        if isinstance(provider, dict)
        for model in provider.get("models") or []
        if isinstance(model, dict)
    )
