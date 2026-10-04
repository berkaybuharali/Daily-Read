"""The filtering proxy: the only way "Add a link" touches the network. Local servers stand in for websites."""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dailyread.safe_proxy import Denied, FilteringProxy, is_public_ip, parse_authority, resolve_public


class Site(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        Site.seen.append((self.path, dict(self.headers)))
        body = f"hello {self.path}".encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a): pass


@pytest.fixture
def site():
    Site.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown(); srv.server_close()


def to_local(host, port):                    # a "public" name that, for the test, maps to our local server
    if host == "pub.test":
        return "127.0.0.1"
    raise Denied("private")


def ask(proxy, raw: bytes, read=True, timeout=5):
    s = socket.create_connection(("127.0.0.1", int(proxy.url.rsplit(":", 1)[1])), timeout=timeout)
    s.sendall(raw)
    out = b""
    try:
        while read:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out


# ---- what counts as public ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("ip, public", [("8.8.8.8", True), ("93.184.216.34", True), ("2606:4700:4700::1111", True),
                                        ("127.0.0.1", False), ("10.0.0.1", False), ("192.168.0.1", False), ("169.254.169.254", False),
                                        ("100.64.0.1", False), ("224.0.0.1", False), ("0.0.0.0", False), ("::1", False), ("fe80::1", False),
                                        ("fc00::1", False), ("::ffff:10.0.0.1", False), ("64:ff9b::7f00:1", False),        # NAT64 -> 127.0.0.1
                                        ("2002:7f00:1::", False), ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False)])      # 6to4, Teredo
def test_which_addresses_are_public(ip, public):
    assert is_public_ip(ipaddress.ip_address(ip)) is public


def test_resolve_public_refuses_the_whole_name_if_any_answer_is_private_and_wrong_ports(monkeypatch):
    answer = lambda *addrs: (lambda host, port, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addrs])
    monkeypatch.setattr(socket, "getaddrinfo", answer("93.184.216.34"))
    assert resolve_public("example.com", 443) == "93.184.216.34"
    monkeypatch.setattr(socket, "getaddrinfo", answer("93.184.216.34", "10.0.0.5"))
    with pytest.raises(Denied):
        resolve_public("rebind.example", 443)
    monkeypatch.setattr(socket, "getaddrinfo", answer("93.184.216.34"))
    for port in (22, 8080, 47821, 0):
        with pytest.raises(Denied):
            resolve_public("example.com", port)


@pytest.mark.parametrize("text, expected", [("example.com:443", ("example.com", 443)), ("[2606:4700::1111]:443", ("2606:4700::1111", 443))])
def test_authority_parsing(text, expected):
    assert parse_authority(text) == expected


@pytest.mark.parametrize("bad", ["example.com", "example.com:", "example.com:0", "example.com:99999", ":443", "[::1", "a:b"])
def test_bad_authorities_are_refused(bad):
    with pytest.raises(Denied):
        parse_authority(bad)


# ---- relaying ---------------------------------------------------------------------------------------------------------------------
def test_a_plain_http_request_is_forwarded_with_proxy_headers_removed(site):
    with FilteringProxy(check=to_local) as proxy:
        out = ask(proxy, f"GET http://pub.test:{site}/page?q=1 HTTP/1.1\r\nHost: pub.test\r\nProxy-Connection: keep-alive\r\nX-Keep: yes\r\n\r\n".encode())
    assert b"200 OK" in out and b"hello /page?q=1" in out
    path, headers = Site.seen[0]
    assert headers.get("Connection") == "close" and "Proxy-Connection" not in headers and headers.get("X-Keep") == "yes"


