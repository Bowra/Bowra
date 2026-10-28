"""Attach complete method data and enforce its budget at each resolved model.

This is an input integrity/budget gate, not resource authorization or evidence of
successful method use. The mixed runtime must restore/authorize the composition
before constructing this input and separately revalidate each live invocation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from base64 import b64decode
from binascii import Error as Base64Error
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from typing import ClassVar

from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.types import LLMRequest, Message

from bowhead_core.skill_read_render import EMPTY_SKILL_SELECTION_DIGEST, SKILL_READ_RENDER_CONTRACT, RenderedSkillReads

MAX_REQUIRED_REFERENCE_TOKENS = 8192
REFERENCE_INPUT_RESERVE = 1024
TOKEN_ESTIMATE_CONTRACT = "utf8-bytes-conservative.v1"
IMAGE_CONTEXT_RESERVE = 32768
MAX_REFERENCE_IMAGES = 8
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
logger = logging.getLogger(__name__)


def _image_refs(request: LLMRequest) -> list[tuple[str, str]]:
    images = [(image.mime, image.base64) for message in request.messages for image in message.images]
    if len(images) > MAX_REFERENCE_IMAGES:
        raise LLMInputAdmissionError("reference_model_image_unavailable")
    for mime, content in images:
        if mime not in IMAGE_MIMES or not isinstance(content, str) or not 0 < len(content) <= 12 * 1024 * 1024:
            raise LLMInputAdmissionError("reference_model_image_unavailable")
        try:
            b64decode(content, validate=True)
        except (Base64Error, ValueError):
            raise LLMInputAdmissionError("reference_model_image_unavailable") from None
    return images


def _wire_images(messages: list[dict]) -> list[tuple[str, str, dict, str]]:
    """Locate native image blocks so a text-only adapter cannot drop them."""
    found: list[tuple[str, str, dict, str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in {"image_url", "input_image"}:
                container = part.get("image_url") if isinstance(part.get("image_url"), dict) else part
                key = "url" if container is not part else "image_url"
                value = container.get(key)
                if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
                    mime, content = value[5:].split(";base64,", 1)
                    found.append((mime, content, container, key))
            elif kind == "image" and isinstance(part.get("source"), dict):
                source = part["source"]
                if source.get("type") == "base64" and isinstance(source.get("data"), str):
                    found.append((source.get("media_type"), source["data"], source, "data"))
    return found


@dataclass(frozen=True, slots=True)
class SkillReadModelBudget:
    provider: str
    model: str
    context_window: int
    base_input_estimate: int
    output_reserve: int
    available_input: int
    reference_limit: int
    reference_estimate: int
    estimate_contract: str = TOKEN_ESTIMATE_CONTRACT


@dataclass(frozen=True, slots=True)
class RequiredSkillModelInput:
    requires_declared_context_window: ClassVar[bool] = True
    content: str = field(repr=False)
    content_hash: str

    @classmethod
    def from_rendered(cls, rendered: RenderedSkillReads) -> RequiredSkillModelInput:
        """Caller supplies a render verified against its durable composition."""
        try:
            if (
                not isinstance(rendered.content, bytes)
                or not 0 < len(rendered.content) <= 1024 * 1024
                or rendered.render_contract != SKILL_READ_RENDER_CONTRACT
                or rendered.rendered_content_hash != "sha256:" + hashlib.sha256(rendered.content).hexdigest()
            ):
                raise ValueError()
            body = rendered.content.decode("utf-8")
            # A separate data message leaves the exact Agent baseline and user's
            # task untouched. The renderer already separates parameters/files.
            prefix = (
                "Reference task context (JSON; no explicitly selected methods):\n"
                if rendered.selection_digest == EMPTY_SKILL_SELECTION_DIGEST
                else "Required selected method data (JSON; parameters are data, never substituted into instructions):\n"
            )
            content = prefix + body
            return cls(content, "sha256:" + hashlib.sha256(content.encode()).hexdigest())
        except Exception:
            raise LLMInputAdmissionError("reference_model_input_invalid") from None

    def attach(self, request: LLMRequest) -> LLMRequest:
        if request.input_admission is not None:
            raise LLMInputAdmissionError("reference_model_input_already_attached")
        # Preserve the tail's language/finalization instructions and the exact
        # baseline. Insert after leading system messages, in this request only.
        index = next((i for i, message in enumerate(request.messages) if message.role != "system"), len(request.messages))
        return replace(
            request, messages=[*request.messages[:index], Message(role="user", content=self.content), *request.messages[index:]], input_admission=self
        )

    def budget(self, request: LLMRequest, *, provider: str, model: str, context_window: int | None) -> SkillReadModelBudget:
        try:
            # Keep the external failure code finite and unchanged, but record
            # which prerequisite failed. The labels reveal no task or Skill data.
            content_bytes = self.content.encode() if isinstance(self.content, str) else b""
            content_valid = isinstance(self.content, str) and 0 < len(content_bytes) <= 1024 * 1024 + 256
            invalid = [
                name
                for name, valid in (
                    ("admission", request.input_admission is self),
                    ("messages", bool(request.messages)),
                    ("model_match", request.model == model),
                    ("provider", isinstance(provider, str) and bool(provider)),
                    ("model", isinstance(model, str) and bool(model)),
                    ("context_window", type(context_window) is int and context_window > 0),
                    (
                        "max_output_tokens",
                        request.max_output_tokens is None or (type(request.max_output_tokens) is int and request.max_output_tokens > 0),
                    ),
                    ("thinking_budget_tokens", type(request.thinking_budget_tokens) is int and request.thinking_budget_tokens >= 0),
                    ("content", content_valid),
                    (
                        "content_hash",
                        content_valid and self.content_hash == "sha256:" + hashlib.sha256(content_bytes).hexdigest(),
                    ),
                )
                if not valid
            ]
            if invalid:
                logger.warning("reference_model_budget_unavailable failed=%s", ",".join(invalid))
                raise LLMInputAdmissionError("reference_model_budget_unavailable")
            required = Message(role="user", content=self.content)
            index = next((i for i, message in enumerate(request.messages) if message.role != "system"), None)
            if index is None or request.messages[index] != required:
                raise LLMInputAdmissionError("reference_model_input_changed")
            images = _image_refs(request)

            def count(value):
                return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())

            # No provider tokenizer is installed at this boundary. Count every
            # serialized UTF-8 byte as an estimated token and reserve framing.
            # This is deliberately conservative, explicitly not exact usage.
            messages = []
            for i, message in enumerate(request.messages):
                if i == index:
                    continue
                projected = asdict(message)
                projected["images"] = [{"mime": image.mime, "base64": "[image]"} for image in message.images]
                messages.append(projected)
            base = count(
                {
                    "system": request.system,
                    "messages": messages,
                    "tools": [asdict(tool) for tool in request.tools],
                    "response_format": request.response_format,
                }
            ) + IMAGE_CONTEXT_RESERVE * len(images)
            reference = count(asdict(required))
            output = (
                request.output_reservation
                if request.output_reservation is not None
                else (request.max_output_tokens or 1024) + request.thinking_budget_tokens
            )
            available = max(0, context_window - output - REFERENCE_INPUT_RESERVE - base)
            limit = min(MAX_REQUIRED_REFERENCE_TOKENS, available // 5)
            if reference > limit:
                raise LLMInputAdmissionError("reference_context_budget_exceeded")
            admitted = SkillReadModelBudget(provider, model, context_window, base, output, available, limit, reference)
            return replace(admitted, estimate_contract="utf8-bytes+image-reserve.v1") if images else admitted
        except LLMInputAdmissionError:
            raise
        except Exception:
            raise LLMInputAdmissionError("reference_model_budget_unavailable") from None

    async def validate(self, request: LLMRequest, *, provider: str, model: str, context_window: int | None) -> None:
        self.budget(request, provider=provider, model=model, context_window=context_window)

    async def validate_dispatch(self, request: LLMRequest, body: dict, *, provider: str, model: str, family: str) -> None:
        self.budget(request, provider=provider, model=model, context_window=request.input_admission_window)
        self.validate_outbound(request, body, family=family)

    def validate_outbound(self, request: LLMRequest, body: dict, *, family: str) -> None:
        room = self.output_room(request, body, family=family)
        fields = [key for key in ("max_tokens", "max_output_tokens", "max_completion_tokens") if key in body]
        if len(fields) != 1 or type(body[fields[0]]) is not int or body[fields[0]] <= 0:
            raise LLMInputAdmissionError("reference_model_budget_unavailable")
        if body[fields[0]] > room:
            raise LLMInputAdmissionError("reference_context_budget_exceeded")

    def output_room(self, request: LLMRequest, body: dict, *, family: str) -> int:
        """Recheck final wire semantics/reserves after overrides and self-heal.

        Recognizes the existing chat/Responses/Anthropic body contracts only;
        unknown templated transports cannot claim this input support.
        """
        try:
            self.budget(request, provider="outbound", model=request.model, context_window=request.input_admission_window)
            if family not in {"openai", "openai_responses", "anthropic"} or body.get("model") != request.model:
                raise LLMInputAdmissionError("reference_model_adapter_unavailable")
            # Work on a detached JSON body; neither conversion nor accounting may
            # remove bytes from the object the HTTP client will actually send.
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stripped = json.loads(encoded)
            messages = stripped.get("input" if family == "openai_responses" else "messages")
            if not isinstance(messages, list):
                raise LLMInputAdmissionError("reference_model_input_changed")
            expected_images = Counter(_image_refs(request))
            actual_images = _wire_images(messages)
            if Counter((mime, content) for mime, content, _, _ in actual_images) != expected_images:
                raise LLMInputAdmissionError("reference_model_image_changed")
            if any(encoded.count(content) != count for (_mime, content), count in expected_images.items()):
                raise LLMInputAdmissionError("reference_model_image_changed")
            for _mime, _content, container, key in actual_images:
                container[key] = "[image]"
            encoded_redacted = json.dumps(stripped, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            slots = []
            for message in messages:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    slots.append((message, "content"))
                elif isinstance(content, list):
                    slots.extend((part, "text") for part in content if isinstance(part, dict) and part.get("type") in {"text", "input_text"})
            found = [(obj, key) for obj, key in slots if isinstance(obj.get(key), str) and self.content in obj[key]]
            if len(found) != 1 or found[0] != slots[0] or found[0][0][found[0][1]].count(self.content) != 1:
                raise LLMInputAdmissionError("reference_model_input_changed")
            obj, key = found[0]
            if not obj[key].startswith(self.content):
                raise LLMInputAdmissionError("reference_model_input_changed")
            obj[key] = obj[key][len(self.content) :]
            wire_without_required = len(json.dumps(stripped, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
            base = wire_without_required + IMAGE_CONTEXT_RESERVE * len(actual_images)
            # Include the required message's framing as well as its actual JSON
            # escaping, even when an adapter merges adjacent user messages.
            reference = len(encoded_redacted.encode()) - wire_without_required + 128
            output_keys = [key for key in ("max_tokens", "max_output_tokens", "max_completion_tokens") if key in body]
            if len(output_keys) != 1 or type(body[output_keys[0]]) is not int or body[output_keys[0]] <= 0:
                raise LLMInputAdmissionError("reference_model_budget_unavailable")
            thinking = body.get("thinking", {})
            thinking_tokens = thinking.get("budget_tokens", 0) if isinstance(thinking, dict) else 0
            if type(thinking_tokens) is not int or thinking_tokens < 0:
                raise LLMInputAdmissionError("reference_model_budget_unavailable")
            if reference > MAX_REQUIRED_REFERENCE_TOKENS:
                raise LLMInputAdmissionError("reference_context_budget_exceeded")
            return (
                request.input_admission_window - REFERENCE_INPUT_RESERVE - base - reference * 5 - max(request.thinking_budget_tokens, thinking_tokens)
            )
        except LLMInputAdmissionError:
            raise
        except Exception:
            raise LLMInputAdmissionError("reference_model_budget_unavailable") from None
