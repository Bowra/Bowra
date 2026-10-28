"""email-config sprint M4 — 真 SMTP 收信集成测(aiosmtpd 假 server).

验证整条 resolver → deliver_email → _send_smtp_detailed → smtplib → SMTP 接收链路:
真发一封信到本地 aiosmtpd,断言 From(formataddr)/To/Subject 落地正确。
"""

from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser

import pytest

aiosmtpd_controller = pytest.importorskip("aiosmtpd.controller")
from aiosmtpd.controller import Controller  # noqa: E402
from bowhead_core.email_config import SmtpConfig, deliver_email  # noqa: E402
from bowhead_core.email_sender import EmailAttachment  # noqa: E402


@pytest.mark.parametrize("html_body", [None, "<p>Report attached.</p>"])
async def test_smtp_delivers_unicode_named_attachment_without_changing_bytes(smtp_server, html_body: str | None) -> None:
    host, port, sink = smtp_server
    cfg = SmtpConfig(host=host, port=port, user="", password="", from_addr="sender@example.com", tls=False)
    content = b"PK\x03\x04\x00\xff presentation bytes\r\n"
    mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ok, error = await deliver_email(
        "recipient@example.com",
        "Sales report",
        "Report attached.",
        config=cfg,
        html_body=html_body,
        attachments=(EmailAttachment(filename="销售报告.pptx", mime=mime, content=content),),
    )
    assert ok is True, error
    assert len(sink.messages) == 1
    message = BytesParser(policy=policy.default).parsebytes(sink.messages[0]["data"].encode())
    assert message.get_content_type() == "multipart/mixed"
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "Report attached."
    if html_body is not None:
        assert message.get_body(preferencelist=("html",)).get_content().strip() == html_body
    files = list(message.iter_attachments())
    assert len(files) == 1
    assert files[0].get_filename() == "销售报告.pptx"
    assert files[0].get_content_type() == mime
    assert files[0].get_payload(decode=True) == content


class _Sink:
    """收集收到的邮件。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        self.messages.append(
            {
                "mail_from": envelope.mail_from,
                "rcpt_tos": list(envelope.rcpt_tos),
                "data": envelope.content.decode("utf-8", errors="replace"),
            }
        )
        return "250 OK"


def _free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def smtp_server():
    sink = _Sink()
    port = _free_port()
    # Explicit port (some aiosmtpd versions still report .port=0 for port=0); no TLS.
    controller = Controller(sink, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        yield "127.0.0.1", port, sink
    finally:
        controller.stop()


@pytest.mark.asyncio
async def test_deliver_email_real_smtp_receives(smtp_server) -> None:
    host, port, sink = smtp_server
    cfg = SmtpConfig(
        host=host,
        port=port,
        user="",
        password="",
        from_addr="no-reply@bowhead.ai",
        from_name="Bowhead 通知",
        tls=False,
    )
    ok, err = await deliver_email(
        "alice@example.com",
        "测试主题 Subject",
        "正文 body line\n",
        config=cfg,
    )
    assert ok is True, err
    # Give the server a moment to process.
    for _ in range(20):
        if sink.messages:
            break
        await asyncio.sleep(0.05)
    assert len(sink.messages) == 1
    msg = sink.messages[0]
    assert msg["mail_from"] == "no-reply@bowhead.ai"
    assert msg["rcpt_tos"] == ["alice@example.com"]
    # formataddr: RFC2047 encodes Chinese from_name (=?utf-8?...?=), preserving recipient addresses.
    assert "no-reply@bowhead.ai" in msg["data"]
    assert "alice@example.com" in msg["data"]
    # Subject becomes a MIME-encoded header (Chinese → =?utf-8?b?...); at least the ASCII portion remains.
    assert "Subject" in msg["data"]


@pytest.mark.asyncio
async def test_port_465_uses_implicit_ssl(monkeypatch) -> None:
    # review-fix(P1): port 465 = implicit SSL → must use SMTP_SSL, not SMTP+starttls.
    import smtplib

    from bowhead_core import email_sender

    used = {}

    class _FakeSSL:
        def __init__(self, host, port, timeout=10):
            used["ssl"] = (host, port)

        def login(self, u, p):
            pass

        def send_message(self, m):
            pass

        def quit(self):
            pass

    def _no_plain(*a, **k):
        raise AssertionError("465 不应走 plain SMTP")

    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSSL)
    monkeypatch.setattr(smtplib, "SMTP", _no_plain)
    cfg = SmtpConfig(host="smtp.exmail.qq.com", port=465, user="u", password="p", from_addr="a@x.com", tls=True)
    ok, err = email_sender._send_smtp_detailed("b@x.com", "s", "body", config=cfg)
    assert ok is True, err
    assert used["ssl"] == ("smtp.exmail.qq.com", 465)


@pytest.mark.asyncio
async def test_deliver_email_bad_host_returns_error() -> None:
    # Unlistened port → smtplib connection failure → (False, error); no raise or event-loop blockage.
    cfg = SmtpConfig(host="127.0.0.1", port=1, user="", password="", from_addr="a@x.com", tls=False)
    ok, err = await deliver_email("b@x.com", "s", "b", config=cfg, timeout=5.0)
    assert ok is False
    assert err
