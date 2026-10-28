"""Deployment-mode outbound destination policy.

Private deployments intentionally permit their internal service topology.
SaaS deployments apply the public-HTTPS policy to every user-supplied URL, and
the public-SMTP policy (``validate_smtp_target``) to every user-supplied SMTP
server: the host must resolve only to public addresses and the port must be one
of the standard SMTP submission/relay ports. Callers pin the connection to the
returned IPs so a later DNS answer cannot redirect the socket (DNS rebinding).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2

__all__ = [
    "Resolver",
    "SMTP_PORTS",
    "SmtpPortBlocked",
    "SsrfBlocked",
    "SsrfGuardedAsyncTransport",
    "ValidatedTarget",
    "build_guarded_async_client",
    "resolve_and_validate",
    "validate_smtp_target",
    "validate_url",
]

Resolver = Callable[[str], Sequence[str]]


class SsrfBlocked(ValueError):  # noqa: N818
    """Raised when a SaaS outbound URL violates the public URL policy."""


class SmtpPortBlocked(SsrfBlocked):
    """Raised when a SaaS SMTP target uses a port outside ``SMTP_PORTS``."""


# Standard SMTP relay / submission ports. SaaS SMTP targets must use one of them;
# the generic URL policy keeps rejecting these ports for every other protocol.
SMTP_PORTS: frozenset[int] = frozenset({25, 465, 587, 2525})


@dataclass(frozen=True)
class ValidatedTarget:
    scheme: str
    host: str
    port: int
    ips: tuple[str, ...]

    @property
    def ip(self) -> str:
        return self.ips[0] if self.ips else self.host


def validate_url(
    url: str,
    *,
    resolver: Resolver | None = None,
) -> ValidatedTarget:
    """Validate only in SaaS mode; local mode permits all HTTP destinations.

    Deliberately takes no per-call policy overrides: the outbound policy is decided
    solely by ``BOWHEAD_DEPLOYMENT_MODE``. ``allow_loopback`` / ``allow_http`` /
    ``allow_private_network`` were removed in 2026-09 — they had been accepted and
    discarded since 22014fa9e, so every signature carrying them advertised an
    escape hatch that could not open anything. Per-module escape hatches are
    prohibited (CLAUDE.md, 出站地址与部署模式); adjust policy here, by mode.
    """
    parts = urlsplit(url)
    if not _is_saas():
        return _target(parts)
    if (parts.scheme or "").lower() != "https":
        raise SsrfBlocked("SaaS outbound URLs must use HTTPS")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise SsrfBlocked("SaaS outbound URLs must not contain credentials")
    host = parts.hostname
    if not host:
        raise SsrfBlocked("SaaS outbound URL has no host")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise SsrfBlocked("SaaS outbound URL has an invalid port") from exc
    if port in _BLOCKED_PORTS:
        raise SsrfBlocked("SaaS outbound URL uses a restricted port")
    ips = _resolve(host, resolver)
    if not ips or any(_ip_is_non_public(ip) for ip in ips):
        raise SsrfBlocked("SaaS outbound URL must resolve only to public IP addresses")
    return ValidatedTarget(
        scheme="https",
        host=host,
        port=port,
        ips=tuple(dict.fromkeys(ips)),
    )


def validate_smtp_target(
    host: str,
    port: int,
    *,
    resolver: Resolver | None = None,
) -> ValidatedTarget:
    """Validate a user-supplied SMTP server according to deployment mode.

    Local mode permits every destination (internal relays, loopback, any port).
    SaaS mode requires a port in ``SMTP_PORTS`` and a host that resolves only to
    public addresses. Returns the resolved IPs so the caller can connect to the
    exact addresses that were checked; call it again right before delivery.
    """
    host = (host or "").strip()
    if not _is_saas():
        return ValidatedTarget(scheme="smtp", host=host, port=port, ips=(host,))
    if not host:
        raise SsrfBlocked("SaaS SMTP target has no host")
    if port not in SMTP_PORTS:
        raise SmtpPortBlocked("SaaS SMTP target must use a standard SMTP port")
    try:
        ips = _resolve(host, resolver)
    except (OSError, UnicodeError) as exc:
        raise SsrfBlocked("SaaS SMTP host could not be resolved") from exc
    if not ips or any(_ip_is_non_public(ip) for ip in ips):
        raise SsrfBlocked("SaaS SMTP host must resolve only to public IP addresses")
    return ValidatedTarget(scheme="smtp", host=host, port=port, ips=tuple(dict.fromkeys(ips)))


def resolve_and_validate(url: str, **kwargs: Any) -> ValidatedTarget:
    """Validate the outbound destination according to deployment mode."""
    return validate_url(url, **kwargs)


class SsrfGuardedAsyncTransport(httpx2.AsyncBaseTransport):
    """Use normal networking locally and a pinned public target in SaaS mode."""

    def __init__(self, **kwargs: Any) -> None:
        self._resolver = kwargs.pop("resolver", None)
        self._transport = kwargs.pop("transport", None) or httpx2.AsyncHTTPTransport(**kwargs)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if not _is_saas():
            return await self._transport.handle_async_request(request)
        target = validate_url(str(request.url), resolver=self._resolver)
        headers = request.headers.copy()
        headers["Host"] = target.host if target.port == 443 else f"{target.host}:{target.port}"
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = target.host
        last_error: httpx2.ConnectError | httpx2.ConnectTimeout | None = None
        for ip in target.ips:
            guarded = httpx2.Request(
                request.method,
                _swap_host(str(request.url), ip),
                headers=headers,
                stream=request.stream,
                extensions=extensions,
            )
            try:
                return await self._transport.handle_async_request(guarded)
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def aclose(self) -> None:
        await self._transport.aclose()


def build_guarded_async_client(**kwargs: Any) -> httpx2.AsyncClient:
    """Build a client with the deployment-mode URL policy."""
    policy_kwargs = {key: kwargs.pop(key) for key in ("resolver",) if key in kwargs}
    transport = kwargs.pop("transport", None)
    return httpx2.AsyncClient(
        transport=SsrfGuardedAsyncTransport(transport=transport, **policy_kwargs),
        **kwargs,
    )


_BLOCKED_PORTS = frozenset(
    {22, 23, 25, 110, 135, 139, 143, 445, 465, 587, 993, 995, 1433, 1521, 2049, 2375, 2376, 3306, 3389, 5432, 5984, 6379, 9200, 9300, 11211, 27017}
)


def _is_saas() -> bool:
    return (os.environ.get("BOWHEAD_DEPLOYMENT_MODE") or "local").strip().lower() == "saas"


def _target(parts: Any) -> ValidatedTarget:
    scheme = (parts.scheme or "").lower()
    return ValidatedTarget(
        scheme=scheme,
        host=parts.hostname or "",
        port=parts.port or (443 if scheme == "https" else 80),
        ips=(parts.hostname or "",),
    )


def _resolve(host: str, resolver: Resolver | None) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        if resolver is not None:
            return list(resolver(host))
        return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, None)))


def _ip_is_non_public(ip_string: str) -> bool:
    ip = ipaddress.ip_address(ip_string)
    return not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def _swap_host(url: str, ip: str) -> str:
    parts = urlsplit(url)
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
