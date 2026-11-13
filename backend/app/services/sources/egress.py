"""Outbound network controls for source providers: allowlist, URL and IP safety, DNS pinning.

Every outbound request passes two gates:

1. `EgressGuard.check_url` (before each request and each redirect hop): HTTPS only, no
   credentials/IP literals/confusable hostnames, host present in BOTH the deployment
   allowlist and the connector's reviewed host list, and every resolved address public.
2. `PinnedNetworkBackend.connect_tcp` (at connection time, authoritative): resolves the
   host once, rejects the connection if ANY address is non-public, and connects to that
   validated address. TLS still verifies the certificate against the hostname, so the
   check and the connection use the same DNS answer and rebinding cannot swap in an
   internal address between them.

With an egress proxy configured (`SOURCE_EGRESS_PROXY_URL`), gate 2 is replaced by
`ProxyTunnelTransport`: it re-checks the host, tunnels with `CONNECT host:443`, and its
network backend can connect to nothing but the proxy. The proxy resolves DNS and refuses
non-public destinations (the worker has no route or external DNS of its own), so the guard
skips its own resolution in that mode; every other check in gate 1 still applies.
"""

import ipaddress
import socket
import ssl
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from app.services.sources.errors import (
    RetryableSourceError,
    SourceErrorCode,
    SourcePolicyError,
)
from app.services.sources.policy import host_matches, is_blocked_host, normalize_host

