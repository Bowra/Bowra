"""Code-owned Artifact Adapter, Prompt Module, and output grant contracts.

This M3-A module is deliberately side-effect free.  It validates exact-version
manifests and produces dispatch plans for the existing Tool registry; it does
not register tools, mutate Canvas, or replace the report event/replay path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bowhead_core.agents.exceptions import ManifestValidationError
from bowhead_core.agents.registry import _validate_path

if TYPE_CHECKING:
    from bowhead_core.agent_capability_catalog import CapabilityCatalog, CapabilityImplementation, PromptModuleRef

ARTIFACT_CAPABILITY_SCHEMA_VERSION = 1
ARTIFACT_CAPABILITY_COMPILER_VERSION = "artifact-capability-v1"

_PACKAGE_ROOT = Path(__file__).parent
_DEFAULT_MANIFEST_DIR = _PACKAGE_ROOT / "agents" / "manifests" / "artifacts"
_DEFAULT_PROMPT_ROOT = _PACKAGE_ROOT / "agents" / "prompts" / "capabilities"
_STABLE_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


class ExactComponentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_STABLE_ID_PATTERN)
    version: int = Field(ge=1)

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class ArtifactContractRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: str = Field(pattern=_STABLE_ID_PATTERN)
    version: int = Field(ge=1)

    @property
    def exact_ref(self) -> str:
        return f"{self.contract}.v{self.version}"


class PromptModuleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_STABLE_ID_PATTERN)
    version: int = Field(ge=1)
    lifecycle: Literal["active", "deprecated", "revoked"] = "active"
    replacement: ExactComponentRef | None = None
    path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    priority: int = Field(ge=1, le=10000)
    conflicts_with: tuple[str, ...] = ()
    # Only an executor holding at least one of these tools should read this section.
    #
    # The module declares this itself because Implementation's `required_tools` is
    # **empty** for `capi_report_canvas` (its dependencies are Adapter delegate_tools),
    # and `required_tools ⊆ tools` always holds for an empty set: everyone would match,
    # giving the canvas protocol to a peer without even `add_chart`.
    #
    # Deliberately unlike `required_tools`, this uses **any match**: one protocol often serves
    # a family of tools (charts/images, writes/reads). Empty adds no restriction; Implementation's `required_tools` decides.
    anchor_tools: tuple[str, ...] = ()

    @field_validator("anchor_tools")
    @classmethod
    def _anchors_are_stable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate Prompt Module anchor tool")
        for item in value:
            if not item or len(item) > 128:
                raise ValueError("invalid Prompt Module anchor tool")
        return value

    @field_validator("conflicts_with")
    @classmethod
    def _conflicts_are_stable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate Prompt Module conflict")
        for item in value:
            if not item or len(item) > 128:
                raise ValueError("invalid Prompt Module conflict")
        return value

    @model_validator(mode="after")
    def _revoked_has_replacement(self) -> PromptModuleDefinition:
        if self.lifecycle == "revoked" and self.replacement is None:
            raise ValueError("revoked Prompt Module requires replacement")
        return self

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class ArtifactAdapterOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal[
        "artifact_read",
        "artifact_apply_patch",
        "artifact_add_asset",
        "artifact_finalize",
        # canvas-update-turn：续轮修订——就地改上一轮的正文 / 章标题、收起上一轮被取代的图。
        # 与 artifacts/catalog-v1.yaml 的 canvas-report@1 同一提交改（加载期按这里校验）。
        "artifact_revise_prior",
        "artifact_retire_asset",
    ]
    delegate_tools: tuple[str, ...] = ()
    grant_operations: tuple[str, ...] = ()
    requires_expected_revision: bool = False

    @field_validator("delegate_tools", "grant_operations")
    @classmethod
    def _values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(re.fullmatch(r"^[A-Za-z][A-Za-z0-9_.:-]{0,199}$", item) is None for item in value):
            raise ValueError("Adapter dependencies must be unique stable values")
        return value


class ArtifactAdapterDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_STABLE_ID_PATTERN)
    version: int = Field(ge=1)
    lifecycle: Literal["active", "deprecated", "revoked"] = "active"
    replacement: ExactComponentRef | None = None
    output: ArtifactContractRef
    operations: tuple[ArtifactAdapterOperation, ...] = Field(min_length=1)
    prompt_modules: tuple[ExactComponentRef, ...] = ()

    @field_validator("prompt_modules")
    @classmethod
    def _prompt_modules_are_unique(
        cls,
        value: tuple[ExactComponentRef, ...],
    ) -> tuple[ExactComponentRef, ...]:
        refs = [item.exact_ref for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate Adapter Prompt Module")
        return value

    @field_validator("operations")
    @classmethod
    def _operations_are_unique(
        cls,
        value: tuple[ArtifactAdapterOperation, ...],
    ) -> tuple[ArtifactAdapterOperation, ...]:
        names = [item.operation for item in value]
        if len(names) != len(set(names)):
            raise ValueError("duplicate Adapter operation")
        return value

    @model_validator(mode="after")
    def _revoked_has_replacement(self) -> ArtifactAdapterDefinition:
        if self.lifecycle == "revoked" and self.replacement is None:
            raise ValueError("revoked Artifact Adapter requires replacement")
        return self

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class ArtifactGrantPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_STABLE_ID_PATTERN)
    version: int = Field(ge=1)
    lifecycle: Literal["active", "deprecated", "revoked"] = "active"
    output: ArtifactContractRef
    allowed_operations: tuple[str, ...] = Field(min_length=1)
    allowed_section_kinds: tuple[str, ...] = Field(min_length=1)
    max_sections: int = Field(ge=1, le=1000)
    max_bytes: int = Field(ge=1, le=100_000_000)
    ttl_seconds: int = Field(ge=1, le=86400)
    validators: tuple[str, ...] = Field(min_length=1)

    @field_validator("allowed_operations", "allowed_section_kinds", "validators")
    @classmethod
    def _policy_values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item or len(item) > 128 for item in value):
            raise ValueError("grant policy values must be unique stable values")
        return value

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class ArtifactManifestBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    prompt_modules: tuple[PromptModuleDefinition, ...] = ()
    adapters: tuple[ArtifactAdapterDefinition, ...] = ()
    grant_policies: tuple[ArtifactGrantPolicy, ...] = ()


@dataclass(frozen=True, slots=True)
class CompiledPromptModule:
    exact_ref: str
    sha256: str
    priority: int
    content: str


@dataclass(frozen=True, slots=True)
class CompiledPromptBundle:
    modules: tuple[CompiledPromptModule, ...]
    token_count: int
    fingerprint: str

    @property
    def content(self) -> str:
        return "\n\n".join(module.content for module in self.modules)


@dataclass(frozen=True, slots=True)
class ArtifactDispatchPlan:
    adapter_ref: str
    operation: str
    delegate_tools: tuple[str, ...]
    grant_operations: tuple[str, ...]
    requires_expected_revision: bool
    fingerprint: str


class ArtifactCapabilityRegistry:
    """Validated code-owned registry.  Prompt contents remain internal."""

    def __init__(
        self,
        *,
        prompt_modules: dict[tuple[str, int], tuple[PromptModuleDefinition, str]],
        adapters: dict[tuple[str, int], ArtifactAdapterDefinition],
        grant_policies: dict[tuple[str, int], ArtifactGrantPolicy],
    ) -> None:
        self._prompt_modules = prompt_modules
        self._adapters = adapters
        self._grant_policies = grant_policies
        self.fingerprint = _hash_json(
            {
                "schema_version": ARTIFACT_CAPABILITY_SCHEMA_VERSION,
                "prompt_modules": [item.model_dump(mode="json") for _, (item, _) in sorted(prompt_modules.items())],
                "adapters": [item.model_dump(mode="json") for _, item in sorted(adapters.items())],
                "grant_policies": [item.model_dump(mode="json") for _, item in sorted(grant_policies.items())],
            },
        )

    @classmethod
    def bootstrap(
        cls,
        manifest_dir: Path | None = None,
        prompt_root: Path | None = None,
    ) -> ArtifactCapabilityRegistry:
        manifests = manifest_dir or _DEFAULT_MANIFEST_DIR
        prompts = prompt_root or _DEFAULT_PROMPT_ROOT
        if not manifests.is_dir():
            raise ManifestValidationError(f"artifact manifest dir not found: {manifests}")
        if not prompts.is_dir():
            raise ManifestValidationError(f"Prompt Module dir not found: {prompts}")
        prompt_modules: dict[tuple[str, int], tuple[PromptModuleDefinition, str]] = {}
        adapters: dict[tuple[str, int], ArtifactAdapterDefinition] = {}
        policies: dict[tuple[str, int], ArtifactGrantPolicy] = {}
        for path in sorted(manifests.glob("*.yaml")):
            _validate_path(path, manifests)
            try:
                bundle = ArtifactManifestBundle.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            except Exception as exc:
                raise ManifestValidationError(f"failed to load artifact manifest {path}: {exc}") from exc
            for module in bundle.prompt_modules:
                key = (module.id, module.version)
                if key in prompt_modules:
                    raise ManifestValidationError(f"duplicate Prompt Module: {module.exact_ref}")
                module_path = prompts / module.path
                _validate_path(module_path, prompts)
                try:
                    raw = module_path.read_bytes()
                    if len(raw) > 262_144:
                        raise ManifestValidationError(f"Prompt Module exceeds size limit: {module.exact_ref}")
                    content = raw.decode("utf-8").strip()
                except (OSError, UnicodeError) as exc:
                    raise ManifestValidationError(f"failed to load Prompt Module {module.exact_ref}: {exc}") from exc
                if not content or f"sha256:{hashlib.sha256(raw).hexdigest()}" != module.sha256:
                    raise ManifestValidationError(f"Prompt Module digest mismatch: {module.exact_ref}")
                prompt_modules[key] = (module, content)
            for adapter in bundle.adapters:
                key = (adapter.id, adapter.version)
                if key in adapters:
                    raise ManifestValidationError(f"duplicate Artifact Adapter: {adapter.exact_ref}")
                adapters[key] = adapter
            for policy in bundle.grant_policies:
                key = (policy.id, policy.version)
                if key in policies:
                    raise ManifestValidationError(f"duplicate Artifact grant policy: {policy.exact_ref}")
                policies[key] = policy
        registry = cls(prompt_modules=prompt_modules, adapters=adapters, grant_policies=policies)
        registry._validate_references()
        return registry

    def _validate_references(self) -> None:
        for adapter in self._adapters.values():
            for ref in adapter.prompt_modules:
                module = self.get_prompt_module(ref.id, ref.version)[0]
                if module.lifecycle == "revoked":
                    raise ManifestValidationError(
                        f"{adapter.exact_ref} references revoked Prompt Module {module.exact_ref}",
                    )
        for module, _ in self._prompt_modules.values():
            if module.replacement is not None:
                replacement = self.get_prompt_module(module.replacement.id, module.replacement.version)[0]
                if replacement.exact_ref == module.exact_ref:
                    raise ManifestValidationError(f"invalid Prompt Module replacement: {module.exact_ref}")
        for adapter in self._adapters.values():
            if adapter.replacement is not None:
                replacement = self.get_adapter(adapter.replacement.id, adapter.replacement.version)
                if replacement.exact_ref == adapter.exact_ref:
                    raise ManifestValidationError(f"invalid Artifact Adapter replacement: {adapter.exact_ref}")

    def get_prompt_module(self, module_id: str, version: int) -> tuple[PromptModuleDefinition, str]:
        try:
            return self._prompt_modules[(module_id, version)]
        except KeyError as exc:
            raise ManifestValidationError(f"unknown Prompt Module: {module_id}@{version}") from exc

    def get_adapter(self, adapter_id: str, version: int) -> ArtifactAdapterDefinition:
        try:
            return self._adapters[(adapter_id, version)]
        except KeyError as exc:
            raise ManifestValidationError(f"unknown Artifact Adapter: {adapter_id}@{version}") from exc

    def get_grant_policy(self, policy_id: str, version: int) -> ArtifactGrantPolicy:
        try:
            return self._grant_policies[(policy_id, version)]
        except KeyError as exc:
            raise ManifestValidationError(f"unknown Artifact grant policy: {policy_id}@{version}") from exc

    def compile_prompt_bundle(
        self,
        refs: Iterable[PromptModuleRef],
        *,
        max_tokens: int,
        token_counter: Callable[[str], int],
    ) -> CompiledPromptBundle:
        if max_tokens < 1:
            raise ValueError("Prompt Module token budget must be positive")
        selected: dict[str, PromptModuleDefinition] = {}
        contents: dict[str, str] = {}
        for ref in refs:
            module, content = self.get_prompt_module(ref.id, ref.version)
            if module.lifecycle == "revoked" or module.sha256 != ref.sha256:
                raise ManifestValidationError(f"Prompt Module identity mismatch: {module.exact_ref}")
            previous = selected.get(module.id)
            if previous is not None and previous.version != module.version:
                raise ManifestValidationError(f"Prompt Module version conflict: {module.id}")
            selected[module.id] = module
            contents[module.exact_ref] = content
        for module in selected.values():
            conflicts = set(module.conflicts_with)
            selected_refs = {item.exact_ref for item in selected.values()}
            if conflicts.intersection(selected) or conflicts.intersection(selected_refs):
                raise ManifestValidationError(f"Prompt Module conflict: {module.exact_ref}")
        ordered = sorted(selected.values(), key=lambda item: (item.priority, item.id, item.version))
        compiled = tuple(
            CompiledPromptModule(
                exact_ref=module.exact_ref,
                sha256=module.sha256,
                priority=module.priority,
                content=contents[module.exact_ref],
            )
            for module in ordered
        )
        content = "\n\n".join(item.content for item in compiled)
        token_count = token_counter(content)
        if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
            raise ManifestValidationError("Prompt Module token counter returned an invalid value")
        if token_count > max_tokens:
            raise ManifestValidationError("Prompt Module token budget exceeded")
        return CompiledPromptBundle(
            modules=compiled,
            token_count=token_count,
            fingerprint=_hash_json(
                {
                    "compiler_version": ARTIFACT_CAPABILITY_COMPILER_VERSION,
                    "max_tokens": max_tokens,
                    "modules": [{"exact_ref": item.exact_ref, "priority": item.priority, "sha256": item.sha256} for item in compiled],
                    "token_count": token_count,
                },
            ),
        )

    def validate_capability_catalog(self, catalog: CapabilityCatalog) -> None:
        for implementation in catalog.implementations:
            self.validate_implementation(implementation)

    def validate_implementation(self, implementation: CapabilityImplementation) -> None:
        if implementation.artifact is None:
            if implementation.output_grant is not None:
                raise ManifestValidationError(
                    f"{implementation.exact_ref} has an output grant without an Artifact Adapter",
                )
            for ref in implementation.prompt_modules:
                module, _ = self.get_prompt_module(ref.id, ref.version)
                if module.lifecycle == "revoked" or module.sha256 != ref.sha256:
                    raise ManifestValidationError(f"Prompt Module identity mismatch: {implementation.exact_ref}")
            return
        adapter = self.get_adapter(
            implementation.artifact.adapter.id,
            implementation.artifact.adapter.version,
        )
        artifact_ref = f"{implementation.artifact.contract}.v{implementation.artifact.version}"
        if adapter.lifecycle == "revoked" or adapter.output.exact_ref != artifact_ref:
            raise ManifestValidationError(f"Artifact Adapter contract mismatch: {implementation.exact_ref}")
        declared_prompts = {(item.id, item.version): item.sha256 for item in implementation.prompt_modules}
        required_prompts = {(item.id, item.version) for item in adapter.prompt_modules}
        if set(declared_prompts) != required_prompts:
            raise ManifestValidationError(f"Artifact Adapter Prompt Module mismatch: {implementation.exact_ref}")
        for key, digest in declared_prompts.items():
            module, _ = self.get_prompt_module(*key)
            if module.sha256 != digest:
                raise ManifestValidationError(f"Prompt Module digest mismatch: {implementation.exact_ref}")
        if implementation.output_grant is None:
            raise ManifestValidationError(f"Artifact implementation requires output grant: {implementation.exact_ref}")
        policy = self.get_grant_policy(implementation.output_grant.id, implementation.output_grant.version)
        if policy.lifecycle == "revoked" or policy.output.exact_ref != artifact_ref:
            raise ManifestValidationError(f"Artifact grant contract mismatch: {implementation.exact_ref}")
        adapter_write_operations = {
            grant_operation for operation in adapter.operations for grant_operation in operation.grant_operations if grant_operation != "read"
        }
        if adapter_write_operations != set(policy.allowed_operations):
            raise ManifestValidationError(f"Artifact Adapter grant operation mismatch: {implementation.exact_ref}")

    @property
    def prompt_modules(self) -> tuple[PromptModuleDefinition, ...]:
        return tuple(item for _, (item, _) in sorted(self._prompt_modules.items()))

    @property
    def adapters(self) -> tuple[ArtifactAdapterDefinition, ...]:
        return tuple(item for _, item in sorted(self._adapters.items()))

    @property
    def grant_policies(self) -> tuple[ArtifactGrantPolicy, ...]:
        return tuple(item for _, item in sorted(self._grant_policies.items()))


class CanvasArtifactFacade:
    """Compile semantic Canvas operations into existing Tool dispatch plans."""

    def __init__(self, registry: ArtifactCapabilityRegistry) -> None:
        self._registry = registry

    def plan(self, adapter_ref: ExactComponentRef, operation: str) -> ArtifactDispatchPlan:
        adapter = self._registry.get_adapter(adapter_ref.id, adapter_ref.version)
        if adapter.lifecycle == "revoked":
            raise ManifestValidationError(f"revoked Artifact Adapter: {adapter.exact_ref}")
        selected = next((item for item in adapter.operations if item.operation == operation), None)
        if selected is None:
            raise ManifestValidationError(f"unsupported Artifact Adapter operation: {operation}")
        payload = {
            "adapter_ref": adapter.exact_ref,
            "delegate_tools": list(selected.delegate_tools),
            "grant_operations": list(selected.grant_operations),
            "operation": selected.operation,
            "requires_expected_revision": selected.requires_expected_revision,
        }
        return ArtifactDispatchPlan(
            adapter_ref=adapter.exact_ref,
            operation=selected.operation,
            delegate_tools=selected.delegate_tools,
            grant_operations=selected.grant_operations,
            requires_expected_revision=selected.requires_expected_revision,
            fingerprint=_hash_json(payload),
        )


@dataclass(frozen=True, slots=True)
class ArtifactWriteGrant:
    team_id: str
    user_id: str
    session_id: str
    turn_id: str
    agent_id: str
    agent_revision: str
    execution_id: str
    capability_execution_id: str
    canvas_id: str
    canvas_generation: int
    allowed_operations: tuple[str, ...]
    allowed_section_kinds: tuple[str, ...]
    max_sections: int
    max_bytes: int
    expires_at: datetime

    def __post_init__(self) -> None:
        identities = (
            self.team_id,
            self.user_id,
            self.session_id,
            self.turn_id,
            self.agent_id,
            self.agent_revision,
            self.execution_id,
            self.capability_execution_id,
            self.canvas_id,
        )
        if any(not value or len(value) > 200 for value in identities):
            raise ValueError("Artifact write grant identity is invalid")
        if not self.capability_execution_id.startswith("capx_"):
            raise ValueError("Artifact write grant Capability execution is invalid")
        if self.canvas_generation < 1 or self.max_sections < 1 or self.max_bytes < 1:
            raise ValueError("Artifact write grant limit is invalid")
        if self.expires_at.tzinfo is None:
            raise ValueError("Artifact write grant expiry must be timezone-aware")
        for values in (self.allowed_operations, self.allowed_section_kinds):
            if not values or len(values) != len(set(values)) or any(re.fullmatch(r"^[a-z][a-z0-9_.-]{1,127}$", item) is None for item in values):
                raise ValueError("Artifact write grant scope is invalid")


@dataclass(frozen=True, slots=True)
class ArtifactWriteAttempt:
    team_id: str
    user_id: str
    session_id: str
    turn_id: str
    agent_id: str
    agent_revision: str
    execution_id: str
    capability_execution_id: str
    canvas_id: str
    canvas_generation: int
    operation: str
    section_kind: str | None
    section_count: int
    payload_bytes: int
    owns_target_section: bool
    expected_revision: str | None
    current_revision: str | None
    now: datetime


def artifact_write_denial(grant: ArtifactWriteGrant, attempt: ArtifactWriteAttempt) -> str | None:
    exact_fields = (
        "team_id",
        "user_id",
        "session_id",
        "turn_id",
        "agent_id",
        "agent_revision",
        "execution_id",
        "capability_execution_id",
        "canvas_id",
        "canvas_generation",
    )
    if any(getattr(grant, field) != getattr(attempt, field) for field in exact_fields):
        return "artifact_grant_scope_mismatch"
    if attempt.now.tzinfo is None:
        return "artifact_time_invalid"
    now = attempt.now
    if now >= grant.expires_at:
        return "artifact_grant_expired"
    if attempt.operation not in grant.allowed_operations:
        return "artifact_operation_denied"
    if attempt.operation != "finalize" and attempt.section_kind not in grant.allowed_section_kinds:
        return "artifact_section_kind_denied"
    if (
        attempt.section_count < 0
        or attempt.payload_bytes < 0
        or attempt.section_count > grant.max_sections
        or attempt.payload_bytes > grant.max_bytes
    ):
        return "artifact_grant_limit_exceeded"
    if attempt.operation in {"patch_owned", "add_owned_asset"} and not attempt.owns_target_section:
        return "artifact_section_ownership_denied"
    # 收起上一轮的图（canvas-report@1 artifact_retire_asset，requires_expected_revision=false）：
    # `remove_chart` 不带 rev，按图 id 软折叠；其余写操作仍要 CAS。
    # ⚠ 豁免按操作名写死，须与 canvas-report@1 的 requires_expected_revision 保持一致。
    # security review P2：只收图——`section_kind` 必须取自库里那一段，不能取模型给的参数，否则「收图」能被拿去折叠正文。
    if attempt.operation == "retire_prior_asset":
        return None if attempt.section_kind in {"chart", "image"} else "artifact_section_kind_denied"
    if attempt.expected_revision is None or attempt.current_revision is None:
        return "artifact_revision_required"
    if attempt.expected_revision != attempt.current_revision:
        return "artifact_revision_conflict"
    return None


@dataclass(frozen=True, slots=True)
class ArtifactGenerationGrant:
    team_id: str
    user_id: str
    turn_id: str
    agent_id: str
    agent_revision: str
    execution_id: str
    capability_execution_id: str
    input_artifact_ids: tuple[str, ...]
    allowed_extensions: tuple[str, ...]
    workdir: str
    validators: tuple[str, ...]
    max_files: int
    expires_at: datetime

    def __post_init__(self) -> None:
        identities = (
            self.team_id,
            self.user_id,
            self.turn_id,
            self.agent_id,
            self.agent_revision,
            self.execution_id,
            self.capability_execution_id,
        )
        if any(not value or len(value) > 200 for value in identities):
            raise ValueError("Artifact generation identity is invalid")
        if not self.capability_execution_id.startswith("capx_"):
            raise ValueError("Artifact generation Capability execution is invalid")
        if self.max_files < 1 or self.expires_at.tzinfo is None:
            raise ValueError("Artifact generation grant is invalid")
        if len(self.workdir) > 1024 or "\x00" in self.workdir or not self.workdir.startswith("/") or ".." in PurePosixPath(self.workdir).parts:
            raise ValueError("Artifact generation workdir must be an absolute normalized path")
        if (
            not self.allowed_extensions
            or any(not item.startswith(".") or item != item.casefold() for item in self.allowed_extensions)
            or len(self.allowed_extensions) != len(set(self.allowed_extensions))
            or not self.validators
            or len(self.validators) != len(set(self.validators))
            or len(self.input_artifact_ids) != len(set(self.input_artifact_ids))
        ):
            raise ValueError("Artifact generation scope is invalid")


@dataclass(frozen=True, slots=True)
class ArtifactGenerationAttempt:
    team_id: str
    user_id: str
    turn_id: str
    agent_id: str
    agent_revision: str
    execution_id: str
    capability_execution_id: str
    input_artifact_ids: tuple[str, ...]
    output_paths: tuple[str, ...]
    validators: tuple[str, ...]
    now: datetime


def artifact_generation_denial(
    grant: ArtifactGenerationGrant,
    attempt: ArtifactGenerationAttempt,
) -> str | None:
    for field in (
        "team_id",
        "user_id",
        "turn_id",
        "agent_id",
        "agent_revision",
        "execution_id",
        "capability_execution_id",
    ):
        if getattr(grant, field) != getattr(attempt, field):
            return "artifact_generation_scope_mismatch"
    if attempt.now.tzinfo is None:
        return "artifact_generation_time_invalid"
    now = attempt.now
    if now >= grant.expires_at:
        return "artifact_generation_expired"
    if tuple(sorted(attempt.input_artifact_ids)) != tuple(sorted(grant.input_artifact_ids)):
        return "artifact_generation_input_denied"
    if not attempt.output_paths or len(attempt.output_paths) > grant.max_files:
        return "artifact_generation_file_limit_exceeded"
    if len(attempt.output_paths) != len(set(attempt.output_paths)):
        return "artifact_generation_path_denied"
    root = PurePosixPath(grant.workdir)
    for value in attempt.output_paths:
        if not value or len(value) > 1024 or "\x00" in value:
            return "artifact_generation_path_denied"
        path = PurePosixPath(value)
        if ".." in path.parts or not path.is_absolute() or not path.is_relative_to(root):
            return "artifact_generation_path_denied"
        if path.suffix.casefold() not in grant.allowed_extensions:
            return "artifact_generation_extension_denied"
    if not set(grant.validators).issubset(attempt.validators):
        return "artifact_generation_validator_missing"
    return None


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def default_artifact_registry() -> ArtifactCapabilityRegistry | None:
    """进程级 Artifact/Prompt 注册表（与 `default_catalog` 同形）。

    加载失败返 None —— 协议缺席只是回到没有它的状态，不该阻塞 turn 或 boot。
    有缓存是必要的：每次 bootstrap 都要读 manifest + 逐字节校验每份 Prompt Module。
    """
    try:
        return ArtifactCapabilityRegistry.bootstrap()
    except Exception:
        logger.warning("artifact_capability_registry.bootstrap_failed", exc_info=True)
        return None


def reset_artifact_registry_cache_for_test() -> None:
    default_artifact_registry.cache_clear()


def _hash_json(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


__all__ = [
    "ARTIFACT_CAPABILITY_COMPILER_VERSION",
    "ArtifactCapabilityRegistry",
    "ArtifactDispatchPlan",
    "ArtifactGenerationAttempt",
    "ArtifactGenerationGrant",
    "ArtifactWriteAttempt",
    "ArtifactWriteGrant",
    "CanvasArtifactFacade",
    "CompiledPromptBundle",
    "ExactComponentRef",
    "artifact_generation_denial",
    "artifact_write_denial",
    "default_artifact_registry",
    "reset_artifact_registry_cache_for_test",
]
