"""Feishu long-connection event ingress using the official SDK."""

from __future__ import annotations

import asyncio
import base64
import http
import json
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING, Any

from bowhead_gateway.feishu.bridge import FeishuTurnBridge, parse_message_event
from bowhead_gateway.product_identity import product_name

try:
    import lark_oapi as lark
    import lark_oapi.ws as lark_ws
    import lark_oapi.ws.client as lark_ws_client
    from lark_oapi.core.const import UTF_8
    from lark_oapi.core.json import JSON
    from lark_oapi.ws.client import _get_by_key
    from lark_oapi.ws.const import (
        HEADER_BIZ_RT,
        HEADER_MESSAGE_ID,
        HEADER_SEQ,
        HEADER_SUM,
        HEADER_TRACE_ID,
        HEADER_TYPE,
    )
    from lark_oapi.ws.enum import MessageType
    from lark_oapi.ws.model import Response
except Exception:  # pragma: no cover - import failure is reported on worker start
    lark = None  # type: ignore[assignment]
    lark_ws = None  # type: ignore[assignment]
    lark_ws_client = None  # type: ignore[assignment]
    UTF_8 = "utf-8"  # type: ignore[assignment]
    JSON = None  # type: ignore[assignment]
    MessageType = None  # type: ignore[assignment]
    Response = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from bowhead_gateway.feishu.config import FeishuAppConfigResolver
    from bowhead_gateway.feishu.store import FeishuEventStore

logger = logging.getLogger(__name__)


def _accepting_dispatch(bridge: Any) -> bool:
    app = getattr(bridge, "app", None)
    state = getattr(app, "state", None)
    if state is None:
        return True
    return bool(getattr(state, "feishu_accepting_dispatch", True))


