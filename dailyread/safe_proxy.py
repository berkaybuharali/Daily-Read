"""A tiny filtering HTTP/HTTPS proxy: the only way "Add a link" is allowed to touch the network.

Both the plain page fetch and the headless Chrome are pointed at it. For EVERY connection (the page, redirects, iframes,
images, scripts) it resolves the host itself, refuses anything that is not a public website, and connects to the exact
address it validated. That closes three holes at once: redirects and subresources to your router or localhost services,
DNS rebinding (the name cannot change between the check and the connection), and proxy settings of the environment.

It listens on 127.0.0.1 only, lives for the duration of one "add", and is standard library only. HTTPS is tunnelled
(CONNECT), never opened: it sees host and port, not content.
"""
from __future__ import annotations

import ipaddress
import logging
import select
import socket
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ALLOWED_PORTS = (80, 443)
# Ranges Python's `is_global` treats as global but that can encode a private address (NAT64, 6to4, Teredo).
EXTRA_BLOCKED = [ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")]
DENY = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class Denied(Exception):
    """The destination is not a public website (or not an allowed port)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    """True only for a normal public address. `is_global` alone lets some special ranges through, so each is refused too."""
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return not any(ip.version == n.version and ip in n for n in EXTRA_BLOCKED)


def resolve_public(host: str, port: int, ports: tuple[int, ...] = ALLOWED_PORTS) -> str:
    """The one IP to connect to for `host`, or Denied. Every address the name resolves to must be public."""
    if port not in ports:
        raise Denied("port")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise Denied("unresolvable")
    chosen = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not is_public_ip(ip):
            raise Denied("private")
        chosen = chosen or str(ip)
    if not chosen:
        raise Denied("unresolvable")
    return chosen


def parse_authority(text: str) -> tuple[str, int]:
    """'host:443', '[::1]:443' -> (host, port)."""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = text.rpartition(":")
        if not host:
            host, port = text, ""
    if not host or not (port.isdigit() and 0 < int(port) < 65536):
        raise Denied("address")
    return host, int(port)


class FilteringProxy:
    def __init__(self, check=None, idle: float = 20.0, deadline: float = 150.0, max_bytes: int = 40_000_000, max_conns: int = 48):
        self.check = check or resolve_public
        self.idle, self.max_bytes = idle, max_bytes
        self.deadline = time.monotonic() + deadline            # the whole proxy stops relaying after this
        self.denied: list[str] = []
        self.sock: socket.socket | None = None
        self._closed = threading.Event()
        self._gate = threading.BoundedSemaphore(max_conns)
        self._bytes = 0
        self._lock = threading.Lock()

    # ---- lifecycle ----
    def __enter__(self) -> "FilteringProxy":
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        threading.Thread(target=self._accept_loop, name="filtering-proxy", daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._closed.set()
        try:
            self.sock.close()
        except OSError:
            pass

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    # ---- serving ----
    def _accept_loop(self) -> None:
        self.sock.settimeout(0.5)
        while not self._closed.is_set():
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._gate.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(self.idle)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(8192)
                if not chunk or len(head) > 32_768:
                    return client.sendall(BAD)
                head += chunk
            header, _, rest = head.partition(b"\r\n\r\n")
            lines = header.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3:
                return client.sendall(BAD)
            method, target = parts[0].upper(), parts[1]
            try:
                if method == "CONNECT":
                    host, port = parse_authority(target)
                    ip = self.check(host, port)
                    upstream = socket.create_connection((ip, port), timeout=10)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    leftover = rest
                else:
                    url = urlsplit(target)
                    if url.scheme != "http" or not url.hostname:
                        raise Denied("scheme")
                    port = url.port or 80
                    ip = self.check(url.hostname, port)
                    upstream = socket.create_connection((ip, port), timeout=10)
                    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                    kept = [l for l in lines[1:] if l and not l.lower().startswith(("proxy-", "connection:"))]
                    out = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(kept) + "\r\nConnection: close\r\n\r\n"
                    upstream.sendall(out.encode("latin-1") + rest)
                    leftover = b""
            except Denied as e:
                self.denied.append(e.reason)
                log.info("filtering proxy refused a connection (%s)", e.reason)
                return client.sendall(DENY)
            except OSError:
                return client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            if leftover:
                upstream.sendall(leftover)
            self._relay(client, upstream)
        except OSError:
            pass
        finally:
            for s in (upstream, client):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass
            self._gate.release()

    def _relay(self, a: socket.socket, b: socket.socket) -> None:
        pair = {a: b, b: a}
        while not self._closed.is_set() and time.monotonic() < self.deadline:
            ready, _, _ = select.select(list(pair), [], [], 1.0)
            if not ready:
                continue
            for s in ready:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                with self._lock:
                    self._bytes += len(data)
                    if self._bytes > self.max_bytes:
                        return
                try:
                    pair[s].sendall(data)
                except OSError:
                    return
