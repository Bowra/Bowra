"""Safe model settings projections; canonical saves remain in the original Review/Apply flow."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit

from bowhead_core.config_copilot import POLICY_GENERATED, FieldIssue, FormContext, FormRegistration, LiteApplyError, MaterializeResult, register_form
from bowhead_core.team_llm_config import SECRET_KEYS, TeamLlmConfigError, migrate_legacy_config_to_v2, validate_v2_config
from bowhead_router.retry import DEFAULT_MAX_RETRIES, MAX_RETRIES_LIMIT
from bowhead_router.runtime_parameters import PROFILES, runtime_parameters_schema
from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model

from bowhead_gateway.routes.admin_llm_config_v2 import V2ModelDeclaration, V2ProviderDraft

# These fields require dedicated identity, credential or transport review. New DTO fields
# propagate into the safe schema unless explicitly classified here by the coverage gate.
PROVIDER_MANUAL_FIELDS = {
    "provider_id": "Server-owned connection identity.",
    "runtime_key": "Existing connection key selects the object.",
    "api_key": "Credential entry stays in the original connection editor.",
    "default_headers": "Headers can contain credentials.",
    "auth": "Authentication changes need the original connection editor.",
    "transports": "Transport templates can contain credentials and need the original connection editor.",
}
MODEL_SERVER_FIELDS = {"declaration_origin": "Server-maintained declaration provenance."}


def _projection_fields(model: type[BaseModel], excluded: dict[str, str]) -> dict[str, Any]:
    result = {}
    for name, info in model.model_fields.items():
        if name in excluded:
            continue
        definition = copy.deepcopy(info)
        if not definition.description:
            definition.description = name.replace("_", " ").capitalize() + "; validated by the original model configuration service."
        result[name] = (info.annotation, definition)
    return result


model_fields = _projection_fields(V2ModelDeclaration, MODEL_SERVER_FIELDS)
model_fields.update(
    runtime_parameters=(
        dict[str, Any] | None,
        Field(
            default=None,
            description="Versioned model request policies. Use returned profiles and the nested schema; original runtime validation remains authoritative.",
            json_schema_extra={"anyOf": [runtime_parameters_schema(), {"type": "null"}]},
        ),
    ),
    capabilities=(
        list[str] | None,
        Field(
            default=None,
            min_length=1,
            max_length=5,
            description="Declared model capabilities; preserve existing declarations when editing parameters.",
        ),
    ),
    protocol=(str | None, Field(default=None, description="Embedding or rerank wire protocol accepted by the original service.")),
    credit_rates=(dict[str, str] | None, Field(default=None, description="Input, cached_input and output credit multipliers; decimal strings.")),
)
ModelSettings = create_model("ConfigurationModelSettings", __config__=ConfigDict(extra="forbid"), **model_fields)
provider_fields = _projection_fields(V2ProviderDraft, PROVIDER_MANUAL_FIELDS)
models_definition = copy.deepcopy(V2ProviderDraft.model_fields["models"])
models_definition.description = "Model declarations and runtime parameters. Preserve unrelated models and fields."
provider_fields["models"] = (list[ModelSettings | str], models_definition)
ProviderSettings = create_model("ConfigurationProviderSettings", __config__=ConfigDict(extra="forbid"), **provider_fields)


class TeamModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: dict[str, ProviderSettings] = Field(
        default_factory=dict,
        description="Existing connections keyed by their exact saved runtime key. Only non-secret configuration is editable; adding or removing connections uses the original connection workflow.",
    )
    tiers: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Model assignments: primary {provider, model}, primary_fallback list, fast/judge {provider, model}, {follow: primary} or {auto: true}, as accepted by the original service.",
    )
    kb: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Knowledge models, active embedding/rerank assignments and retrieval_top_k. Changing embeddings requires original reindex review. Index status is server-owned.",
    )
    media: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Image models and active assignments using existing Team connections; original capability and protocol validation applies.",
    )
    search: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Search models and active assignments using existing Team connections; original capability and protocol validation applies.",
    )


class ModelRetrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_retries: int = Field(
        default=DEFAULT_MAX_RETRIES,
        ge=0,
        le=MAX_RETRIES_LIMIT,
        description="Retries after a failed first model attempt. Zero disables retries; changes affect subsequent model calls.",
    )


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        if any(
            str(key).lower() in SECRET_KEYS
            or str(key).lower().endswith("_api_key")
            or str(key).lower() in {"api_key_ref", "default_headers", "headers", "auth", "transports", "fingerprint"}
            for key in value
        ):
            raise ValueError("Credentials, headers and transport templates must be edited on the original connection page.")
        for key in ("base_url", "endpoint"):
            if isinstance(value.get(key), str):
                url = urlsplit(value[key])
                if url.username or url.password or url.query or url.fragment:
                    raise ValueError("Connection URLs with credentials, queries or fragments require the original connection editor.")
        for item in value.values():
            _reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item)


def project_config(config: dict[str, Any]) -> dict[str, Any]:
    """Allow-list DTO fields before values enter model context or proposal storage."""
    providers = {}
    for key, provider in (config.get("providers") or {}).items():
        item = {name: copy.deepcopy(value) for name, value in provider.items() if name in ProviderSettings.model_fields}
        if "models" in item:
            item["models"] = [
                {name: value for name, value in model.items() if name in ModelSettings.model_fields} if isinstance(model, dict) else model
                for model in item["models"]
            ]
        providers[key] = item
    result = {"providers": providers, **{key: copy.deepcopy(config.get(key) or {}) for key in TeamModelSettings.model_fields if key != "providers"}}
    result["kb"] = {key: value for key, value in result["kb"].items() if key in {"models", "active", "retrieval_top_k"}}
    # The original editor materializes empty assignment collections on load. They
    # describe the same setting and must not make an unchanged proposal look edited.
    for section in ("kb", "media", "search"):
        result[section] = {"models": {}, "active": {}, **result[section]}
    # Do not silently redact a nested value that the model could later overwrite.
    _reject_secrets(result)
    return result


async def saved_config(ctx: FormContext) -> tuple[dict[str, Any], int]:
    saved, version = await ctx.deps["user_store"].get_team_llm_config_with_version(ctx.team_id)
    return migrate_legacy_config_to_v2(saved) if saved else {}, version


def merge_config(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    _reject_secrets(patch)
    result = copy.deepcopy(current)
    if "providers" in patch:
        if set(patch["providers"]) != set(current.get("providers") or {}):
            raise ValueError("Create or remove connections on the original connection page before proposing model settings.")
        for key, provider in patch["providers"].items():
            stored = result["providers"][key]
            original_models = {model["id"]: model for model in stored.get("models") or [] if isinstance(model, dict)}
            # Replace only admitted fields; credentials and transport settings remain server-owned.
            for name in ProviderSettings.model_fields:
                stored.pop(name, None)
            stored.update(copy.deepcopy(provider))
            if "models" in provider:
                stored["models"] = [
                    {
                        **{name: value for name, value in original_models.get(model["id"], {}).items() if name not in ModelSettings.model_fields},
                        **model,
                    }
                    if isinstance(model, dict)
                    else model
                    for model in provider["models"]
                ]
    for key, value in patch.items():
        if key == "providers":
            continue
        if key == "kb":
            if set(value) - {"models", "active", "retrieval_top_k"}:
                raise ValueError("Knowledge index state is server-owned.")
            result[key] = {
                **{name: val for name, val in (current.get(key) or {}).items() if name not in {"models", "active", "retrieval_top_k"}},
                **copy.deepcopy(value),
            }
        else:
            result[key] = copy.deepcopy(value)
    return result


async def model_read(ctx: FormContext) -> dict[str, Any]:
    config, version = await saved_config(ctx)
    return {
        "revision": version,
        "parameter_profiles": list(PROFILES.values()),
        "fields": {key: {"value": value, "unset": key not in config} for key, value in project_config(config).items()},
    }


async def model_materialize(ctx: FormContext, patch: dict[str, Any]) -> MaterializeResult:
    from bowhead_core.team_llm_flags import runtime_parameter_changes_admitted

    config, _ = await saved_config(ctx)
    try:
        candidate = merge_config(config, patch)
        if not runtime_parameter_changes_admitted(config, candidate):
            raise ValueError("Model runtime parameter editing is currently disabled.")
        validate_v2_config(candidate, allow_unassigned_tiers=True)
    except (ValueError, TeamLlmConfigError) as exc:
        return MaterializeResult(normalized={}, issues=[FieldIssue("_root", "invalid_model_configuration", str(exc))])
    return MaterializeResult(normalized=patch)


async def model_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str):
    raise LiteApplyError("model_review_required", "Open the saved proposal in the model settings editor, review its readiness and confirm Apply.")


async def check_model_proposal(request, user, proposal_id: str, candidate: dict[str, Any], version: int):
    """Bind a receipt to the original reviewed proposal before any canonical mutation."""
    from bowhead_gateway.routes.config_proposals import _error, _owned_lite_proposal

    draft = await _owned_lite_proposal(request, user, proposal_id)
    if draft.form_id != "team_model_config" or draft.status != "proposed":
        raise _error(409, "proposal_state_conflict", status=draft.status)
    rows = draft.payload["rows"]
    if not rows or any(row.get("baseline_revision") != version for row in rows):
        raise _error(409, "config_version_conflict")
    projected = TeamModelSettings.model_validate(project_config(candidate)).model_dump(mode="json", exclude_none=True)
    for row in rows:
        expected = TeamModelSettings.model_validate(project_config({row["field"]: row["to"]})).model_dump(mode="json", exclude_none=True)
        if projected.get(row["field"]) != expected.get(row["field"]):
            raise _error(409, "configuration_proposal_changed")
    return draft


async def retry_read(ctx: FormContext) -> dict[str, Any]:
    config, version = await saved_config(ctx)
    retry = config.get("retry") or {}
    return {
        "revision": version,
        "fields": {"max_retries": {"value": retry.get("max_retries", DEFAULT_MAX_RETRIES), "unset": "max_retries" not in retry}},
    }


async def retry_materialize(ctx: FormContext, patch: dict[str, Any]) -> MaterializeResult:
    return MaterializeResult(normalized=patch)


async def retry_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str):
    from bowhead_gateway.routes.admin_billing import LLMRetryPutBody, update_llm_retry

    if op != "apply" or len(rows) != 1 or rows[0].get("field") != "max_retries":
        raise LiteApplyError("apply_validation_failed", "Prepare a new retry proposal.")
    row = rows[0]
    ModelRetrySettings.model_validate({"max_retries": row["to"]}, strict=True)
    await update_llm_retry(
        LLMRetryPutBody(max_retries=row["to"], expected_version=row["baseline_revision"]),
        ctx.deps["request"],
        ctx.deps["actor"],
        ctx.deps["user_store"],
    )
    return []


def register_model_configuration_forms(with_deps) -> None:
    from bowhead_gateway.configuration_team_settings import domain_context, team_adapter

    for name, schema, reader, materializer, applier, review in (
        ("team_model_config", TeamModelSettings, model_read, model_materialize, model_apply, "/team/settings/models/"),
        ("team_model_retry", ModelRetrySettings, retry_read, retry_materialize, retry_apply, None),
    ):
        register_form(
            FormRegistration(
                form_id=name,
                schema=schema,
                guidance="Preserve unrequested nested values. Use exact existing connection/model identifiers from the read. Credentials, connection creation/removal and connection probes use the original page. Model changes require the original Review/Apply, risk acceptance and embedding reindex confirmation; proposals never authorize them.",
                field_policy=dict.fromkeys(schema.model_fields, POLICY_GENERATED),
                context_provider=team_adapter(with_deps, domain_context),
                materializer=team_adapter(with_deps, materializer),
                read_accessor=team_adapter(with_deps, reader),
                lite_applier=team_adapter(with_deps, applier),
                risk_tier=1,
                read_scope="team_admin",
                write_scope="team_admin",
                typed_patch=True,
                lite_revision_field="revision",
                review_page=review,
                write_descriptor="Original /api/v1/admin/llm-config Review/Apply or /retry",
            )
        )
