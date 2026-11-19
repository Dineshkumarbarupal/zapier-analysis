from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlsplit


class UnsafeURLError(ValueError):
    pass


@lru_cache(maxsize=4096)
def _host_is_public(hostname: str) -> bool:
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
        return literal.is_global
    except ValueError:
        pass
    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not records:
        return False
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


def validate_public_url(url: str) -> str:
    """Accept public HTTP(S) URLs to reduce SSRF and local-network access."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("URL is empty.")
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError("Only http:// and https:// URLs are supported.")
    if not parts.hostname:
        raise UnsafeURLError("URL does not contain a hostname.")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs containing embedded credentials are not allowed.")
    if not _host_is_public(parts.hostname.lower()):
        raise UnsafeURLError("The URL host is not publicly routable; private and local addresses are blocked.")
    return value


def is_safe_browser_request(url: str) -> bool:
    if url.startswith(("data:", "blob:", "about:")):
        return True
    try:
        validate_public_url(url)
        return True
    except UnsafeURLError:
        return False
