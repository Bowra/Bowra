"""Bind prepared method input to current authority at every model send.

Construction grants nothing. Dispatch stores preparation evidence only; actual
provider invocation association is still required by the mixed runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from uuid import uuid4

from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.types import LLMRequest

from bowhead_core.skill_model_context import ModelContextReceipt, record_model_context
from bowhead_core.skill_read_access import ReferenceSkillReadAccess
from bowhead_core.skill_read_model_input import RequiredSkillModelInput
from bowhead_core.skill_read_render import RenderedSkillReads
from bowhead_core.tool_registry import ToolExecutionContext


@dataclass(frozen=True)
class AuthorizedSkillModelInput(RequiredSkillModelInput):
    access: ReferenceSkillReadAccess = field(repr=False)
    ctx: ToolExecutionContext = field(repr=False, compare=False)

    @classmethod
    def for_receiver(cls, rendered: RenderedSkillReads, *, access: ReferenceSkillReadAccess, ctx: ToolExecutionContext) -> AuthorizedSkillModelInput:
        # Construction happens before SubagentRunner enters its ContextVar.
        # It fixes bytes only; current receiver/authority is checked when used.
        required = RequiredSkillModelInput.from_rendered(rendered)
        return cls(required.content, required.content_hash, access, ctx)

    def attach(self, request: LLMRequest) -> LLMRequest:
        if request.input_context_id is not None:
            raise LLMInputAdmissionError("reference_model_context_already_attached")
        return replace(super().attach(request), input_context_id="ric_" + uuid4().hex)

    async def _require_current(self) -> None:
        try:
            if type(self.access) is not ReferenceSkillReadAccess or not isinstance(self.ctx, ToolExecutionContext):
                raise ValueError()
            rendered = await self.access.read_model_input(ctx=self.ctx)
            expected = RequiredSkillModelInput.from_rendered(rendered)
            if self.content != expected.content or self.content_hash != expected.content_hash:
                raise ValueError()
        except Exception:
            raise LLMInputAdmissionError("reference_model_authority_unavailable") from None

    async def validate(self, request: LLMRequest, *, provider: str, model: str, context_window: int | None) -> None:
        await super().validate(request, provider=provider, model=model, context_window=context_window)
        await self._require_current()

    async def validate_dispatch(self, request: LLMRequest, body: dict, *, provider: str, model: str, family: str) -> ModelContextReceipt:
        await super().validate_dispatch(request, body, provider=provider, model=model, family=family)
        try:
            return await record_model_context(request=request, access=self.access, ctx=self.ctx, content=self.content, content_hash=self.content_hash)
        except Exception:
            raise LLMInputAdmissionError("reference_model_authority_unavailable") from None
