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
    runningView: Literal["list", "chips", "lanes", "timeline"] | None = Field(
        default=None,
        description=(
            "How a running chat shows what its agents are doing, for this user only: list (one sentence per step, the default), "
            "chips (a small tag per step), lanes (a row of dots per agent) or timeline (steps drawn by their real duration). "
            "It changes only the display, never answers or outputs."
        ),
    )


PERSONAL_APPEARANCE_FIELDS = frozenset(PersonalAppearanceDraftDTO.model_fields)
# Chat running state R3.6: the run view is its own account setting (`running_view_style`), not a V3 theme override,
# so an older release that rejects unknown theme fields still reads Appearance after a rollback.
RUNNING_VIEW_FIELD = "runningView"
RUNNING_VIEW_KEY = "running_view_style"
RUNNING_VIEW_DEFAULT = "list"
RUNNING_VIEW_VALUES = frozenset({"list", "chips", "lanes", "timeline"})
THEME_FIELDS = PERSONAL_APPEARANCE_FIELDS - {RUNNING_VIEW_FIELD}


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


async def _running_view(ctx: FormContext) -> str | None:
    store = ctx.deps.get("user_settings_store")
    if store is None:
        raise RuntimeError("user settings unavailable")
    value = await store.get(ctx.user_id, RUNNING_VIEW_KEY)
    return value if value in RUNNING_VIEW_VALUES else None


async def personal_appearance_read(ctx: FormContext) -> dict[str, Any]:
    _, base, view = await _resource(ctx)
    payload = view.payload
    running_view = await _running_view(ctx)
    fields = {
        field: {
            "value": _flat(field, payload["effective"][field]),
            "unset": field not in payload["overrides"],
            "default": _flat(field, base.effective[field]),
        }
        for field in sorted(THEME_FIELDS)
    }
    fields[RUNNING_VIEW_FIELD] = {"value": running_view or RUNNING_VIEW_DEFAULT, "unset": running_view is None, "default": RUNNING_VIEW_DEFAULT}
    return {"schemaVersion": 3, "etag": view.etag, "fields": dict(sorted(fields.items()))}


async def personal_appearance_context(ctx: FormContext) -> str:
    return (
        "Personal appearance scalar choices. Propose only explicitly requested fields; theme fields save through the existing "
        "personal theme service and the run view through its own account setting."
    )


async def personal_appearance_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    patch = {field: value for field, value in data.items() if value is not None}
    theme_patch = {field: value for field, value in patch.items() if field in THEME_FIELDS}
    if not theme_patch:
        # The run view's allowed values are the schema's; nothing in the theme document changes.
        return MaterializeResult(normalized=patch)
    service, base, view = await _resource(ctx)
    candidate = copy.deepcopy(view.payload["overrides"])
    candidate.update({field: _to_override(field, value) for field, value in theme_patch.items()})
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


async def _apply_running_view(ctx: FormContext, row: dict[str, Any], op: str) -> AppliedChange:
    """Write the run view key inside its own transaction, comparing it with the suggestion's baseline."""
    store = ctx.deps.get("user_settings_store")
    if store is None:
        raise LiteApplyError("apply_validation_failed", "user settings unavailable")

    def stored(value: Any, unset: Any) -> str | None:
        # The list is the default: storing it means no personal choice, as the Appearance page and the card do.
        return None if unset or value in (None, RUNNING_VIEW_DEFAULT) else str(value)

    for value in (row.get("to"), None if row.get("from_unset") else row.get("from")):
        if value is not None and value not in RUNNING_VIEW_VALUES:
            raise LiteApplyError("apply_validation_failed", "invalid run view value")
    reset = row.get("operation") == "reset"
    target = stored(row.get("to"), reset) if op == "apply" else stored(row.get("from"), row.get("from_unset"))
    expected = stored(row.get("from"), row.get("from_unset")) if op == "apply" else stored(row.get("to"), reset)

    def mutate(before: str | None) -> str | None:
        current = before if before in RUNNING_VIEW_VALUES else None
        if current != expected and current != target:
            raise LiteApplyError("configuration_baseline_changed", "run view changed")
        return target

    before, after = await store.update_value(ctx.user_id, RUNNING_VIEW_KEY, mutate)
    return AppliedChange(scope="user", key=RUNNING_VIEW_KEY, before=before, after=after, operation="delete" if after is None else "put")


async def personal_appearance_lite_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str) -> list[AppliedChange]:
    """Write the run view key first, then the theme fields with V3 OCC; a theme failure puts the key back.

    One suggestion is applied whole or not at all: the apply route records nothing when the applier refuses, so a
    half-written suggestion would leave a change without its receipt or Undo.
    """
    if op not in {"apply", "undo"} or not rows:
        raise LiteApplyError("apply_validation_failed", "invalid personal appearance operation")
    view_rows = [row for row in rows if row.get("field") == RUNNING_VIEW_FIELD]
    theme_rows = [row for row in rows if row.get("field") != RUNNING_VIEW_FIELD]
    if len(view_rows) > 1:
        raise LiteApplyError("apply_validation_failed", "invalid personal appearance field")
    view_change = await _apply_running_view(ctx, view_rows[0], op) if view_rows else None
    if not theme_rows:
        return [view_change] if view_change else []
    try:
        theme_changes = await _apply_theme(ctx, theme_rows, op)
    except Exception:
        if view_change is not None:
            store = ctx.deps["user_settings_store"]
            restored = view_change.before

            def undo(current: str | None) -> str | None:
                return restored if current == view_change.after else current

            await store.update_value(ctx.user_id, RUNNING_VIEW_KEY, undo)
        raise
    return [*([view_change] if view_change else []), *theme_changes]


async def _apply_theme(ctx: FormContext, rows: list[dict[str, Any]], op: str) -> list[AppliedChange]:
    """Guard affected values, then use V3 OCC to preserve concurrent manual saves."""
    service, base, view = await _resource(ctx)
    changes: dict[str, Any] = {}
    for row in rows:
        field = row.get("field")
        if field not in THEME_FIELDS or field in changes:
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
