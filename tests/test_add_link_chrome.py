"""The real Chrome against a hostile "public website" (a local server) that tries to reach an "internal" server by every route
from the security review: redirects, meta refresh, scripts, iframes, images, fetch(), downloads. Skipped without Chrome."""
from __future__ import annotations

import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dailyread import add_link
from dailyread.safe_proxy import Denied, FilteringProxy

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
pytestmark = pytest.mark.skipif(not Path(CHROME).exists(), reason="Google Chrome is not installed")


class Internal(BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):
        Internal.hits.append(self.path)
        body = b"<html><body>internalsecret</body></html>"
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    do_POST = do_GET

    def log_message(self, *a): pass


class Pub(BaseHTTPRequestHandler):
    internal_port = 0
    download_name = "x.txt"

    def do_GET(self):
        t = f"http://127.0.0.1:{Pub.internal_port}"
        if self.path == "/article":
            page = "<html><head><title>ok</title></head><body><article><p>ARTICLE-MARKER " + "Real text here. " * 50 + "</p></article></body></html>"
        elif self.path == "/redirect":
            self.send_response(302); self.send_header("Location", t + "/secret"); self.end_headers(); return
        elif self.path == "/embed":
            page = (f'<html><body><p>embed</p><img src="{t}/pixel"><iframe src="{t}/frame"></iframe><script src="{t}/s.js"></script>'
                    f'<link rel="stylesheet" href="{t}/c.css"><script>fetch("{t}/fetch").catch(function(){{}});'
                    f'new Image().src="{t}/img2"; var x=new XMLHttpRequest(); x.open("GET","{t}/xhr"); x.send();</script></body></html>')
        elif self.path == "/meta":
            page = f'<html><head><meta http-equiv="refresh" content="0;url={t}/meta-target"></head><body>wait</body></html>'
        elif self.path == "/js":
            page = f'<html><body><script>location.href="{t}/js-target"</script></body></html>'
        elif self.path == "/download":
            body = b"malicious bytes"
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{Pub.download_name}"'); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        else:
            self.send_response(404); self.end_headers(); return
        data = page.encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a): pass


@pytest.fixture
def world():
    Internal.hits = []
    internal = ThreadingHTTPServer(("127.0.0.1", 0), Internal)
    pub = ThreadingHTTPServer(("127.0.0.1", 0), Pub)
    Pub.internal_port = internal.server_address[1]
    for srv in (internal, pub):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    pub_port = pub.server_address[1]

    def check(host, port):                       # the production rules would also refuse pub.test; this lets the test site through
        if host == "pub.test" and port == pub_port:
            return "127.0.0.1"
        raise Denied("private")
    yield {"pub": f"http://pub.test:{pub_port}", "internal": f"http://127.0.0.1:{internal.server_address[1]}", "check": check}
    for srv in (internal, pub):
        srv.shutdown(); srv.server_close()


def read(world, path, timeout=20):
    with FilteringProxy(check=world["check"]) as proxy:
        html = add_link.fetch_chrome(world["pub"] + path, CHROME, timeout, proxy.url)
        time.sleep(0.5)
        return html, list(proxy.denied)


def test_chrome_reads_a_normal_public_page_through_the_filter(world):
    html, denied = read(world, "/article")
    assert "ARTICLE-MARKER" in html


def test_a_redirect_to_a_private_address_is_not_followed(world):
    html, denied = read(world, "/redirect")
    assert Internal.hits == [] and "internalsecret" not in html and denied


@pytest.mark.parametrize("path", ["/embed", "/meta", "/js"])
def test_pages_cannot_reach_private_addresses_through_images_frames_scripts_fetch_or_navigation(world, path):
    html, denied = read(world, path)
    assert Internal.hits == [], f"{path} reached the internal server: {Internal.hits}"
    assert "internalsecret" not in html and denied


def test_typing_a_loopback_address_straight_into_chrome_is_refused_too(world):
    with FilteringProxy(check=world["check"]) as proxy:
        html = add_link.fetch_chrome(world["internal"] + "/", CHROME, 15, proxy.url)
    assert Internal.hits == [] and "internalsecret" not in html


def test_a_download_does_not_land_in_the_real_downloads_folder(world):
    Pub.download_name = f"dr-test-{uuid.uuid4().hex}.txt"
    read(world, "/download", timeout=10)
    assert not (Path.home() / "Downloads" / Pub.download_name).exists()


def test_control_the_same_attack_works_when_the_filter_is_switched_off(world):
    """Proves the tests above are meaningful: without the proxy, Chrome follows the redirect and the internal server answers."""
    pub_direct = world["pub"].replace("pub.test", "127.0.0.1")
    html = add_link.fetch_chrome(pub_direct + "/redirect", CHROME, 15, None)
    assert Internal.hits and "internalsecret" in html
