from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import mcp.types
import pytest
from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.chat_reference_storage import ContentDescriptor
from bowhead_core.chat_references import McpPromptResourceRef

from bowhead_core import mcp_prompt_expansion as expansion

_NODE_ID = "12345678-1234-1234-1234-123456789abc"


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _resource() -> McpPromptResourceRef:
    return McpPromptResourceRef(
        kind="mcp_prompt",
        source_id="ots_prompt",
        local_contract_revision=_hash("a"),
        connection_binding_id="ots_prompt",
        connection_subject_fingerprint=_hash("b"),
        prompt_name="profitability",
        prompt_contract_revision=_hash("c"),
    )


def _result(text: str = "Use DuPont analysis") -> mcp.types.GetPromptResult:
    return mcp.types.GetPromptResult(
        description="Profitability template",
        messages=[
            mcp.types.PromptMessage(
                role="user",
                content=mcp.types.TextContent(type="text", text=text),
            ),
            mcp.types.PromptMessage(
                role="assistant",
                content=mcp.types.TextContent(type="text", text="I will compare ROE drivers."),
            ),
        ],
        _meta={"server_private": "discarded"},
    )


def test_prompt_expansion_codec_keeps_exact_text_roles_and_arguments():
    resource = _resource()
    prepared = expansion.prepare_mcp_prompt_expansion(
        resource=resource,
        parameters={"period": "FY2025"},
        result=_result(),
    )
    descriptor = prepared.descriptor(
        node_id=_NODE_ID,
        content_key=f"prompt:{_NODE_ID}",
    )

    assert descriptor.content_kind == "prompt_result"
    assert descriptor.resource == resource
    document = prepared.document()
    assert document.resource == resource
    assert document.parameters == {"period": "FY2025"}
    assert [(message.role, message.text) for message in document.messages] == [
        ("user", "Use DuPont analysis"),
        ("assistant", "I will compare ROE drivers."),
    ]
    assert b"server_private" not in prepared.content
    assert (
        expansion.restore_mcp_prompt_expansion(
            prepared.content,
            descriptor=descriptor,
        )
        == prepared
    )


def test_prompt_expansion_rejects_non_text_content():
    result = mcp.types.GetPromptResult(
        messages=[
            mcp.types.PromptMessage(
                role="user",
                content=mcp.types.ImageContent(
                    type="image",
                    data="aGVsbG8=",
                    mimeType="image/png",
                ),
            )
        ]
    )

    with pytest.raises(
        expansion.McpPromptExpansionError,
        match="reference_mcp_prompt_content_unsupported",
    ):
        expansion.prepare_mcp_prompt_expansion(
            resource=_resource(),
            parameters={},
            result=result,
        )


def test_prompt_expansion_rejects_result_over_256_kib():
    with pytest.raises(
        expansion.McpPromptExpansionError,
        match="reference_mcp_prompt_result_too_large",
    ):
        expansion.prepare_mcp_prompt_expansion(
            resource=_resource(),
            parameters={},
            result=_result("x" * expansion.MAX_MCP_PROMPT_RESULT_BYTES),
        )


def test_prompt_expansion_rejects_tampered_snapshot_descriptor():
    prepared = expansion.prepare_mcp_prompt_expansion(
        resource=_resource(),
        parameters={},
        result=_result(),
    )
    descriptor = prepared.descriptor(
        node_id=_NODE_ID,
        content_key=f"prompt:{_NODE_ID}",
    ).model_copy(update={"materialization_hash": _hash("d")})

    with pytest.raises(
        expansion.McpPromptExpansionError,
        match="reference_mcp_prompt_snapshot_invalid",
    ):
        expansion.restore_mcp_prompt_expansion(
            prepared.content,
            descriptor=descriptor,
        )


@pytest.mark.parametrize(
    "parameters",
    [
        {"": "value"},
        {f"arg-{index}": "value" for index in range(129)},
        {"arg": "x" * (expansion.MAX_MCP_PROMPT_ARGUMENT_VALUE_BYTES + 1)},
    ],
)
def test_prompt_expansion_restore_rejects_parameters_producer_cannot_create(parameters):
    resource = _resource()
    document = {
        "schema_version": 1,
        "result_contract": "mcp-prompt-result.v1",
        "resource": resource.model_dump(mode="json"),
        "parameters": parameters,
        "description": "",
        "messages": [{"role": "user", "text": "Use DuPont analysis"}],
    }
    content = expansion._json(document)
    descriptor = ContentDescriptor(
        schema_version=1,
        content_key=f"prompt:{_NODE_ID}",
        node_id=_NODE_ID,
        resource=resource.model_dump(mode="python"),
        content_kind="prompt_result",
        materialization_hash=canonical_fingerprint(
            {
                "contract": "mcp-prompt-result.v1",
                "resource": resource.model_dump(mode="json"),
                "parameters": parameters,
            }
        ),
        content_hash=expansion._hash(content),
        byte_count=len(content),
    )

    with pytest.raises(
        expansion.McpPromptExpansionError,
        match="reference_mcp_prompt_snapshot_invalid",
    ):
        expansion.restore_mcp_prompt_expansion(content, descriptor=descriptor)


