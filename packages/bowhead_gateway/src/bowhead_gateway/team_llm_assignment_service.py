"""Server-authoritative assignment maintenance using the original evidence store."""

from __future__ import annotations

from typing import Any

from bowhead_core.team_llm_assignments import reconcile_model_assignments
from bowhead_core.team_llm_config import TeamLlmConfigError, is_v2_config, migrate_legacy_config_to_v2, validate_v2_config
from bowhead_core.team_llm_flags import model_assignment_reconciliation_enabled
from bowhead_core.team_llm_verification_store import TeamLlmVerificationStore

from bowhead_gateway.team_llm_readiness_service import catalog_verification_entries, provider_ids_for_config


async def prepare_assignment_changes(
    *, db_path: str, team_id: str, current: dict[str, Any], draft: dict[str, Any], verification_completed: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Review and Apply use the same evidence projection and deterministic choices."""
    if not model_assignment_reconciliation_enabled():
        return draft, []
    if current and not is_v2_config(current):
        current = migrate_legacy_config_to_v2(current)
    # Invalid input still belongs to the original validator, not the repair path.
    try:
        validate_v2_config({**draft, "tiers": {}, "kb": {}, "media": {}, "search": {}}, allow_unassigned_tiers=True)
    except TeamLlmConfigError:
        return draft, []
    tiers = draft.get("tiers")
    if not isinstance(tiers, dict) or any(not isinstance(tiers.get(key, {}), dict) for key in ("primary", "fast", "judge")):
        return draft, []
    if not isinstance(tiers.get("primary_fallback", []), list) or any(not isinstance(ref, dict) for ref in tiers.get("primary_fallback", [])):
        return draft, []
    for section in ("kb", "media", "search"):
        value = draft.get(section, {})
        if not isinstance(value, dict) or any(not isinstance(value.get(key, {}), dict) for key in ("models", "active")):
            return draft, []
        if any(not isinstance(model, dict) for model in value.get("models", {}).values()):
            return draft, []
    disabled = draft.get("kb", {}).get("auto_assign_disabled", [])
    if not isinstance(disabled, list) or any(not isinstance(cap, str) for cap in disabled):
        return draft, []
    identities = await provider_ids_for_config(
        team_id=team_id, config=draft, verification_store=TeamLlmVerificationStore(db_path), create_legacy_if_missing=False
    )
    entries = await catalog_verification_entries(db_path=db_path, team_id=team_id, config=draft, provider_ids=identities)
    runtime_keys = {value: key for key, value in identities.items()}
    verified = {(runtime_keys[row["provider_id"]], row["model_id"], row["capability"]) for row in entries if row["fact_state"] == "verified"}
    return reconcile_model_assignments(current, draft, verified, verification_completed=verification_completed)
