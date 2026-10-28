"""Explicit reference wire reader, before FastAPI's ordinary JSON decoding.

Legacy application/json requests keep their existing contract. The versioned
media type opts into bounded, duplicate-key-rejecting decoding of the full body.
Reading this contract is not deployment readiness or execution permission.
"""

from __future__ import annotations

from bowhead_core.chat_references import MAX_DOCUMENT_BYTES, MAX_JSON_DEPTH, decode_reference_json
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

REFERENCE_MEDIA_TYPE = "application/vnd.bowhead.chat-references.v1+json"
MAX_REFERENCE_REQUEST_BYTES = 4 * MAX_DOCUMENT_BYTES


def is_reference_media_type(request: Request) -> bool:
    return request.headers.get("content-type", "").partition(";")[0].strip().lower() == REFERENCE_MEDIA_TYPE


def _invalid_reference_input() -> HTTPException:
    return HTTPException(status_code=422, detail={"error": {"code": "reference_input_invalid"}})


class ChatReferenceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        if not self.path.endswith("/send") or "POST" not in self.methods:
            return original

        async def handler(request: Request):
            if not is_reference_media_type(request):
                return await original(request)
            buffer = bytearray()
            async for chunk in request.stream():
                if len(buffer) + len(chunk) > MAX_REFERENCE_REQUEST_BYTES:
                    raise HTTPException(status_code=413, detail={"error": {"code": "reference_request_too_large"}})
                buffer.extend(chunk)
            raw = bytes(buffer)
            try:
                decoded = decode_reference_json(raw, max_bytes=MAX_REFERENCE_REQUEST_BYTES, max_depth=MAX_JSON_DEPTH + 1)
                if not isinstance(decoded, dict) or decoded.get("submission_contract_version") != "chat-references.v1":
                    raise ValueError("reference wire requires its matching contract")
            except (ValueError, RecursionError) as exc:
                raise _invalid_reference_input() from exc
            # Preserve the bytes and strict decoded object for FastAPI and other
            # readers of this same Request; the stream has already been consumed.
            request._body = raw
            request._json = decoded
            try:
                return await original(request)
            except RequestValidationError as exc:
                raise _invalid_reference_input() from exc

        return handler