class FeishuLongConnectionWorker:
    """Run the Feishu SDK WebSocket client and dispatch events into Bowhead."""

    def __init__(
        self,
        *,
        bridge: FeishuTurnBridge,
        store: FeishuEventStore,
        app_config_resolver: FeishuAppConfigResolver,
        base_url: str = "https://open.feishu.cn",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.bridge = bridge
        self.store = store
        self.app_config_resolver = app_config_resolver
        self.base_url = base_url
        self.loop = loop
        self._thread: threading.Thread | None = None
        self._running = False
        self._start_error: str | None = None

    @property
    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "start_error": self._start_error,
        }

    def start(self) -> None:
        """Start the blocking SDK client in a daemon thread."""

        if self._thread is not None:
            return
        self.loop = self.loop or asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._run_sdk_client,
            name="feishu-long-connection",
            daemon=True,
        )
        self._thread.start()

    def _run_sdk_client(self) -> None:
        config = self.app_config_resolver.resolve("")
        if not config.enabled:
            self._start_error = "Feishu integration is disabled"
            logger.info("feishu.long_connection_disabled")
            return
        if not (config.app_id and config.app_secret):
            self._start_error = "Feishu app_id/app_secret are required"
            logger.error("feishu.long_connection_missing_app_config")
            return
        if lark is None or lark_ws is None:  # pragma: no cover - dependency guard
            self._start_error = "lark-oapi package is not installed"
            logger.error("feishu.long_connection_sdk_missing")
            return

        sdk_loop, previous_sdk_loop = _install_sdk_thread_event_loop()
        event_handler = (
            lark.EventDispatcherHandler.builder("", "", lark.LogLevel.INFO)
            .register_p2_im_message_receive_v1(self._on_message_event)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )
        client = FeishuCardAwareWSClient(
            config.app_id,
            config.app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=event_handler,
            domain=self.base_url,
        )
        self._running = True
        try:
            logger.info("feishu.long_connection_starting base_url=%s", self.base_url)
            client.start()
        except Exception as exc:  # pragma: no cover - live websocket behavior
            self._start_error = str(exc)
            logger.exception("feishu.long_connection_failed err=%s", exc)
        finally:
            self._running = False
            _restore_sdk_event_loop(sdk_loop, previous_sdk_loop)

    def _on_message_event(self, event: Any) -> None:
        self._submit(self.handle_message_payload(_event_to_dict(event)))

    def _on_card_action(self, event: Any) -> dict[str, Any]:
        return self._run_card_action_sync(_event_to_dict(event))

    def _submit(self, coro: Any) -> None:
        if not _accepting_dispatch(self.bridge):
            logger.info("feishu.long_connection_dispatch_blocked reason=shutdown")
            _close_coroutine(coro)
            return
        if self.loop is None:
            logger.error("feishu.long_connection_no_loop")
            _close_coroutine(coro)
            return
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(_log_future_error)

    def _run_card_action_sync(self, body: dict[str, Any]) -> dict[str, Any]:
        coro = self.handle_card_action_payload(body)
        if not _accepting_dispatch(self.bridge):
            logger.info("feishu.long_connection_card_action_blocked reason=shutdown")
            _close_coroutine(coro)
            return _toast(f"{product_name()} 正在关闭连接，请稍后再试。", level="warning")
        if self.loop is None:
            logger.error("feishu.long_connection_no_loop")
            _close_coroutine(coro)
            return _toast(f"{product_name()} 暂时无法处理卡片操作。", level="warning")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self.loop:
            task = self.loop.create_task(coro)
            task.add_done_callback(_log_future_error)
            return _toast("请求已收到。", level="success")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            result = future.result(timeout=2.5)
        except FutureTimeoutError:
            logger.warning("feishu.long_connection_card_action_timeout")
            return _toast(f"请求已收到, {product_name()} 正在处理。", level="success")
        except Exception as exc:
            logger.exception("feishu.long_connection_card_action_failed err=%s", exc)
            return _toast(f"操作失败, 请在 {product_name()} Web 查看状态。", level="warning")
        return result if isinstance(result, dict) else _toast("请求已收到。", level="success")

    async def handle_message_payload(self, body: dict[str, Any]) -> None:
        inbound = parse_message_event(
            body,
            bot_open_id=self.bridge.config.bot_open_id,
        )
        if inbound is None:
            logger.info("feishu.long_connection_ignored reason=not_message_event")
            return
        claimed = await self.store.claim_message(
            event_id=inbound.event_id,
            message_id=inbound.message_id,
        )
        if not claimed:
            logger.info("feishu.long_connection_duplicate message_id=%s", inbound.message_id)
            return
        await self.store.mark_message_status(message_id=inbound.message_id, status="accepted")
        try:
            result = await self.bridge.handle_message_event(body)
            await self.store.mark_message_status(
                message_id=inbound.message_id,
                status=str(result.get("status") or "done"),
            )
        except Exception as exc:
            logger.exception(
                "feishu.long_connection_message_failed event_id=%s message_id=%s tenant_key=%s chat_id=%s open_id=%s err=%s",
                inbound.event_id,
                inbound.message_id,
                inbound.tenant_key,
                inbound.chat_id,
                inbound.sender_open_id,
                exc,
            )
            await self.store.mark_message_status(message_id=inbound.message_id, status="failed")

    async def handle_card_action_payload(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.bridge.handle_card_callback(body)
        except Exception as exc:
            logger.exception("feishu.long_connection_card_action_failed err=%s", exc)
            return _toast(f"操作失败, 请在 {product_name()} Web 查看状态。", level="warning")


class FeishuCardAwareWSClient(lark_ws.Client if lark_ws else object):  # type: ignore[misc]
    """WS client that dispatches Feishu card callback frames.

    The bundled Python SDK dispatches `event` frames but returns immediately for
    `card` frames. Live card button clicks can arrive as `card`, so route both
    through the EventDispatcherHandler and ACK within the WS protocol.
    """

    async def _handle_data_frame(self, frame: Any) -> None:
        headers = frame.headers
        msg_id = _get_by_key(headers, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(headers, HEADER_TRACE_ID)
        sum_ = _get_by_key(headers, HEADER_SUM)
        seq = _get_by_key(headers, HEADER_SEQ)
        type_ = _get_by_key(headers, HEADER_TYPE)

        payload = frame.payload
        if int(sum_) > 1:
            payload = self._combine(msg_id, int(sum_), int(seq), payload)
            if payload is None:
                return

        message_type = MessageType(type_)
        logger.debug(
            self._fmt_log(
                "receive message, message_type: {}, message_id: {}, trace_id: {}, payload: {}",
                message_type.value,
                msg_id,
                trace_id,
                payload.decode(UTF_8),
            ),
        )

        resp = Response(code=http.HTTPStatus.OK)
        try:
            if message_type not in {MessageType.EVENT, MessageType.CARD}:
                return
            if message_type == MessageType.EVENT and (_payload_event_type(payload) != "im.message.receive_v1"):
                logger.debug(
                    "feishu.long_connection_ignored_event type=%s message_id=%s trace_id=%s",
                    _payload_event_type(payload),
                    msg_id,
                    trace_id,
                )
                frame.payload = JSON.marshal(resp).encode(UTF_8)
                await self._write_message(frame.SerializeToString())
                return
            start = round(time.time() * 1000)
            result = self._event_handler._do_without_validation(payload)
            end = round(time.time() * 1000)
            header = headers.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as exc:
            logger.error(
                self._fmt_log(
                    "handle message failed, message_type: {}, message_id: {}, trace_id: {}, err: {}",
                    message_type.value,
                    msg_id,
                    trace_id,
                    exc,
                ),
            )
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())


def _event_to_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    try:
        if lark is not None and (raw := lark.JSON.marshal(event)):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        logger.debug("feishu.event_marshal_failed", exc_info=True)
    payload: dict[str, Any] = {}
    for key in ("header", "event", "schema"):
        value = getattr(event, key, None)
        if value is not None:
            payload[key] = value
    return payload


def _payload_event_type(payload: bytes) -> str:
    try:
        parsed = json.loads(payload.decode(UTF_8))
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""
    header = parsed.get("header")
    if isinstance(header, dict):
        return str(header.get("event_type") or "")
    return str(parsed.get("type") or "")


def _install_sdk_thread_event_loop() -> tuple[asyncio.AbstractEventLoop | None, Any]:
    """Point the Feishu SDK WS module at a loop owned by this worker thread."""

    if lark_ws_client is None:
        return None, None
    previous = getattr(lark_ws_client, "loop", None)
    sdk_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(sdk_loop)
    lark_ws_client.loop = sdk_loop
    return sdk_loop, previous


def _restore_sdk_event_loop(
    sdk_loop: asyncio.AbstractEventLoop | None,
    previous: Any,
) -> None:
    if lark_ws_client is not None and previous is not None:
        lark_ws_client.loop = previous
    if sdk_loop is not None and not sdk_loop.is_closed():
        try:
            sdk_loop.close()
        except Exception as exc:  # pragma: no cover - defensive cleanup
            logger.debug("feishu.long_connection_sdk_loop_close_failed err=%s", exc)


def _log_future_error(future: Any) -> None:
    try:
        future.result()
    except Exception as exc:
        logger.exception("feishu.long_connection_task_failed err=%s", exc)


def _toast(content: str, *, level: str = "info") -> dict[str, Any]:
    return {"toast": {"type": level, "content": content}}


def _close_coroutine(coro: Any) -> None:
    close = getattr(coro, "close", None)
    if callable(close):
        close()
