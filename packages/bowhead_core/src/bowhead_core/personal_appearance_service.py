"""Personal appearance V3 projection, validation, OCC, reset, and undo."""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from bowhead_core.personal_appearance_store import (
    PersonalAppearanceBaseGuard,
    PersonalAppearanceRows,
    PersonalAppearanceStore,
)
from bowhead_core.personal_theme_compiler import (
    PERSONAL_OVERLAY_VERSION,
    PersonalThemeBaseSchemeError,
    PersonalThemeCompiler,
    PersonalThemeCompilerError,
)
from bowhead_core.personal_theme_models import (
    PERSONAL_ACCENT_REGISTRY_V1,
    PERSONAL_BACKGROUND_REGISTRY_V1,
    PERSONAL_PALETTE_REGISTRY_V1,
    PERSONAL_THEME_OVERRIDE_FIELDS,
    PersonalThemeModelError,
    normalize_personal_theme_config,
    normalize_personal_theme_overrides,
    normalize_personal_theme_undo,
    parse_personal_theme_config,
    parse_personal_theme_undo,
    project_legacy_appearance_config,
)
from bowhead_core.theme_compiler import canonical_json
from bowhead_core.theme_config import ThemeConfigError, parse_appearance_theme_config

_RESET_TTL: Final = timedelta(minutes=10)
_SCALAR_FIELDS: Final = (
    "mode",
    "typographyProfile",
    "typeScale",
    "density",
    "radius",
    "motion",
)
_ACCENT_LABELS: Final = {
    "bowhead": "appearance.accent.bowhead",
    "ocean": "appearance.accent.ocean",
    "forest": "appearance.accent.forest",
    "plum": "appearance.accent.plum",
    "graphite": "appearance.accent.graphite",
}
_BACKGROUND_LABELS: Final = {
    "warm-paper": "appearance.background.warmPaper",
    "pure-white": "appearance.background.pureWhite",
    "cool-gray": "appearance.background.coolGray",
}
_PALETTE_LABELS: Final = {
    "bowhead-editorial": "appearance.palette.bowheadEditorial",
    "saas-blue": "appearance.palette.saasBlue",
    "product-indigo": "appearance.palette.productIndigo",
    "collaboration-teal": "appearance.palette.collaborationTeal",
    "focus-violet": "appearance.palette.focusViolet",
    "neutral-graphite": "appearance.palette.neutralGraphite",
}


class PersonalAppearanceError(RuntimeError):
    """Base personal appearance service error."""


class PersonalAppearancePreconditionRequired(PersonalAppearanceError):
    pass


class PersonalAppearanceConflict(PersonalAppearanceError):
    def __init__(self, *, current_etag: str, row_version: int) -> None:
        self.current_etag = current_etag
        self.row_version = row_version
        super().__init__("personal appearance changed")


class PersonalAppearanceCompileStale(PersonalAppearanceConflict):
    pass


class PersonalAppearanceInvalidDocument(PersonalAppearanceError):
    pass


class PersonalAppearanceBaseIncompatible(PersonalAppearanceError):
    pass


class PersonalAppearanceValidationFailed(PersonalAppearanceError):
    def __init__(self, validation: dict[str, Any]) -> None:
        self.validation = validation
        super().__init__("personal appearance validation failed")


class PersonalAppearanceUndoConflict(PersonalAppearanceError):
    pass


class PersonalAppearanceUndoExpired(PersonalAppearanceError):
    pass


@dataclass(frozen=True, slots=True)
class PersonalAppearanceBase:
    identity: str
    color_scheme: dict[str, Any]
    effective: dict[str, Any]
    sources: dict[str, str]
    team: dict[str, Any]
    guard: PersonalAppearanceBaseGuard


@dataclass(frozen=True, slots=True)
class PersonalAppearanceView:
    payload: dict[str, Any]
    etag: str
    rows: PersonalAppearanceRows
    config: dict[str, Any] | None
    config_checksum: str
    corrupt: bool


