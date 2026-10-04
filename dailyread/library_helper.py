"""Library helper: a tiny local web server that lets the report page save Read Later / Favorites straight into
state/library.json, with no clicks. Standard library only.

It does not run all day. The LaunchAgent (schedule.py) keeps the port open and starts the agent on the first request
(a click, or opening a report); the process exits after `helper_idle_minutes` (default 3) without a request, and
launchd starts it again on the next one. Safety: listens on 127.0.0.1 only, every request needs the secret token
from state/library.token, the browser's Origin must be a local file (not a website), the Host must be this address,
bodies are size-limited, and everything is validated before it is merged into the file (atomically)."""
from __future__ import annotations

import ctypes
import ctypes.util
import hmac
import json
import logging
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path

from .library import clean_doc, load_library, merge_docs, quarantine_if_damaged, save_doc, stamp_after

log = logging.getLogger(__name__)
MAX_BODY = 5_000_000
POLL_SECONDS = 5           # the loop wakes this often to check for idleness / stop (one cheap wakeup per 5 s)
HOLD_POLL_SECONDS = 1      # ...and this often while the daily check runs, so stop() takes effect quickly
MAX_ADD_BODY = 8_000          # a pasted link is tiny
MAX_CONNECTIONS = 64       # a local process cannot pin hundreds of threads (or keep the helper alive) with idle sockets


class Handler(BaseHTTPRequestHandler):
    server_version = "DailyReadLibrary"
    timeout = 5                # a browser's speculative idle connection must not hold a thread (or the helper) for long

    def log_message(self, fmt, *args):         # quiet: never log tokens or content
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
        try:
            self._send_unsafe(code, body, ctype)
        except OSError:
            pass                                               # the client is gone; nothing left to tell it

    def _send_unsafe(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # A report opened from disk has Origin "null". The private-network header is for Chrome versions that still
        # send the old preflight (Local Network Access replaced it in Chrome 142+); harmless otherwise.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type, x-dailyread-token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        """Host must be this server (DNS rebinding) and Origin a local file or absent (not another website)."""
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        return self.headers.get("Origin") in (None, "null")

    def _authorised(self) -> bool:
        given = self.headers.get_all("X-DailyRead-Token") or []
        if len(given) != 1:                                    # missing, or several (which one would count?)
            return False
        return hmac.compare_digest(given[0].encode("utf-8", "surrogateescape"), self.server.token.encode())

    def do_OPTIONS(self):
        self.server.touch()
        self._send(204 if self._allowed() else 403)

    def do_GET(self):
        self.server.touch()
        if not self._allowed() or not self._authorised():
            return self._send(403)
        if self.path != "/library":
            return self._send(404)
        with self.server.lock:
            body = json.dumps(load_library(self.server.path)).encode()
        self._send(200, body)

    def _add(self, raw: bytes) -> None:
        """POST /add {"url": ...}: read the page, summarize and rate it, save it to Read Later, answer with the record."""
        try:
            url = json.loads(raw).get("url")
        except (ValueError, RecursionError, AttributeError):
            url = None
        if not isinstance(url, str) or not url.strip():
            return self._json(400, {"code": "invalid_url", "error": "Paste a web link to add."})
        if not self.server.add_gate.acquire(blocking=False):         # one link at a time: each one starts Chrome and Claude
            return self._json(429, {"code": "busy", "error": "Another link is still being read. Try again in a moment."})
        try:
            from .add_link import AddLinkError
            try:
                rec = self.server.add_processor(url)
            except AddLinkError as e:
                return self._json(e.status, {"code": e.code, "error": e.message})
            except Exception:
                log.exception("adding %s failed", url)
                return self._json(500, {"code": "failed", "error": "Something went wrong while reading that page. Nothing was added."})
            try:
                with self.server.lock:
                    quarantine_if_damaged(self.server.path)
                    doc = load_library(self.server.path)
                    old = doc["items"].get(rec["id"])
                    existing = bool(old and old["read_later"])
                    if old:                                           # same link again: keep its favorite flag, put it back
                        rec = {**rec, "favorite": old["favorite"], "added_at": old["added_at"] if existing else rec["added_at"]}
                        if rec["updated_at"] <= old["updated_at"]:    # the newest change wins a merge: this one must be newer
                            rec["updated_at"] = stamp_after(old["updated_at"])
                    merged = merge_docs(doc, {"version": 1, "updated_at": rec["updated_at"], "items": {rec["id"]: rec}})
                    save_doc(self.server.path, merged)
            except (OSError, ValueError) as e:
                log.error("could not save %s: %s", self.server.path, e)
                return self._json(500, {"code": "failed", "error": "The page was read but your list could not be saved."})
            self._json(200, {"record": merged["items"][rec["id"]], "existing": existing, "doc": merged})
        finally:
            self.server.add_gate.release()

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode())

    def do_POST(self):
        self.server.touch()
        if not self._allowed() or not self._authorised():
            return self._send(403)
        if self.path not in ("/library", "/add"):
            return self._send(404)
        adding = self.path == "/add"
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411)
        if length < 0:
            return self._send(400)
        if length > (MAX_ADD_BODY if adding else MAX_BODY):
            return self._send(413)
        try:
            raw = self.rfile.read(length)
        except OSError:                                        # the client stalled: the 5 s connection timeout fired
            return self._send(408)
        if len(raw) != length:                                 # the client hung up mid-body
            return self._send(400)
        if adding:
            return self._add(raw)
        try:
            incoming = clean_doc(json.loads(raw))
        except (ValueError, RecursionError):                   # not JSON, or nested so deeply that parsing gave up
            incoming = None
        if incoming is None:
            return self._send(400, b'{"error": "not a library document"}')
        try:
            with self.server.lock:           # read-merge-write must not interleave between two requests
                quarantine_if_damaged(self.server.path)
                merged = merge_docs(load_library(self.server.path), incoming)
                save_doc(self.server.path, merged)
        except (OSError, ValueError) as e:
            log.error("could not save %s: %s", self.server.path, e)
            return self._send(500)
        self._send(200, json.dumps(merged).encode())


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 64            # the default of 5 resets a burst of simultaneous saves

    def __init__(self, address, path: Path, token: str, idle_seconds: float, bind: bool = True):
        super().__init__(address, Handler, bind_and_activate=bind)
        self.path, self.token, self.idle_seconds = path, token, idle_seconds
        self.poll = POLL_SECONDS           # how long one accept() wait lasts; tests shorten it
        self.last_activity = time.monotonic()
        self.requests = 0
        self.active = 0                    # requests being handled right now (never exit under one)
        self.hold = False                  # True: never expire for idleness (the daily check is running in the same process)
        self._stop = False
        self.lock = threading.Lock()
        self.add_gate = threading.BoundedSemaphore(1)
        self._add_processor = None                # tests replace this; by default it loads the config and runs add_link.process

    def get_request(self):
        conn = super().get_request()
        self.touch()                       # an accepted connection is activity, even before its handler thread has run
        with self.lock:
            if self.active >= MAX_CONNECTIONS:
                conn[0].close()            # too many open connections: drop this one at once
                raise OSError("too many connections")      # handled by handle_request: it just returns
            self.active += 1
        return conn

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.lock:
                self.active -= 1
            self.touch()                   # the idle time counts from the END of the last request

    @property
    def add_processor(self):
        if self._add_processor is None:
            from . import add_link
            from .config import load_config
            cfg = load_config()
            self._add_processor = lambda url: add_link.process(cfg, url)
        return self._add_processor

    @add_processor.setter
    def add_processor(self, fn) -> None:
        self._add_processor = fn

    def touch(self) -> None:
        self.last_activity = time.monotonic()
        self.requests += 1

    def idle_expired(self, now: float | None = None) -> bool:
        if self.active:
            return False
        return (now if now is not None else time.monotonic()) - self.last_activity >= self.idle_seconds

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        """Serve until nothing has asked for `idle_seconds` (or stop() / hold), then return: the process exits."""
        while not self._stop and (self.hold or not self.idle_expired()):
            self.timeout = min(HOLD_POLL_SECONDS, self.poll) if self.hold else self.poll
            self.handle_request()
        log.info("library helper finished: exiting")