def test_connect_tunnels_bytes_both_ways_without_looking_inside(site):
    echo = socket.socket(); echo.bind(("127.0.0.1", 0)); echo.listen(1)

    def serve():
        c, _ = echo.accept(); c.sendall(c.recv(100).upper()); c.close()
    threading.Thread(target=serve, daemon=True).start()
    with FilteringProxy(check=to_local) as proxy:
        s = socket.create_connection(("127.0.0.1", int(proxy.url.rsplit(":", 1)[1])), timeout=5)
        s.sendall(f"CONNECT pub.test:{echo.getsockname()[1]} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert b"200 Connection Established" in s.recv(200)
        s.sendall(b"ping through the tunnel")
        assert s.recv(100) == b"PING THROUGH THE TUNNEL"
        s.close()
    echo.close()


def test_the_proxy_connects_to_the_validated_address_not_to_the_name(site, monkeypatch):
    """DNS rebinding: whatever the name resolves to later, the connection goes to the exact address that was checked."""
    targets, real = [], socket.create_connection
    monkeypatch.setattr(socket, "create_connection", lambda address, *a, **k: (targets.append(address), real(address, *a, **k))[1])
    with FilteringProxy(check=lambda host, port: "127.0.0.1") as proxy:
        out = ask(proxy, f"GET http://anything.test:{site}/x HTTP/1.1\r\nHost: anything.test\r\n\r\n".encode())
    assert b"hello /x" in out
    assert all(host == "127.0.0.1" for host, port in targets if port == site)         # an IP literal, never "anything.test"


# ---- refusing --------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", b"CONNECT localhost:443 HTTP/1.1\r\n\r\n", b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n",
    b"CONNECT 192.168.1.1:443 HTTP/1.1\r\n\r\n", b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", b"CONNECT example.com:22 HTTP/1.1\r\n\r\n",
    b"CONNECT example.com:47821 HTTP/1.1\r\n\r\n", b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n", b"GET http://[::1]/ HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET http://example.com:8080/ HTTP/1.1\r\nHost: x\r\n\r\n", b"GET ftp://example.com/ HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /relative HTTP/1.1\r\nHost: x\r\n\r\n", b"CONNECT nonsense HTTP/1.1\r\n\r\n"])
def test_the_default_rules_refuse_everything_that_is_not_a_public_website(raw):
    with FilteringProxy() as proxy:
        out = ask(proxy, raw)
        assert b" 403 " in out.split(b"\r\n")[0], out[:80]
        assert proxy.denied


@pytest.mark.parametrize("raw", [b"garbage\r\n\r\n", b"GET\r\n\r\n", b"A B C D E\r\n\r\n", b"x" * 40_000 + b"\r\n\r\n"])
def test_malformed_requests_get_a_400_and_never_crash_it(raw):
    with FilteringProxy() as proxy:
        out = ask(proxy, raw)
        assert b" 400 " in out.split(b"\r\n")[0] or out == b""
        assert ask(proxy, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")           # still serving


def test_it_listens_on_loopback_only_and_closes_when_done():
    with FilteringProxy() as proxy:
        assert proxy.sock.getsockname()[0] == "127.0.0.1"
        port = proxy.sock.getsockname()[1]
    time.sleep(0.8)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


# ---- limits ----------------------------------------------------------------------------------------------------------------------------
def test_many_idle_connections_do_not_exhaust_it_and_it_recovers(site):
    with FilteringProxy(check=to_local, max_conns=8, idle=2) as proxy:
        port = int(proxy.url.rsplit(":", 1)[1])
        idle = []
        for _ in range(40):
            try:
                idle.append(socket.create_connection(("127.0.0.1", port), timeout=1))
            except OSError:
                break
        for s in idle:
            s.close()
        time.sleep(0.5)
        assert b"hello /ok" in ask(proxy, f"GET http://pub.test:{site}/ok HTTP/1.1\r\nHost: x\r\n\r\n".encode())


def test_a_download_bigger_than_the_byte_cap_is_cut_off(site):
    big = socket.socket(); big.bind(("127.0.0.1", 0)); big.listen(1)

    def serve():
        c, _ = big.accept(); c.recv(1000)
        try:
            for _ in range(200):
                c.sendall(b"x" * 65536)
        except OSError:
            pass
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    with FilteringProxy(check=to_local, max_bytes=300_000) as proxy:
        out = ask(proxy, f"GET http://pub.test:{big.getsockname()[1]}/big HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert len(out) < 700_000
    big.close()


def test_the_whole_proxy_stops_relaying_after_its_deadline():
    slow = socket.socket(); slow.bind(("127.0.0.1", 0)); slow.listen(1)
    threading.Thread(target=lambda: (slow.accept()[0].recv(100), time.sleep(30)), daemon=True).start()
    with FilteringProxy(check=to_local, deadline=1.5) as proxy:
        t0 = time.monotonic()
        out = ask(proxy, f"GET http://pub.test:{slow.getsockname()[1]}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode(), timeout=10)
        assert time.monotonic() - t0 < 6 and out == b""
    slow.close()
