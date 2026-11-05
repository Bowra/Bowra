"""Task-owned text selections, independent of permanent Agent dependencies."""

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensions
from bowhead_core.chat_references import ContentHash, InputDocument, ReferenceNode, SkillResourceRef
from bowhead_core.sqlite_domains import domain_database_path

METHOD_REFERENCE_CONTRACT = "user-selected-method.v1"


async def task_method_reads_ready(db_path) -> bool:
    from bowhead_core._db import open_shared_ctx

    async with (
        open_shared_ctx(domain_database_path(db_path, "execution")) as db,
        db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_method_composition'") as cursor,
    ):
        return await cursor.fetchone() is not None


class TaskMethodReadPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract: Literal["user-selected-method.v1"] = METHOD_REFERENCE_CONTRACT
    input_digest: ContentHash
    resources: tuple[SkillResourceRef, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_resources(self) -> Self:
        if len({(item.source_kind, item.id) for item in self.resources}) != len(self.resources):
            raise ValueError("reference_method_selection_invalid")
        return self

    @property
    def digest(self) -> str:
        return canonical_fingerprint(self.model_dump(mode="json"))

    @property
    def skill_read(self) -> Self:
        return self

    @property
    def enabled(self) -> bool:
        return True

    def permits(self, resource: SkillResourceRef) -> bool:
        return resource in self.resources

    def require_selection(self, resources: Sequence[SkillResourceRef]) -> None:
        if not resources or len(resources) > 3 or any(not self.permits(item) for item in resources):
            raise ValueError("reference_method_selection_invalid")


def task_method_policy(document: InputDocument) -> TaskMethodReadPolicy:
    return TaskMethodReadPolicy(
        input_digest=canonical_fingerprint(document.model_dump(mode="json")),
        resources=tuple(node.resource for node in document.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, SkillResourceRef)),
    )


def selected_method_policy(
    document: InputDocument,
    grants: tuple[ExactSkillGrant, ...],
    published: AgentRuntimeExtensions | None,
) -> AgentRuntimeExtensions | TaskMethodReadPolicy | None:
    selected = tuple(node.resource for node in document.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, SkillResourceRef))
    outside = []
    for resource in selected:
        grant = next((item for item in grants if (item.kind, item.id) == (resource.source_kind, resource.id)), None)
        if grant is not None:
            if grant.revision != resource.version or "read" not in grant.permissions:
                raise ValueError("reference_skill_baseline_required")
        else:
            outside.append(resource)
    if not outside:
        return published
    if (
        published is not None
        and len(outside) <= published.skill_read.max_selected
        and all(published.skill_read.permits(resource) for resource in outside)
    ):
        return published
    return task_method_policy(document)
