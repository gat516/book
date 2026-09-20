"""Public HTTPS egress for configurable hosted providers (§15).

Resolve once and connect to a vetted numeric address. Preserve the original Host and
TLS SNI/verification name. Redirects and environment proxies are never followed.
"""
import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit
import httpx


def approved_endpoint(raw: str) -> str:
    u = urlsplit(raw)
    approved = {h.strip().lower() for h in os.getenv('PROVIDER_HEALTH_ALLOWED_HOSTS', '').split(',')}
    if (u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment
            or u.port not in (None, 443) or u.hostname.lower() not in approved):
        raise ValueError('custom endpoint must be approved public HTTPS')
    return raw.rstrip('/')


def public_address(raw: str) -> bool:
    ip = ipaddress.ip_address(raw)
    if getattr(ip, 'ipv4_mapped', None):
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request):
        if request.url.scheme != 'https' or request.url.port not in (None, 443):
            raise ValueError('HTTPS required')
        host = request.url.host
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = [row[4][0] for row in addresses]
        if not ips or not all(public_address(ip) for ip in ips):
            raise ValueError('non-public destination blocked')
        # httpcore's SNI extension preserves certificate validation against the hostname.
        pinned = httpx.Request(request.method, request.url.copy_with(host=ips[0]),
            headers=request.headers, stream=request.stream,
            extensions={**request.extensions, 'sni_hostname': host})
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()
