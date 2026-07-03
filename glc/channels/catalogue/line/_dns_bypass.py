"""Bypass ISP DNS/SNI hijacking for LINE API hostnames.

Some ISPs (e.g. Airtel via rpz.airtelspam.com) poison DNS and do TLS SNI
filtering for external API hostnames. This module provides:

  1. patch_line_dns()  – monkey-patches socket.getaddrinfo (partial fix; not
                         sufficient when the ISP also does SNI filtering).
  2. make_bypass_transport() – returns an httpx AsyncBaseTransport that
                         rewrites requests for LINE API hosts to use the
                         pre-resolved IP directly, so no SNI is sent in the
                         TLS ClientHello, bypassing SNI-based filtering.

Call make_bypass_transport() and pass the result to LiveLineClient(transport=…).
"""

from __future__ import annotations

import logging
import socket

import dns.resolver
import httpx

_LINE_API_HOSTS: frozenset[str] = frozenset(["api.line.me"])
_NAMESERVERS = ["8.8.8.8", "8.8.4.4"]

log = logging.getLogger("glc.line.dns_bypass")


# ---------------------------------------------------------------------------
# Low-level DNS query via a specific nameserver
# ---------------------------------------------------------------------------

def _resolve_via_google(hostname: str) -> str:
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = _NAMESERVERS
    resolver.timeout = 5.0
    resolver.lifetime = 5.0
    answers = resolver.resolve(hostname, "A")
    return str(answers[0])


# ---------------------------------------------------------------------------
# socket.getaddrinfo patch (kept for reference; transport approach is better)
# ---------------------------------------------------------------------------

_original_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, port, *args, **kwargs):
    if host in _LINE_API_HOSTS:
        try:
            ip = _resolve_via_google(host)
            log.debug("dns_bypass: %s -> %s (via 8.8.8.8)", host, ip)
            return _original_getaddrinfo(ip, port, *args, **kwargs)
        except Exception as exc:
            log.warning("dns_bypass: custom resolver failed for %s (%r), using system DNS", host, exc)
    return _original_getaddrinfo(host, port, *args, **kwargs)


def patch_line_dns() -> None:
    socket.getaddrinfo = _patched_getaddrinfo
    log.info("dns_bypass: socket patch active for %s", sorted(_LINE_API_HOSTS))


# ---------------------------------------------------------------------------
# httpx transport that bypasses SNI filtering
# ---------------------------------------------------------------------------

class _SNIBypassTransport(httpx.AsyncBaseTransport):
    """Rewrites LINE API requests to connect by IP so no SNI hostname is sent.

    When httpx connects to https://<hostname>/…, it sends SNI=<hostname> in
    the TLS ClientHello.  Airtel intercepts connections with SNI=api.line.me.
    Connecting to the raw IP skips SNI entirely (RFC 6066 forbids IP-literal
    SNI), so the TLS handshake goes through.  The HTTP Host header is left
    intact so LINE's servers route the request correctly.

    SSL verification is disabled because the server certificate is issued for
    the hostname, not the IP — but the traffic is still TLS-encrypted and the
    LINE Bearer token provides application-level authentication.
    """

    def __init__(self, ip_map: dict[str, str]) -> None:
        self._ip_map = ip_map
        self._inner = httpx.AsyncHTTPTransport(verify=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        ip = self._ip_map.get(host)
        if ip:
            new_url = request.url.copy_with(host=ip)
            # httpx.Request preserves the existing Host header when headers are
            # passed explicitly, so Host: api.line.me is kept even though the
            # URL now points to the raw IP.
            request = httpx.Request(
                method=request.method,
                url=new_url,
                headers=request.headers,
                stream=request.stream,
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def make_bypass_transport() -> httpx.AsyncBaseTransport | None:
    """Resolve LINE API hosts via 8.8.8.8 and return an SNI-bypass transport.

    Returns None if the custom resolver fails (caller falls back to default).
    The returned transport should be passed to httpx.AsyncClient(transport=…).
    """
    ip_map: dict[str, str] = {}
    for hostname in _LINE_API_HOSTS:
        try:
            ip = _resolve_via_google(hostname)
            ip_map[hostname] = ip
            log.info("dns_bypass: %s -> %s (SNI bypass active)", hostname, ip)
        except Exception as exc:
            log.warning("dns_bypass: could not resolve %s via 8.8.8.8 (%r); skipping bypass", hostname, exc)

    if not ip_map:
        return None
    return _SNIBypassTransport(ip_map)
