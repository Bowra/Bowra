"""Bounded Chat projection of the existing personal appearance V3 service."""

from __future__ import annotations

import copy
from typing import Any, Literal

from bowhead_core.config_copilot import AppliedChange, FieldIssue, FormContext, LiteApplyError, MaterializeResult
from bowhead_core.personal_appearance_service import (
    PersonalAppearanceBase,
    PersonalAppearanceConflict,
    PersonalAppearanceInvalidDocument,
    PersonalAppearanceService,
    PersonalAppearanceValidationFailed,
    PersonalAppearanceView,
)
from bowhead_core.personal_theme_models import KEY_APPEARANCE_THEME_CONFIG_V3, PersonalThemeModelError, normalize_personal_theme_overrides
from pydantic import BaseModel, ConfigDict, Field


class PersonalAppearanceDraftDTO(BaseModel):
    """Scalar editor choices; field reset uses explicit operation metadata, never null."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["system", "light", "dark"] | None = Field(default=None, description="Personal color mode; system follows the device preference.")
    typographyProfile: Literal["bowhead", "system", "readable", "serif-display"] | None = Field(
        default=None, description="Personal font profile, separate from text size. Use a returned preset identifier."
    )
    typeScale: Literal["compact", "standard", "large", "extra-large"] | None = Field(default=None, description="Personal text size preset.")
    density: Literal["comfortable", "compact", "spacious"] | None = Field(default=None, description="Personal spacing density.")
    radius: Literal["balanced", "sharp", "soft"] | None = Field(default=None, description="Personal corner shape preset.")
    motion: Literal["standard", "reduced"] | None = Field(default=None, description="Personal animation preference; reduced limits motion.")


PERSONAL_APPEARANCE_FIELDS = frozenset(PersonalAppearanceDraftDTO.model_fields)


def _to_override(field: str, value: Any) -> Any:
    return {"type": "preset", "id": value} if field == "typographyProfile" and value is not None else value


def _flat(field: str, value: Any) -> Any:
    return value.get("id") if field == "typographyProfile" and isinstance(value, dict) else value


async def _resource(ctx: FormContext) -> tuple[PersonalAppearanceService, PersonalAppearanceBase, PersonalAppearanceView]:
    service = ctx.deps.get("personal_appearance_service")
    provider = ctx.deps.get("personal_appearance_base_provider")
    if service is None or provider is None or not ctx.team_id:
        raise RuntimeError("personal appearance service unavailable")
    base = await provider(ctx)
    view = await service.get(user_id=ctx.user_id, team_id=ctx.team_id, base=base)
    if view.corrupt or view.config is None:
        raise PersonalAppearanceInvalidDocument("invalid persisted personal appearance")
    return service, base, view


async def personal_appearance_read(ctx: FormContext) -> dict[str, Any]:
    _, base, view = await _resource(ctx)
    payload = view.payload
    return {
        "schemaVersion": 3,
        "etag": view.etag,
        "fields": {
            field: {
                "value": _flat(field, payload["effective"][field]),
                "unset": field not in payload["overrides"],
                "default": _flat(field, base.effective[field]),
            }
            for field in sorted(PERSONAL_APPEARANCE_FIELDS)
        },
    }


async def personal_appearance_context(ctx: FormContext) -> str:
    return "Personal appearance V3 scalar choices. Propose only explicitly requested fields; confirmation uses the existing personal theme service."


async def personal_appearance_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    patch = {field: value for field, value in data.items() if value is not None}
    service, base, view = await _resource(ctx)
    candidate = copy.deepcopy(view.payload["overrides"])
    candidate.update({field: _to_override(field, value) for field, value in patch.items()})
    try:
        candidate = normalize_personal_theme_overrides(candidate)
        compiled = await service.compile_candidate(
            user_id=ctx.user_id,
            team_id=ctx.team_id,
            base=base,
            base_etag=view.etag,
            candidate=candidate,
            candidate_id="configuration-agent",
        )
        if compiled["validation"]["blocking"]:
            raise PersonalAppearanceValidationFailed(compiled["validation"])
    except (PersonalThemeModelError, PersonalAppearanceValidationFailed) as exc:
        return MaterializeResult(normalized={}, issues=[FieldIssue(field="_root", code="theme_invalid", message=str(exc), repairable=False)])
    return MaterializeResult(normalized=patch)


async def personal_appearance_lite_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str) -> list[AppliedChange]:
    """Guard affected values, then use V3 OCC to preserve concurrent manual saves."""
    if op not in {"apply", "undo"} or not rows:
        raise LiteApplyError("apply_validation_failed", "invalid personal appearance operation")
    service, base, view = await _resource(ctx)
    changes: dict[str, Any] = {}
    for row in rows:
        field = row.get("field")
        if field not in PERSONAL_APPEARANCE_FIELDS or field in changes:
            raise LiteApplyError("apply_validation_failed", "invalid personal appearance field")
        if not isinstance(row.get("to"), str) or (not row.get("from_unset") and not isinstance(row.get("from"), str)):
            raise LiteApplyError("apply_validation_failed", "invalid personal appearance value")
        if op == "apply":
            if row.get("operation") == "reset" and _flat(field, base.effective[field]) != row.get("to"):
                raise LiteApplyError("configuration_baseline_changed", "The inherited appearance changed. Review the new default before resetting.")
            desired = None if row.get("operation") == "reset" else _to_override(field, row.get("to"))
            current = view.payload["overrides"].get(field)
            expected = None if row.get("from_unset") else _to_override(field, row.get("from"))
            # Inherited effective values can change with the Team base too.
            if current != desired and (
                current != expected or (row.get("from_unset") and _flat(field, view.payload["effective"][field]) != row.get("from"))
            ):
                raise LiteApplyError("configuration_baseline_changed", "personal appearance changed")
        else:
            expected = None if row.get("operation") == "reset" else _to_override(field, row.get("to"))
            desired = None if row.get("from_unset") else _to_override(field, row.get("from"))
            current = view.payload["overrides"].get(field)
            if current != expected and current != desired:
                raise LiteApplyError("configuration_baseline_changed", "personal appearance changed")
        changes[field] = desired
    try:
        # Validate every candidate before the service's single atomic write.
        normalize_personal_theme_overrides({field: value for field, value in changes.items() if value is not None})
        updated = await service.patch(user_id=ctx.user_id, team_id=ctx.team_id, base=base, if_match=view.etag, changes=changes)
    except PersonalAppearanceConflict as exc:
        raise LiteApplyError("configuration_baseline_changed", "personal appearance changed") from exc
    except (PersonalThemeModelError, PersonalAppearanceValidationFailed) as exc:
        raise LiteApplyError("apply_validation_failed", str(exc)) from exc
    return [
        AppliedChange(
            scope="user",
            key=KEY_APPEARANCE_THEME_CONFIG_V3,
            before=view.rows.config_raw,
            after=updated.rows.config_raw,
            operation="put",
        )
    ]