class _Session:
    def __init__(self, *, delay: float = 0):
        self.delay = delay
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get_server_capabilities(self):
        return SimpleNamespace(prompts=object())

    async def get_prompt(self, name: str, arguments: dict[str, str]):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        return _result()


def _connect(session: _Session, captured: dict):
    @asynccontextmanager
    async def connect(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        yield session

    return connect


@pytest.mark.asyncio
async def test_fetch_prompt_expansion_calls_exact_prompt_under_wire_limit(monkeypatch):
    session = _Session()
    captured: dict = {}
    monkeypatch.setattr(
        expansion,
        "connect_remote_mcp",
        _connect(session, captured),
    )

    prepared = await expansion.fetch_mcp_prompt_expansion(
        "https://example.com/mcp",
        resource=_resource(),
        parameters={"period": "FY2025"},
        headers={"Authorization": "Bearer secret"},
    )

    assert session.calls == [("profitability", {"period": "FY2025"})]
    assert captured["kwargs"]["response_bytes_limit"] == expansion.MAX_MCP_PROMPT_WIRE_RESPONSE_BYTES
    assert prepared.document().parameters == {"period": "FY2025"}


@pytest.mark.asyncio
async def test_fetch_prompt_expansion_has_one_absolute_deadline(monkeypatch):
    session = _Session(delay=0.05)
    monkeypatch.setattr(
        expansion,
        "connect_remote_mcp",
        _connect(session, {}),
    )

    with pytest.raises(
        expansion.McpPromptExpansionError,
        match="reference_mcp_prompt_expansion_timeout",
    ):
        await expansion.fetch_mcp_prompt_expansion(
            "https://example.com/mcp",
            resource=_resource(),
            parameters={},
            timeout=0.01,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters",
    [
        {"arg": "\x00" * expansion.MAX_MCP_PROMPT_ARGUMENT_VALUE_BYTES},
        {f"a{index}": "x" * 65_500 for index in range(4)},
    ],
    ids=("json-escape-expansion", "minimum-candidate-envelope"),
)
async def test_fetch_prompt_expansion_rejects_impossible_parameters_before_remote_call(
    monkeypatch,
    parameters,
):
    session = _Session()
    monkeypatch.setattr(expansion, "connect_remote_mcp", _connect(session, {}))

    with pytest.raises(
        expansion.McpPromptExpansionError,
        match="reference_mcp_prompt_parameters_too_large",
    ):
        await expansion.fetch_mcp_prompt_expansion(
            "https://example.com/mcp",
            resource=_resource(),
            parameters=parameters,
        )

    assert session.calls == []


@pytest.mark.asyncio
async def test_mcp_sdk_transport_logs_do_not_expose_prompt_payloads(monkeypatch, caplog):
    class LoggingSession(_Session):
        async def get_prompt(self, name: str, arguments: dict[str, str]):
            logger = logging.getLogger("mcp.client.streamable_http")
            logger.debug("Sending client message: canary-argument")
            logger.debug("SSE message: canary-result")
            logger.debug("Connecting to StreamableHTTP endpoint: https://example.com?token=canary-url")
            try:
                raise ValueError("malformed canary-exception")
            except ValueError:
                logger.exception("Error parsing JSON response")
            logging.getLogger("bowhead_core.mcp_transport_test").debug("transport-ready")
            return await super().get_prompt(name, arguments)

    session = LoggingSession()
    monkeypatch.setattr(expansion, "connect_remote_mcp", _connect(session, {}))
    caplog.set_level(logging.DEBUG)

    await expansion.fetch_mcp_prompt_expansion(
        "https://example.com/mcp",
        resource=_resource(),
        parameters={"period": "FY2025"},
    )

    assert "canary-argument" not in caplog.text
    assert "canary-result" not in caplog.text
    assert "canary-url" not in caplog.text
    assert "canary-exception" not in caplog.text
    assert "transport-ready" in caplog.text
