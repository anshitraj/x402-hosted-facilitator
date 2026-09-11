from __future__ import annotations

import json
import os
import socket
import time
import urllib.parse
import urllib.request
from ipaddress import ip_address
from typing import Any

_INSTALLED = False
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_CACHE: dict[str, tuple[float, list[str]]] = {}


def install_trycloudflare_doh_resolver() -> None:
    """Resolve quick Cloudflare tunnel hostnames over DoH for external smoke tests."""
    global _INSTALLED
    if _INSTALLED:
        return
    enabled = os.environ.get("OMNICLAW_EXAMPLE_TRYCLOUDFLARE_DOH", "").lower()
    if enabled not in {"1", "true", "yes"}:
        return

    # Test harness only. Do not set this env var in production seller services.
    socket.getaddrinfo = _getaddrinfo  # type: ignore[assignment]
    _INSTALLED = True


def _getaddrinfo(
    host: str | bytes | None,
    port: str | int | None,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[Any]:
    hostname = host.decode("ascii", errors="ignore") if isinstance(host, bytes) else host
    if not hostname or not hostname.endswith(".trycloudflare.com"):
        return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

    addresses = _resolve_a_records(hostname)
    if not addresses:
        return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

    results: list[Any] = []
    families = [family] if family in {socket.AF_INET, socket.AF_UNSPEC, 0} else []
    if not families:
        return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

    socket_types = [type] if type else [socket.SOCK_STREAM]
    protocols = [proto] if proto else [socket.IPPROTO_TCP]
    for address in addresses:
        for socket_type in socket_types:
            for protocol in protocols:
                results.append(
                    (socket.AF_INET, socket_type, protocol, "", (address, int(port or 0)))
                )
    return results


def _resolve_a_records(hostname: str) -> list[str]:
    now = time.monotonic()
    cached = _CACHE.get(hostname)
    if cached and cached[0] > now:
        return cached[1]

    query = urllib.parse.urlencode({"name": hostname, "type": "A"})
    url = f"https://cloudflare-dns.com/dns-query?{query}"
    request = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))

    addresses = []
    for answer in payload.get("Answer", []):
        if (
            isinstance(answer, dict)
            and answer.get("type") == 1
            and isinstance(answer.get("data"), str)
        ):
            ip_address(answer["data"])
            addresses.append(answer["data"])
    ttl_values = [
        int(answer.get("TTL", 60))
        for answer in payload.get("Answer", [])
        if isinstance(answer, dict) and answer.get("type") == 1
    ]
    ttl = max(30, min(ttl_values or [60]))
    _CACHE[hostname] = (now + ttl, addresses)
    return addresses
