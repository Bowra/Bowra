"""Attach complete method data and verify it at each resolved model.

This is an input integrity gate, not resource authorization or evidence of
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
from dataclasses import dataclass, field, replace
from typing import ClassVar

from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.types import LLMRequest, Message

from bowhead_core.skill_read_render import EMPTY_SKILL_SELECTION_DIGEST, SKILL_READ_RENDER_CONTRACT, RenderedSkillReads

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

    def _validate_input(self, request: LLMRequest, *, provider: str, model: str, context_window: int | None) -> None:
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
            _image_refs(request)
            # Capacity belongs to the adapter's shared final-body accounting.
            # This gate retains exact protected data, never a second token quota.
        except LLMInputAdmissionError:
            raise
        except Exception:
            raise LLMInputAdmissionError("reference_model_budget_unavailable") from None

    async def validate(self, request: LLMRequest, *, provider: str, model: str, context_window: int | None) -> None:
        self._validate_input(request, provider=provider, model=model, context_window=context_window)

    async def validate_dispatch(self, request: LLMRequest, body: dict, *, provider: str, model: str, family: str) -> None:
        self._validate_input(request, provider=provider, model=model, context_window=request.input_admission_window)
        self.validate_outbound(request, body, family=family)

    def validate_outbound(self, request: LLMRequest, body: dict, *, family: str) -> None:
        """Verify final wire integrity after shared context/output accounting.

        Recognizes the existing chat/Responses/Anthropic body contracts only;
        unknown templated transports cannot claim this input support.
        """
        try:
            self._validate_input(request, provider="outbound", model=request.model, context_window=request.input_admission_window)
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
        except LLMInputAdmissionError:
            raise
        except Exception:
            raise LLMInputAdmissionError("reference_model_budget_unavailable") from None
