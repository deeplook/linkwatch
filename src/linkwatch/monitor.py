"""Connectivity probes, public-IP lookup, and polling loop."""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from .defaults import DEFAULT_INTERVAL, DEFAULT_LOG, DEFAULT_TIMEOUT
from .events import append_event


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str


ENDPOINTS = (
    Endpoint("Google", "https://connectivitycheck.gstatic.com/generate_204"),
    Endpoint("Apple", "https://captive.apple.com/hotspot-detect.html"),
    Endpoint("Microsoft", "https://www.msftconnecttest.com/connecttest.txt"),
)

PUBLIC_IP_ENDPOINTS = {
    4: ("https://api.ipify.org", "https://ipv4.icanhazip.com/"),
    6: ("https://api6.ipify.org", "https://ipv6.icanhazip.com/"),
}


def probe(endpoint_url: str, timeout: float) -> bool:
    """Return whether an HTTP connectivity check receives a successful response."""
    request = Request(endpoint_url, headers={"User-Agent": "linkwatch/0.1"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (OSError, URLError, TimeoutError, ValueError):
        return False


def find_reachable_endpoint(
    *,
    check: Callable[[str, float], bool] = probe,
    timeout: float = DEFAULT_TIMEOUT,
    endpoints: Sequence[Endpoint] = ENDPOINTS,
) -> Endpoint | None:
    """Try endpoints in order and return the first responder."""
    for endpoint in endpoints:
        if check(endpoint.url, timeout):
            return endpoint
    return None


def lookup_public_ip(version: int, timeout: float) -> str | None:
    """Return the externally observed address for one family, trying a fallback."""
    if version not in PUBLIC_IP_ENDPOINTS:
        raise ValueError("IP version must be 4 or 6")
    for endpoint_url in PUBLIC_IP_ENDPOINTS[version]:
        request = Request(endpoint_url, headers={"User-Agent": "linkwatch/0.1"}, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                text = response.read().decode("ascii").strip()
            address = ipaddress.ip_address(text)
        except (HTTPException, OSError, URLError, TimeoutError, UnicodeError, ValueError):
            continue
        if address.version == version and address.is_global:
            return str(address)
    return None


def monitor(
    interval: float = DEFAULT_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    log_path: Path = DEFAULT_LOG,
    *,
    check: Callable[[str, float], bool] = probe,
    sleep: Callable[[float], None] = time.sleep,
    lookup_ip: Callable[[int, float], str | None] = lookup_public_ip,
    endpoints: Sequence[Endpoint] = ENDPOINTS,
) -> None:
    """Poll until interrupted, logging only offline/online transitions."""
    if interval <= 0:
        raise ValueError("interval must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    previous_online: bool | None = None
    while True:
        endpoint = find_reachable_endpoint(check=check, timeout=timeout, endpoints=endpoints)
        online = endpoint is not None
        if previous_online is None and not online:
            append_event(log_path, "disconnected")
            print(f"disconnected: {datetime.now(timezone.utc).isoformat()} (logged to {log_path})", flush=True)
        elif previous_online is not None and online != previous_online:
            event = "connected" if online else "disconnected"
            endpoint_name = endpoint.name if endpoint is not None else ""
            public_ipv4 = (lookup_ip(4, timeout) or "") if online else ""
            public_ipv6 = (lookup_ip(6, timeout) or "") if online else ""
            append_event(
                log_path,
                event,
                endpoint_name,
                public_ipv4=public_ipv4,
                public_ipv6=public_ipv6,
            )
            detail = f" via {endpoint_name}" if endpoint_name else ""
            print(f"{event}{detail}: {datetime.now(timezone.utc).isoformat()} (logged to {log_path})", flush=True)
        previous_online = online
        sleep(interval)