Resolver = Callable[[str, int], Sequence[str]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_URL_LENGTH = 2048
DNS_TIMEOUT_SECONDS = 5.0
ALLOWED_PORTS = frozenset({443})
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def path_within(path: str, prefix: str) -> bool:
    """Exact path or a sub-path: "/board/acme" permits "/board/acme/x" but not "/board/acmeX"."""
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


class EgressDenied(SourcePolicyError):
    """A request target is not permitted. Never retried; never bypassed."""

    def __init__(self, reason: str, host: str | None = None) -> None:
        self.reason = reason
        self.host = (host or "")[:255]
        super().__init__(SourceErrorCode.EGRESS_DENIED, f"Egress denied ({reason})")


def is_public_address(address: IPAddress) -> bool:
    """True only for globally routable unicast addresses (fail closed on everything else)."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_address(address.ipv4_mapped)
        if any(address in network for network in _NAT64):
            return is_public_address(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        if address.sixtofour is not None:
            return is_public_address(address.sixtofour)
        if address.teredo is not None:
            return False
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_unspecified
    )


def system_resolver(host: str, port: int) -> list[str]:
    def lookup() -> list[str]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return sorted({str(info[4][0]) for info in infos})

    # Not a `with` block: its exit waits for the worker, which would defeat the timeout when
    # getaddrinfo hangs. A timed-out lookup thread is abandoned instead.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source-dns")
    future = executor.submit(lookup)
    try:
        return future.result(timeout=DNS_TIMEOUT_SECONDS)
    except FutureTimeout as exc:
        raise RetryableSourceError(
            SourceErrorCode.PROVIDER_TIMEOUT, "DNS resolution timed out"
        ) from exc
    except OSError as exc:
        raise RetryableSourceError(
            SourceErrorCode.PROVIDER_UNAVAILABLE, "DNS resolution failed"
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True)
class EgressPolicy:
    """Immutable per-connector egress policy; providers can read but never change it."""

    deployment_allowlist: tuple[str, ...]
    connector_hosts: tuple[str, ...]
    path_prefixes: tuple[str, ...] = ()

    def host_allowed(self, host: str) -> bool:
        return (
            bool(self.deployment_allowlist)
            and bool(self.connector_hosts)
            and host_matches(host, self.deployment_allowlist)
            and host_matches(host, self.connector_hosts)
        )


class EgressGuard:
    def __init__(
        self,
        policy: EgressPolicy,
        resolver: Resolver = system_resolver,
        *,
        resolve_dns: bool = True,
    ) -> None:
        self.policy = policy
        self._resolver = resolver
        # False only behind the egress proxy, which resolves and checks addresses itself.
        self.resolve_dns = resolve_dns

    def check_url(self, url: str) -> str:
        """Validate a request URL and return its canonical host. Raises EgressDenied."""
        if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
            raise EgressDenied("url_invalid")
        if any(ch.isspace() or ord(ch) < 0x20 or ch == "\\" for ch in url):
            raise EgressDenied("url_invalid")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise EgressDenied("url_invalid") from exc
        if parts.scheme != "https":
            raise EgressDenied("scheme_not_https")
        if "@" in parts.netloc or parts.username or parts.password:
            raise EgressDenied("userinfo_not_allowed")
        if "%" in parts.netloc:
            raise EgressDenied("encoded_host_not_allowed")
        if port is not None and port not in ALLOWED_PORTS:
            raise EgressDenied("port_not_allowed", parts.hostname)
        raw_host = parts.hostname or ""
        try:
            ipaddress.ip_address(raw_host.strip("[]"))
            raise EgressDenied("ip_literal_not_allowed", raw_host)
        except ValueError:
            pass
        host = normalize_host(raw_host)
        if host is None:
            raise EgressDenied("hostname_invalid", raw_host)
        if is_blocked_host(host):
            raise EgressDenied("domain_blocked", host)
        if not self.policy.host_allowed(host):
            raise EgressDenied("host_not_allowlisted", host)
        if self.policy.path_prefixes and not any(
            path_within(parts.path or "/", prefix) for prefix in self.policy.path_prefixes
        ):
            raise EgressDenied("path_not_allowlisted", host)
        if self.resolve_dns:
            self.resolve_public(host, port or 443)
        return host

    def resolve_public(self, host: str, port: int) -> list[IPAddress]:
        answers = list(self._resolver(host, port))
        if not answers:
            raise EgressDenied("dns_no_answer", host)
        addresses: list[IPAddress] = []
        for answer in answers:
            try:
                address = ipaddress.ip_address(str(answer).split("%", 1)[0])
            except ValueError as exc:
                raise EgressDenied("dns_answer_invalid", host) from exc
            # One unsafe answer rejects the host: never "pick the good one".
            if not is_public_address(address):
                raise EgressDenied("resolved_to_non_public_address", host)
            addresses.append(address)
        return addresses


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connects only to addresses validated from a single resolution, at connect time."""

    def __init__(
        self,
        guard: EgressGuard,
        inner: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._guard = guard
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        canonical = normalize_host(host)
        if canonical is None or not self._guard.policy.host_allowed(canonical):
            raise EgressDenied("host_not_allowlisted", host)
        if port not in ALLOWED_PORTS:
            raise EgressDenied("port_not_allowed", host)
        addresses = self._guard.resolve_public(canonical, port)
        return self._inner.connect_tcp(
            str(addresses[0]),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        raise EgressDenied("unix_socket_not_allowed")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class PinnedTransport(httpx.HTTPTransport):
    """httpx transport whose connection pool resolves and connects via the pinned backend.

    httpx has no public hook for the network backend, so the pool is replaced after
    construction; `tests/test_phase5a_network.py` asserts the wiring so an httpx upgrade
    that changes it fails loudly instead of silently dropping DNS pinning.
    """

    def __init__(
        self,
        guard: EgressGuard,
        *,
        inner_backend: httpcore.NetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        context = _secure_context(ssl_context)
        super().__init__(verify=context, trust_env=False, retries=0)
        self._pool = httpcore.ConnectionPool(
            ssl_context=context,
            max_connections=4,
            retries=0,
            network_backend=PinnedNetworkBackend(guard, inner_backend),
        )


def _secure_context(ssl_context: ssl.SSLContext | None) -> ssl.SSLContext:
    context = ssl_context or ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class ProxyTunnelBackend(httpcore.NetworkBackend):
    """Network backend that can connect to the configured egress proxy and nothing else."""

    def __init__(
        self, proxy_host: str, proxy_port: int, inner: httpcore.NetworkBackend | None = None
    ) -> None:
        self._host = proxy_host.lower().rstrip(".")
        self._port = proxy_port
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        if (host or "").lower().rstrip(".") != self._host or port != self._port:
            raise EgressDenied("proxy_only", host)
        return self._inner.connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        raise EgressDenied("unix_socket_not_allowed")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class ProxyTunnelTransport(httpx.HTTPTransport):
    """Tunnels every request through the egress proxy with CONNECT, then TLS end to end.

    The certificate and SNI are verified against the target hostname, so the proxy can
    neither read nor alter the traffic. The pool is replaced after construction, as in
    `PinnedTransport`; `tests/test_live_access_deployment.py` asserts the wiring.
    """

    def __init__(
        self,
        guard: EgressGuard,
        proxy: tuple[str, int],
        *,
        inner_backend: httpcore.NetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        host, port = proxy
        context = _secure_context(ssl_context)
        super().__init__(verify=context, trust_env=False, retries=0)
        self._guard = guard
        self._pool = httpcore.HTTPProxy(
            proxy_url=f"http://{host}:{port}",
            ssl_context=context,
            max_connections=4,
            retries=0,
            network_backend=ProxyTunnelBackend(host, port, inner_backend),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Defense in depth: the guard has already checked this URL. Only an HTTPS request
        # to an allowlisted host on 443 is ever tunneled. Plain HTTP would be forwarded by
        # the proxy in clear text, so it is refused here.
        host = normalize_host(request.url.host)
        if request.url.scheme != "https":
            raise EgressDenied("scheme_not_https", host)
        if (request.url.port or 443) not in ALLOWED_PORTS:
            raise EgressDenied("port_not_allowed", host)
        if host is None or not self._guard.policy.host_allowed(host):
            raise EgressDenied("host_not_allowlisted", request.url.host)
        return super().handle_request(request)


def build_transport(guard: EgressGuard, proxy: tuple[str, int] | None) -> httpx.BaseTransport:
    """The production transport: proxy tunnel when a proxy is configured, else DNS pinning."""
    if proxy is not None:
        return ProxyTunnelTransport(guard, proxy)
    return PinnedTransport(guard)