@dataclass(frozen=True, slots=True)
class PersonalAppearanceResetResult:
    resource: PersonalAppearanceView
    reset_id: str | None
    expires_at: str


class PersonalAppearanceService:
    def __init__(
        self,
        store: PersonalAppearanceStore,
        *,
        compiler: PersonalThemeCompiler | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.compiler = compiler or PersonalThemeCompiler()
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    async def get(
        self,
        *,
        user_id: str,
        team_id: str,
        base: PersonalAppearanceBase,
    ) -> PersonalAppearanceView:
        if base.guard.team_id != team_id:
            raise ValueError("personal appearance base guard has the wrong team")
        rows = await self.store.read(user_id)
        return self._view_from_rows(
            user_id=user_id,
            team_id=team_id,
            base=base,
            rows=rows,
        )

    async def patch(
        self,
        *,
        user_id: str,
        team_id: str,
        base: PersonalAppearanceBase,
        if_match: str | None,
        changes: dict[str, Any],
    ) -> PersonalAppearanceView:
        current = await self.get(user_id=user_id, team_id=team_id, base=base)
        self._require_match(current, if_match)
        if current.corrupt or current.config is None:
            raise PersonalAppearanceInvalidDocument("invalid persisted personal appearance")
        if not isinstance(changes, dict):
            raise PersonalThemeModelError("personal theme changes must be an object")
        unknown = sorted(set(changes) - PERSONAL_THEME_OVERRIDE_FIELDS)
        if unknown:
            raise PersonalThemeModelError(f"personal theme changes has unknown field(s): {', '.join(unknown)}")
        if not changes:
            raise PersonalThemeModelError("personal theme changes must not be empty")

        overrides = copy.deepcopy(current.config["overrides"])
        for field, value in changes.items():
            if value is None:
                overrides.pop(field, None)
            else:
                overrides[field] = value
        overrides = normalize_personal_theme_overrides(overrides)
        if overrides == current.config["overrides"]:
            return current
        self._validate_for_save(base.color_scheme, overrides)

        next_config: dict[str, Any] = {
            "version": 3,
            "rowVersion": current.payload["rowVersion"] + 1,
            "overrides": overrides,
        }
        if "origin" in current.config:
            next_config["origin"] = current.config["origin"]
        config_raw = normalize_personal_theme_config(_compact_json(next_config))
        committed = await self.store.compare_and_write(
            user_id=user_id,
            expected=current.rows,
            config_raw=config_raw,
            undo_raw=None,
            base_guard=base.guard,
        )
        if not committed:
            await self._raise_conflict(user_id=user_id, team_id=team_id, base=base)
        return await self.get(user_id=user_id, team_id=team_id, base=base)

    async def compile_candidate(
        self,
        *,
        user_id: str,
        team_id: str,
        base: PersonalAppearanceBase,
        base_etag: str,
        candidate: dict[str, Any],
        candidate_id: str,
    ) -> dict[str, Any]:
        current = await self.get(user_id=user_id, team_id=team_id, base=base)
        if base_etag != current.etag:
            raise PersonalAppearanceCompileStale(
                current_etag=current.etag,
                row_version=current.payload["rowVersion"],
            )
        normalized = normalize_personal_theme_overrides(candidate)
        try:
            result = self.compiler.compile(base.color_scheme, normalized)
        except PersonalThemeBaseSchemeError as exc:
            raise PersonalAppearanceBaseIncompatible("personal theme base is incompatible") from exc
        except PersonalThemeCompilerError as exc:
            raise PersonalAppearanceValidationFailed(_compiler_error_validation(normalized)) from exc
        return {
            "candidateId": candidate_id,
            "baseEtag": current.etag,
            "team": copy.deepcopy(base.team),
            "personalOverlayVersion": PERSONAL_OVERLAY_VERSION,
            "colorScheme": result.color_scheme,
            "inputSeeds": result.input_seeds,
            "validation": result.validation,
            "contextRecommendations": self._registries(base.color_scheme),
        }

    async def reset(
        self,
        *,
        user_id: str,
        team_id: str,
        base: PersonalAppearanceBase,
        if_match: str | None,
        idempotency_key: str,
    ) -> PersonalAppearanceResetResult:
        current = await self.get(user_id=user_id, team_id=team_id, base=base)
        digest = _digest(idempotency_key)
        receipt = _valid_receipt(current.rows.undo_raw)
        if (
            receipt is not None
            and receipt["idempotencyDigest"] == digest
            and not _is_expired(receipt, self._now())
            and _receipt_matches_current(receipt, current)
        ):
            return PersonalAppearanceResetResult(
                resource=current,
                reset_id=receipt["resetId"] if receipt["restorable"] else None,
                expires_at=receipt["expiresAt"],
            )
        self._require_match(current, if_match)

        next_row_version = 1 if current.corrupt else current.payload["rowVersion"] + 1
        next_config: dict[str, Any] = {
            "version": 3,
            "rowVersion": next_row_version,
            "overrides": {},
        }
        config_raw = normalize_personal_theme_config(_compact_json(next_config))
        expires_at = _format_time(self._now() + _RESET_TTL)
        reset_id = secrets.token_hex(16)
        before: dict[str, Any] | None = None
        before_checksum: str | None = None
        if not current.corrupt and current.config is not None:
            before = {
                "wasMaterialized": current.payload["materialized"],
                "overrides": copy.deepcopy(current.config["overrides"]),
            }
            if "origin" in current.config:
                before["origin"] = copy.deepcopy(current.config["origin"])
            before_checksum = _digest(canonical_json(before))
        receipt_raw = normalize_personal_theme_undo(
            _compact_json(
                {
                    "version": 1,
                    "resetId": reset_id,
                    "idempotencyDigest": digest,
                    "restorable": before is not None,
                    "before": before,
                    "beforeChecksum": before_checksum,
                    "expectedAfter": {
                        "rowVersion": next_row_version,
                        "checksum": _digest(config_raw),
                    },
                    "expiresAt": expires_at,
                }
            )
        )
        committed = await self.store.compare_and_write(
            user_id=user_id,
            expected=current.rows,
            config_raw=config_raw,
            undo_raw=receipt_raw,
            base_guard=base.guard,
        )
        if not committed:
            await self._raise_conflict(user_id=user_id, team_id=team_id, base=base)
        resource = await self.get(user_id=user_id, team_id=team_id, base=base)
        return PersonalAppearanceResetResult(
            resource=resource,
            reset_id=reset_id if before is not None else None,
            expires_at=expires_at,
        )

    async def undo(
        self,
        *,
        user_id: str,
        team_id: str,
        base: PersonalAppearanceBase,
        if_match: str | None,
        reset_id: str,
    ) -> PersonalAppearanceView:
        current = await self.get(user_id=user_id, team_id=team_id, base=base)
        receipt = _valid_receipt(current.rows.undo_raw)
        if receipt is not None and receipt["resetId"] == reset_id and _is_expired(receipt, self._now()):
            await self._delete_expired_receipt(user_id=user_id, current=current)
            raise PersonalAppearanceUndoExpired("personal appearance undo expired")
        self._require_match(current, if_match)
        if receipt is None or receipt["resetId"] != reset_id or not receipt["restorable"]:
            raise PersonalAppearanceUndoConflict("personal appearance undo is not available")
        if not _receipt_matches_current(receipt, current):
            raise PersonalAppearanceUndoConflict("personal appearance changed after reset")

        before = receipt["before"]
        if before is None or _digest(canonical_json(before)) != receipt["beforeChecksum"]:
            raise PersonalAppearanceUndoConflict("personal appearance undo checksum is invalid")
        self._validate_for_save(base.color_scheme, before["overrides"])
        next_config: dict[str, Any] = {
            "version": 3,
            "rowVersion": current.payload["rowVersion"] + 1,
            "overrides": before["overrides"],
        }
        if "origin" in before:
            next_config["origin"] = before["origin"]
        config_raw = normalize_personal_theme_config(_compact_json(next_config))
        committed = await self.store.compare_and_write(
            user_id=user_id,
            expected=current.rows,
            config_raw=config_raw,
            undo_raw=None,
            base_guard=base.guard,
        )
        if not committed:
            raise PersonalAppearanceUndoConflict("personal appearance changed during undo")
        return await self.get(user_id=user_id, team_id=team_id, base=base)

    def _view_from_rows(
        self,
        *,
        user_id: str,
        team_id: str,
        base: PersonalAppearanceBase,
        rows: PersonalAppearanceRows,
    ) -> PersonalAppearanceView:
        materialized = rows.config_raw is not None
        corrupt = False
        degradations: list[dict[str, str]] = []
        if materialized:
            try:
                config = parse_personal_theme_config(rows.config_raw or "")
                config_checksum = _digest(normalize_personal_theme_config(rows.config_raw or ""))
            except PersonalThemeModelError:
                config = None
                config_checksum = _digest(rows.config_raw or "")
                corrupt = True
                degradations.append({"field": "document", "reason": "invalid_persisted_document"})
        else:
            legacy = _parse_legacy(rows.legacy_raw)
            legacy_checksum = _digest(canonical_json(legacy))
            config = project_legacy_appearance_config(legacy, legacy_checksum=legacy_checksum)
            config_checksum = _digest(canonical_json(config))

        overrides = {} if config is None else config["overrides"]
        color_scheme, color_sources, color_degradations = self._resolve_colors(
            base.color_scheme,
            overrides,
            base_source=_source(base.sources.get("colorScheme", "product")),
        )
        degradations.extend(color_degradations)
        effective = copy.deepcopy(base.effective)
        sources = {field: _source(base.sources.get(field, "product")) for field in _SCALAR_FIELDS}
        for field in _SCALAR_FIELDS:
            if field in overrides:
                effective[field] = copy.deepcopy(overrides[field])
                sources[field] = "user"
        effective["colorScheme"] = color_scheme
        sources.update(color_sources)
        registries = self._registries(base.color_scheme)
        receipt = _valid_receipt(rows.undo_raw)
        if receipt is not None and _is_expired(receipt, self._now()):
            receipt = None
        pending_undo = {"resetId": receipt["resetId"], "expiresAt": receipt["expiresAt"]} if receipt is not None and receipt["restorable"] else None
        row_version = 0 if config is None else config["rowVersion"]
        payload: dict[str, Any] = {
            "schemaVersion": 3,
            "materialized": materialized,
            "rowVersion": row_version,
            "overrides": copy.deepcopy(overrides),
            "effective": effective,
            "sources": sources,
            "team": copy.deepcopy(base.team),
            "registries": registries,
            "degradations": degradations,
            "pendingUndo": pending_undo,
        }
        etag_material = {
            "userId": user_id,
            "teamId": team_id,
            "materialized": materialized,
            "rowVersion": row_version,
            "configChecksum": config_checksum,
            "baseIdentity": base.identity,
            "schemeChecksum": color_scheme["checksum"],
            "degradations": degradations,
            "registriesChecksum": _digest(canonical_json(registries)),
            "undo": ({"resetId": receipt["resetId"], "expiresAt": receipt["expiresAt"]} if receipt is not None else None),
            "personalOverlayVersion": PERSONAL_OVERLAY_VERSION,
        }
        etag = f'"personal-v3:{hashlib.sha256(canonical_json(etag_material).encode()).hexdigest()}"'
        return PersonalAppearanceView(
            payload=payload,
            etag=etag,
            rows=rows,
            config=config,
            config_checksum=config_checksum,
            corrupt=corrupt,
        )

    def _resolve_colors(
        self,
        base_scheme: dict[str, Any],
        overrides: dict[str, Any],
        *,
        base_source: str,
    ) -> tuple[dict[str, Any], dict[str, str], list[dict[str, str]]]:
        scheme = copy.deepcopy(base_scheme)
        sources = {"accent": base_source, "background": base_source}
        degradations: list[dict[str, str]] = []
        background = overrides.get("background")
        if background is not None:
            try:
                result = self.compiler.compile(scheme, {"background": background})
            except PersonalThemeBaseSchemeError:
                degradations.append({"field": "background", "reason": "base_incompatible"})
            except PersonalThemeCompilerError:
                degradations.append({"field": "background", "reason": "validation_failed"})
            else:
                if result.validation["blocking"]:
                    degradations.append({"field": "background", "reason": "validation_failed"})
                else:
                    scheme = result.color_scheme
                    sources["background"] = "user"
        accent = overrides.get("accent")
        if accent is not None:
            try:
                result = self.compiler.compile(scheme, {"accent": accent})
            except PersonalThemeBaseSchemeError:
                degradations.append({"field": "accent", "reason": "base_incompatible"})
            except PersonalThemeCompilerError:
                degradations.append({"field": "accent", "reason": "validation_failed"})
            else:
                if result.validation["blocking"]:
                    degradations.append({"field": "accent", "reason": "validation_failed"})
                else:
                    scheme = result.color_scheme
                    sources["accent"] = "user"
        return scheme, sources, degradations

    def _registries(self, base_scheme: dict[str, Any]) -> dict[str, Any]:
        palette_items = []
        for palette_id, palette in PERSONAL_PALETTE_REGISTRY_V1.items():
            value = {
                "accent": {"kind": "custom", "seed": palette["accent"]},
                "background": {
                    "kind": "custom",
                    "light": palette["background"]["light"],
                    "dark": palette["background"]["dark"],
                },
            }
            palette_items.append(
                self._registry_item(
                    base_scheme,
                    field="palette",
                    item_id=palette_id,
                    label_key=_PALETTE_LABELS[palette_id],
                    value=value,
                )
            )
        accent_items = []
        for accent_id in PERSONAL_ACCENT_REGISTRY_V1:
            value = {"kind": "preset", "registryVersion": 1, "id": accent_id}
            accent_items.append(
                self._registry_item(
                    base_scheme,
                    field="accent",
                    item_id=accent_id,
                    label_key=_ACCENT_LABELS[accent_id],
                    value=value,
                )
            )
        background_items = []
        for background_id in PERSONAL_BACKGROUND_REGISTRY_V1:
            value = {"kind": "preset", "registryVersion": 1, "id": background_id}
            background_items.append(
                self._registry_item(
                    base_scheme,
                    field="background",
                    item_id=background_id,
                    label_key=_BACKGROUND_LABELS[background_id],
                    value=value,
                )
            )
        return {
            "palette": {"version": 1, "items": palette_items},
            "accent": {"version": 1, "items": accent_items},
            "background": {"version": 1, "items": background_items},
        }

    def _registry_item(
        self,
        base_scheme: dict[str, Any],
        *,
        field: str,
        item_id: str,
        label_key: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        unavailable_reason = "validation_failed"
        try:
            overrides = value if field == "palette" else {field: value}
            result = self.compiler.compile(base_scheme, overrides)
        except PersonalThemeBaseSchemeError:
            unavailable_reason = "base_incompatible"
            result = None
        except PersonalThemeCompilerError:
            result = None
        if result is None or result.validation["blocking"]:
            return {
                "id": item_id,
                "labelKey": label_key,
                "value": value,
                "swatch": {},
                "availability": "unavailable",
                "reason": unavailable_reason,
            }
        if field == "accent":
            swatch = {
                mode: {
                    "primary": result.color_scheme[mode]["accent"],
                    "onPrimary": result.color_scheme[mode]["accentInk"],
                }
                for mode in ("light", "dark")
            }
        elif field == "background":
            swatch = {
                mode: {
                    "primary": result.color_scheme[mode]["background"],
                    "onPrimary": result.color_scheme[mode]["ink"],
                    "secondary": result.color_scheme[mode]["surface"],
                }
                for mode in ("light", "dark")
            }
        else:
            swatch = {
                mode: {
                    "primary": result.color_scheme[mode]["accent"],
                    "onPrimary": result.color_scheme[mode]["accentInk"],
                    "secondary": result.color_scheme[mode]["background"],
                }
                for mode in ("light", "dark")
            }
        return {
            "id": item_id,
            "labelKey": label_key,
            "value": value,
            "swatch": swatch,
            "availability": "available",
        }

    def _validate_for_save(
        self,
        base_scheme: dict[str, Any],
        overrides: dict[str, Any],
    ) -> None:
        try:
            result = self.compiler.compile(base_scheme, overrides)
        except PersonalThemeBaseSchemeError as exc:
            raise PersonalAppearanceBaseIncompatible("personal theme base is incompatible") from exc
        except PersonalThemeCompilerError as exc:
            raise PersonalAppearanceValidationFailed(_compiler_error_validation(overrides)) from exc
        if result.validation["blocking"]:
            raise PersonalAppearanceValidationFailed(result.validation)

    def _require_match(
        self,
        current: PersonalAppearanceView,
        if_match: str | None,
    ) -> None:
        if not if_match:
            raise PersonalAppearancePreconditionRequired("If-Match is required")
        if if_match != current.etag:
            raise PersonalAppearanceConflict(
                current_etag=current.etag,
                row_version=current.payload["rowVersion"],
            )

    async def _raise_conflict(
        self,
        *,
        user_id: str,
        team_id: str,
        base: PersonalAppearanceBase,
    ) -> None:
        current = await self.get(user_id=user_id, team_id=team_id, base=base)
        raise PersonalAppearanceConflict(
            current_etag=current.etag,
            row_version=current.payload["rowVersion"],
        )

    async def _delete_expired_receipt(
        self,
        *,
        user_id: str,
        current: PersonalAppearanceView,
    ) -> None:
        config_raw = current.rows.config_raw
        if config_raw is None:
            return
        committed = await self.store.compare_and_write(
            user_id=user_id,
            expected=current.rows,
            config_raw=config_raw,
            undo_raw=None,
        )
        if not committed:
            raise PersonalAppearanceUndoConflict("personal appearance changed during undo cleanup")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("personal appearance clock must be timezone-aware")
        return value.astimezone(UTC)


def _parse_legacy(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {"version": 1}
    try:
        return parse_appearance_theme_config(raw)
    except ThemeConfigError:
        return {"version": 1}


def _valid_receipt(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        return parse_personal_theme_undo(raw)
    except PersonalThemeModelError:
        return None


def _receipt_matches_current(
    receipt: dict[str, Any],
    current: PersonalAppearanceView,
) -> bool:
    expected = receipt["expectedAfter"]
    return (
        not current.corrupt
        and current.payload["materialized"]
        and expected["rowVersion"] == current.payload["rowVersion"]
        and expected["checksum"] == current.config_checksum
    )


def _is_expired(receipt: dict[str, Any], now: datetime) -> bool:
    expires = datetime.fromisoformat(receipt["expiresAt"].replace("Z", "+00:00"))
    return expires <= now


def _source(value: str) -> str:
    return "team_default" if value == "team_lock" else value


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _compiler_error_validation(overrides: dict[str, Any]) -> dict[str, Any]:
    field = "background" if "background" in overrides else "accent"
    item = {
        "code": "personal_overlay_unavailable",
        "severity": "blocking",
        "path": field,
        "message": "The selected personal color cannot be applied safely.",
    }
    return {
        "status": "blocking",
        "blockingCount": 1,
        "warningCount": 0,
        "blocking": [item],
        "warnings": [],
    }


__all__ = [
    "PersonalAppearanceBase",
    "PersonalAppearanceBaseIncompatible",
    "PersonalAppearanceCompileStale",
    "PersonalAppearanceConflict",
    "PersonalAppearanceError",
    "PersonalAppearanceInvalidDocument",
    "PersonalAppearancePreconditionRequired",
    "PersonalAppearanceResetResult",
    "PersonalAppearanceService",
    "PersonalAppearanceUndoConflict",
    "PersonalAppearanceUndoExpired",
    "PersonalAppearanceValidationFailed",
    "PersonalAppearanceView",
]
