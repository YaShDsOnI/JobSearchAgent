"""Verify network-layer egress enforcement from inside the internal network.

Run it in a container attached ONLY to the internal backend network (standard library only):

    python verify.py --proxy egress-proxy:3128 --allowed-host api.ashbyhq.com \
        --denied-host www.example.com --private-host localtest.me

Checks:
  1. no direct route out (TCP to public IPs fails) and no external DNS;
  2. the proxy refuses: non-allowlisted hosts, port 80, IP literals, plain HTTP methods,
     and an allowlisted host that resolves to a non-public address (--private-host must
     be in the proxy allowlist for this check);
  3. the proxy tunnels CONNECT to the allowed host, and TLS to it verifies end to end.
Optional --wildcard-apex: a host whose subdomains, but not the apex, are allowlisted.

Exit code 0 when every check passes, 1 otherwise. Sends only a TLS handshake and, with
--http-path, one GET to the allowed host.
"""

import argparse
import socket
import ssl
import sys

TIMEOUT = 10.0
DIRECT_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443))


def proxy_request(proxy: tuple[str, int], line: str) -> tuple[int, socket.socket]:
    sock = socket.create_connection(proxy, timeout=TIMEOUT)
    sock.sendall(line.encode("ascii") + b"\r\n\r\n")
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    status_line = data.split(b"\r\n", 1)[0].decode("latin-1")
    parts = status_line.split()
    return (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0), sock


def connect_status(proxy: tuple[str, int], target: str) -> int:
    status, sock = proxy_request(proxy, f"CONNECT {target} HTTP/1.1\r\nHost: {target}")
    sock.close()
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", required=True, help="host:port of the egress proxy")
    parser.add_argument("--allowed-host", required=True)
    parser.add_argument("--denied-host", required=True)
    parser.add_argument("--private-host", help="allowlisted host resolving to 127.0.0.1")
    parser.add_argument("--wildcard-apex", help="apex of a *.domain allowlist entry")
    parser.add_argument("--http-path", help="optional path for one GET to the allowed host")
    args = parser.parse_args()
    host, _, port = args.proxy.rpartition(":")
    proxy = (host, int(port))
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    for ip, port_number in DIRECT_TARGETS:
        try:
            socket.create_connection((ip, port_number), timeout=3).close()
            check(f"no direct route to {ip}:{port_number}", False, "connected")
        except OSError as exc:
            check(f"no direct route to {ip}:{port_number}", True, type(exc).__name__)
    try:
        answers = socket.getaddrinfo(args.allowed_host, 443)
        check("no external DNS", False, f"resolved {answers[0][4][0]}")
    except OSError as exc:
        check("no external DNS", True, type(exc).__name__)

    denied = {
        "deny non-allowlisted host": f"{args.denied_host}:443",
        "deny port 80": f"{args.allowed_host}:80",
        "deny IP literal": "1.1.1.1:443",
        "deny proxy-internal target": f"{host}:{port}",
    }
    if args.private_host:
        denied["deny allowlisted host with non-public address"] = f"{args.private_host}:443"
    if args.wildcard_apex:
        denied["deny apex of wildcard entry"] = f"{args.wildcard_apex}:443"
    for name, target in denied.items():
        status = connect_status(proxy, target)
        check(name, status == 403, f"status {status}")

    status, sock = proxy_request(
        proxy, f"GET http://{args.allowed_host}/ HTTP/1.1\r\nHost: {args.allowed_host}"
    )
    sock.close()
    check("deny plain HTTP forwarding", status == 403, f"status {status}")

    status, sock = proxy_request(
        proxy, f"CONNECT {args.allowed_host}:443 HTTP/1.1\r\nHost: {args.allowed_host}:443"
    )
    if status != 200:
        sock.close()
        check("tunnel to allowed host", False, f"status {status}")
    else:
        try:
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=args.allowed_host) as tls:
                detail = f"TLS {tls.version()} verified for {args.allowed_host}"
                if args.http_path:
                    tls.sendall(
                        f"GET {args.http_path} HTTP/1.1\r\nHost: {args.allowed_host}\r\n"
                        "Connection: close\r\nUser-Agent: JobFinderAgent/egress-verify\r\n\r\n"
                        .encode("ascii")
                    )
                    detail += f"; {tls.recv(64).split(b'\r\n', 1)[0].decode('latin-1')}"
            check("tunnel to allowed host", True, detail)
        except (OSError, ssl.SSLError) as exc:
            check("tunnel to allowed host", False, type(exc).__name__)

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [name for name, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
