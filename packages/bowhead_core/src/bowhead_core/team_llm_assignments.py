"""Repair model references and fill unconfigured roles from verified declarations."""

from __future__ import annotations

import copy
from typing import Any

from bowhead_router.model_capabilities import effective_model_capabilities

KB_AUTO_CAPABILITIES = {"rerank": ("rerank",), "vision": ("vision",), "row_extractor": ("chat", "tool"), "source_description": ("chat",)}


def reconcile_model_assignments(
    current: dict[str, Any],
    draft: dict[str, Any],
    verified: set[tuple[str, str, str]],
    *,
    verification_completed: bool = False,
    deletion_only: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep valid choices; auto-select only fresh, signature-matching evidence.

    Deleted connections can use a verified, configured embedding replacement. Optional explicit-off choices
    survive connection changes; source descriptions remain off by default.
    """
    result = copy.deepcopy(draft)
    if not isinstance(result.get("tiers"), dict):
        return result, []
    providers = result.get("providers") or {}
    old_providers = current.get("providers") or {}
    changed = {key for key in old_providers.keys() | providers.keys() if old_providers.get(key) != providers.get(key)}
    if deletion_only:
        changed = old_providers.keys() - providers.keys()
    removed = old_providers.keys() - providers.keys()
    changes: list[dict[str, Any]] = []

    def safe_reference(value: Any) -> Any:
        if isinstance(value, list):
            return [safe_reference(ref) for ref in value]
        if isinstance(value, dict):
            return {key: value[key] for key in ("provider", "model", "real_id", "follow", "auto") if key in value}
        return None

    def record(path: str, before: Any, after: Any) -> None:
        if before != after:
            existing = next((change for change in changes if change["path"] == path), None)
            if existing is not None:
                existing["selected"] = safe_reference(after)
            else:
                changes.append({"path": path, "previous": safe_reference(before), "selected": safe_reference(after)})

    def declared(pair: dict[str, str], required: tuple[str, ...]) -> bool:
        return all(cap in (effective_model_capabilities(providers, pair.get("provider", ""), pair.get("model", "")) or ()) for cap in required)

    def candidates(required: tuple[str, ...], *, introduced_only: bool = False) -> list[dict[str, str]]:
        found = []
        for key, provider in sorted(providers.items()):
            if introduced_only and not verification_completed and key not in changed:
                continue
            for model in sorted(provider.get("models") or [], key=lambda item: item if isinstance(item, str) else item["id"]):
                model_id = model if isinstance(model, str) else model["id"]
                pair = {"provider": key, "model": model_id}
                if declared(pair, required) and all((key, model_id, cap) in verified for cap in required):
                    found.append(pair)
        return found

    tiers = result["tiers"]
    for role, required in (("primary", ("chat", "tool")), ("fast", ("chat",)), ("judge", ("chat",))):
        ref = tiers.get(role)
        if isinstance(ref, dict) and ("follow" in ref or "auto" in ref):
            continue
        invalidated = bool(ref and ref.get("provider") in changed and not declared(ref, required))
        if invalidated or (not ref and role == "primary" and (changed or verification_completed)):
            options = candidates(required, introduced_only=not invalidated)
            replacement = options[0] if options else None
            if role == "fast" and replacement is None:
                replacement = {"follow": "primary"}
            if role == "judge" and replacement is None:
                replacement = {"auto": True}
            if replacement is None:
                tiers.pop(role, None)
            else:
                tiers[role] = replacement
            record(f"tiers.{role}", ref, replacement)
    fallbacks = tiers.get("primary_fallback", [])
    repaired = [
        ref for ref in fallbacks if ref != tiers.get("primary") and not (ref.get("provider") in changed and not declared(ref, ("chat", "tool")))
    ]
    if repaired != fallbacks:
        tiers["primary_fallback"] = repaired
        record("tiers.primary_fallback", fallbacks, repaired)

    kb = result.setdefault("kb", {"models": {}, "active": {}})
    models = kb.setdefault("models", {})
    active = kb.setdefault("active", {})
    old_kb = current.get("kb") or {}
    disabled = set(kb.get("auto_assign_disabled") or [])
    # Clearing a previously selected optional assignment is an explicit off action.
    for cap in KB_AUTO_CAPABILITIES:
        previous_name = (old_kb.get("active") or {}).get(cap)
        previous_provider = (old_kb.get("models") or {}).get(previous_name, {}).get("provider")
        if previous_name and cap not in active and previous_provider not in removed:
            disabled.add(cap)
    if disabled or "auto_assign_disabled" in kb:
        kb["auto_assign_disabled"] = sorted(disabled)

    invalid_models = set()
    for name, model in models.items():
        key = model.get("provider")
        if key not in changed:
            continue
        model_id = model.get("real_id") or model.get("model")
        required = KB_AUTO_CAPABILITIES.get(model.get("capability"), (model.get("capability"),))
        previous_catalog = old_providers.get(key, {}).get("models") or []
        was_declared = any((item if isinstance(item, str) else item.get("id")) == model_id for item in previous_catalog)
        # Legacy KB-only declarations remain authoritative until their connection is removed.
        if key not in providers or (was_declared and not declared({"provider": key, "model": model_id}, required)):
            invalid_models.add(name)
    removed_embed = models.get(active.get("embed"), {}).get("provider") in removed
    for cap, name in list(active.items()):
        if name not in invalid_models:
            continue
        before = models.get(name)
        active.pop(cap)
        record(f"kb.active.{cap}", before, None)
    for name in invalid_models:
        models.pop(name)
    if (kb.get("pending_embed") or {}).get("model") in invalid_models:
        kb.pop("pending_embed", None)

    if removed_embed and not active.get("embed"):
        for name, model in sorted(models.items()):
            pair = {"provider": model.get("provider"), "model": model.get("real_id")}
            if model.get("capability") == "embed" and isinstance(model.get("dim"), int) and model["dim"] > 0 and pair in candidates(("embed",)):
                active["embed"] = name
                record("kb.active.embed", None, model)
                break

    for cap, required in KB_AUTO_CAPABILITIES.items():
        if active.get(cap) or cap in disabled:
            continue
        was_active = bool((old_kb.get("active") or {}).get(cap))
        if cap == "source_description" and not was_active:
            continue
        if not (changed or verification_completed):
            continue
        options = candidates(required, introduced_only=not was_active)
        # The existing KB transport contract admits these roles only on compatible providers.
        options = [pair for pair in options if providers[pair["provider"]].get("type") in {"openai_compatible", "anthropic_compatible"}]
        if not options:
            continue
        pair = options[0]
        name = f"{cap}__{pair['provider']}__{pair['model']}"
        model = {"provider": pair["provider"], "real_id": pair["model"], "capability": cap}
        if cap == "rerank":
            declaration = next(item for item in providers[pair["provider"]]["models"] if isinstance(item, dict) and item.get("id") == pair["model"])
            model["protocol"] = declaration.get("protocol") or "openai_compat"
        models[name] = model
        active[cap] = name
        record(f"kb.active.{cap}", None, model)

    for section in ("media", "search"):
        config = result.get(section) or {}
        removed_models = {
            name
            for name, model in (config.get("models") or {}).items()
            if model.get("provider") in changed and model.get("provider") not in providers
        }
        for cap, name in list((config.get("active") or {}).items()):
            if name in removed_models:
                record(f"{section}.active.{cap}", config["models"][name], None)
                config["active"].pop(cap)
                for replacement_name, model in sorted(config["models"].items()):
                    if replacement_name not in removed_models and model.get("capability") == cap and model.get("provider") in providers:
                        config["active"][cap] = replacement_name
                        record(f"{section}.active.{cap}", None, model)
                        break
        for name in removed_models:
            config["models"].pop(name)
    # Review receives an already repaired page draft; retain the deletion impact there too.
    for role in ("primary", "fast", "judge"):
        previous = (current.get("tiers") or {}).get(role)
        if isinstance(previous, dict) and previous.get("provider") in removed:
            record(f"tiers.{role}", previous, tiers.get(role))
    for section in ("kb", "media", "search"):
        previous_section = current.get(section) or {}
        selected_section = result.get(section) or {}
        for cap, name in (previous_section.get("active") or {}).items():
            previous = (previous_section.get("models") or {}).get(name) or {}
            if previous.get("provider") in removed:
                selected = (selected_section.get("models") or {}).get((selected_section.get("active") or {}).get(cap))
                record(f"{section}.active.{cap}", previous, selected)
    return result, changes