def launchd_socket(name: str = "Listener") -> socket.socket:
    """The listening socket launchd opened for us (macOS socket activation)."""
    lib = ctypes.CDLL(ctypes.util.find_library("System"))
    fds = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    if lib.launch_activate_socket(name.encode(), ctypes.byref(fds), ctypes.byref(count)) != 0 or count.value < 1:
        raise RuntimeError("no listening socket from launchd (was this started by the LaunchAgent?)")
    fd = fds[0]
    ctypes.CDLL(None).free(fds)
    return socket.socket(fileno=fd)


def inherited_socket() -> socket.socket | None:
    """launchd's socket, or None when this process was not started by a LaunchAgent that has one."""
    try:
        return launchd_socket()
    except (RuntimeError, OSError, AttributeError):
        return None


def connection_pending(sock: socket.socket) -> bool:
    """True when a client is already waiting on the listening socket: launchd started us because of a request."""
    return bool(select.select([sock], [], [], 0)[0])


def serve(path: Path, token: str, idle_minutes: float, port: int | None = None, sock: socket.socket | None = None) -> None:
    """Run the helper until it has been idle for `idle_minutes`. Listens on `sock` (from launchd) or on 127.0.0.1:port."""
    make_server(path, token, idle_minutes, port, sock).run()


def make_server(path: Path, token: str, idle_minutes: float, port: int | None = None,
                sock: socket.socket | None = None) -> Server:
    idle = idle_minutes * 60
    if sock is not None:
        server = Server(("127.0.0.1", 0), path, token, idle, bind=False)
        server.socket.close()
        server.socket = sock
        server.server_address = sock.getsockname()
        return server
    if port is not None:
        return Server(("127.0.0.1", port), path, token, idle)
    raise ValueError("a port or a socket from launchd is needed")
