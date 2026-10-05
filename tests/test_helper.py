"""The Read Later auto-save helper (dailyread/library_helper.py): protocol, security checks, lifecycle, agent dispatch."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from dailyread import library_helper, schedule
from dailyread.library import EPOCH, clean_record, load_library, merge_docs, quarantine_if_damaged, stamp_after
from dailyread.library_helper import Server

TOKEN = "test-token"


def rec(rid="a1", **kw):
    base = {"id": rid, "title": f"Title {rid}", "url": "https://example.com/x", "source": "Blog", "published": "2026-10-02",
            "read_later": True, "updated_at": "2026-10-03T10:00:00.000Z"}
    return {**base, **kw}


def doc(*records):
    return {"version": 1, "items": {r["id"]: r for r in records}}


@pytest.fixture
def helper(tmp_path):
    path = tmp_path / "state" / "library.json"
    srv = Server(("127.0.0.1", 0), path, TOKEN, idle_seconds=3600)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path="/library", body=None, token=TOKEN, host=None, origin="null", headers=None, raw_length=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": host or f"127.0.0.1:{port}"}
    if token is not None:
        h["X-DailyRead-Token"] = token
    if origin is not None:
        h["Origin"] = origin
    h.update(headers or {})
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    if raw_length is not None:
        h["Content-Length"] = str(raw_length)
    conn.request(method, path, body=data, headers=h)      # our Host header replaces the automatic one
    r = conn.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    conn.close()
    return out


# ---- protocol -----------------------------------------------------------------------------------------------------
def test_get_returns_the_library_and_post_merges_and_saves_it(helper):
    status, _, body = call(helper, "GET")
    assert status == 200 and json.loads(body)["items"] == {}
    status, _, body = call(helper, "POST", body=doc(rec("a1")))
    assert status == 200 and list(json.loads(body)["items"]) == ["a1"]
    on_disk = json.loads(helper.path.read_text())
    assert on_disk["items"]["a1"]["title"] == "Title a1" and on_disk["version"] == 1
    assert (helper.path.stat().st_mode & 0o777) == 0o600           # personal data: owner only
    assert list(helper.path.parent.glob(".library-*.tmp")) == []   # atomic write leaves no temp files


def test_two_browsers_are_merged_newest_change_wins(helper):
    call(helper, "POST", body=doc(rec("a1", read_later=True, updated_at="2026-10-03T10:00:00.000Z"), rec("b2")))
    status, _, body = call(helper, "POST", body=doc(rec("a1", read_later=False, updated_at="2026-10-04T10:00:00.000Z"), rec("c3")))
    items = json.loads(body)["items"]
    assert sorted(items) == ["a1", "b2", "c3"] and items["a1"]["read_later"] is False
    stale = call(helper, "POST", body=doc(rec("a1", read_later=True, updated_at="2026-10-01T00:00:00.000Z")))
    assert json.loads(stale[2])["items"]["a1"]["read_later"] is False      # an older copy cannot undo a newer removal


def test_unknown_path_is_404(helper):
    assert call(helper, "GET", "/other")[0] == 404 and call(helper, "POST", "/other", body=doc())[0] == 404


# ---- security: every refusal happens before anything is read or written -----------------------------------------------
@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x"])
def test_requests_without_the_right_token_are_refused(helper, token):
    assert call(helper, "GET", token=token)[0] == 403
    assert call(helper, "POST", body=doc(rec()), token=token)[0] == 403
    assert not helper.path.exists()


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost:3000", "null.example", "file://"])
def test_a_website_origin_is_refused_even_with_the_token(helper, origin):
    assert call(helper, "GET", origin=origin)[0] == 403
    assert call(helper, "POST", body=doc(rec()), origin=origin)[0] == 403
    assert call(helper, "OPTIONS", origin=origin)[0] == 403
    assert not helper.path.exists()


def test_local_file_pages_and_command_line_clients_are_accepted(helper):
    assert call(helper, "GET", origin="null")[0] == 200          # a report opened from disk sends Origin: null
    assert call(helper, "GET", origin=None)[0] == 200            # curl sends no Origin


@pytest.mark.parametrize("host", ["evil.example", "evil.example:1", "127.0.0.1", "0.0.0.0:1", "[::1]:1"])
def test_other_host_names_are_refused_against_dns_rebinding(helper, host):
    assert call(helper, "GET", host=host)[0] == 403


def test_localhost_name_is_an_allowed_host(helper):
    assert call(helper, "GET", host=f"localhost:{helper.server_address[1]}")[0] == 200


def test_preflight_answers_with_cors_and_private_network_headers(helper):
    status, headers, _ = call(helper, "OPTIONS", token=None)
    h = {k.lower(): v for k, v in headers.items()}
    assert status == 204 and h["access-control-allow-origin"] == "*"
    assert h["access-control-allow-private-network"] == "true"
    assert "x-dailyread-token" in h["access-control-allow-headers"].lower()
    assert h["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [b"{broken", b"[]", b'"x"', b'{"items": []}', b'{"hello": 1}', b""])
def test_bodies_that_are_not_a_library_are_rejected_and_change_nothing(helper, body):
    helper.path.parent.mkdir(parents=True)
    helper.path.write_text(json.dumps(doc(rec("keep"))))
    status = call(helper, "POST", body=body)[0]
    assert status == 400 and "keep" in json.loads(helper.path.read_text())["items"]


def test_oversized_and_unsized_bodies_are_refused_before_reading(helper):
    assert call(helper, "POST", body=b"{}", raw_length=library_helper.MAX_BODY + 1)[0] == 413
    port = helper.server_address[1]
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(f"POST /library HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: null\r\nX-DailyRead-Token: {TOKEN}\r\n\r\n".encode())
    assert s.recv(64).startswith(b"HTTP/1.0 411")
    s.close()


def test_hostile_records_are_cleaned_on_the_way_in(helper):
    evil = rec("e1", url="javascript:alert(1)", title="<img onerror=x>", stars=99, report="../../etc/passwd")
    call(helper, "POST", body=doc(evil))
    saved = json.loads(helper.path.read_text())["items"]["e1"]
    assert saved["url"] == "" and saved["stars"] == 0 and saved["report"] == ""


def test_a_damaged_file_is_set_aside_not_overwritten(helper):
    helper.path.parent.mkdir(parents=True)
    helper.path.write_text("{ this was my only copy")
    assert call(helper, "POST", body=doc(rec("a1")))[0] == 200
    kept = list(helper.path.parent.glob("library.damaged-*.json"))
    assert len(kept) == 1 and kept[0].read_text() == "{ this was my only copy"
    assert "a1" in json.loads(helper.path.read_text())["items"]


def test_quarantine_leaves_healthy_and_missing_files_alone(tmp_path):
    f = tmp_path / "library.json"
    assert quarantine_if_damaged(f) is None
    f.write_text(json.dumps(doc(rec())))
    assert quarantine_if_damaged(f) is None and f.exists()
    for bad in ("[]", "null", '{"items": 3}'):
        f.write_text(bad)
        assert quarantine_if_damaged(f) is not None and not f.exists()


def test_an_unwritable_state_folder_is_a_500_not_a_crash(tmp_path):
    blocker = tmp_path / "state"
    blocker.write_text("i am a file, not a folder")
    srv = Server(("127.0.0.1", 0), blocker / "library.json", TOKEN, 3600)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(srv, "POST", body=doc(rec()))[0] == 500
        assert call(srv, "GET")[0] == 200                 # and it keeps serving
    finally:
        srv.shutdown(); srv.server_close()


def test_merge_docs_drops_only_old_tombstones():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    old = "2025-01-01T00:00:00.000Z"
    a = {"items": {"gone": clean_record(rec("gone", read_later=False, updated_at=old)),
                   "kept": clean_record(rec("kept", updated_at=old)),
                   "recent": clean_record(rec("recent", read_later=False, updated_at="2026-10-01T00:00:00.000Z"))}}
    out = merge_docs(a, {"items": {}}, now=now)
    assert sorted(out["items"]) == ["kept", "recent"]


# ---- lifecycle: it must not linger -------------------------------------------------------------------------------------
def test_idle_expiry_and_activity_reset(tmp_path):
    srv = Server(("127.0.0.1", 0), tmp_path / "l.json", TOKEN, idle_seconds=180)
    try:
        t0 = srv.last_activity
        assert not srv.idle_expired(t0 + 179) and srv.idle_expired(t0 + 180)
        srv.touch()
        assert not srv.idle_expired(srv.last_activity + 179)
    finally:
        srv.server_close()


def test_run_returns_by_itself_after_the_idle_time(tmp_path):
    srv = Server(("127.0.0.1", 0), tmp_path / "l.json", TOKEN, idle_seconds=0.6)
    srv.poll = 0.1
    done = threading.Event()
    threading.Thread(target=lambda: (srv.run(), done.set()), daemon=True).start()
    time.sleep(0.2)
    assert call(srv, "GET")[0] == 200 and not done.is_set()        # a request keeps it alive
    assert done.wait(5), "the helper must exit once it has been idle"
    srv.server_close()


def test_the_idle_check_runs_often_enough_for_a_3_minute_limit():
    assert library_helper.POLL_SECONDS <= 5


def test_serves_on_a_socket_handed_over_by_launchd(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))       # the connection that made launchd start us
    assert library_helper.connection_pending(listener)
    path = tmp_path / "library.json"
    done = threading.Event()
    threading.Thread(target=lambda: (library_helper.serve(path, TOKEN, 0.05, sock=listener), done.set()), daemon=True).start()
    body = json.dumps(doc(rec("a1"))).encode()
    client.sendall(f"POST /library HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: null\r\nX-DailyRead-Token: {TOKEN}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    assert b"200" in client.recv(200).split(b"\r\n")[0]
    assert done.wait(10)
    assert "a1" in json.loads(path.read_text())["items"]
    client.close()


def test_no_connection_pending_means_a_timer_start():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    assert not library_helper.connection_pending(listener)
    listener.close()


# ---- concurrency: a browser's idle connection or parallel saves must not break it ---------------------------------------
def test_an_idle_speculative_connection_does_not_block_other_requests(helper):
    idle = socket.create_connection(("127.0.0.1", helper.server_address[1]))      # Chrome opens these and sends nothing
    try:
        t0 = time.monotonic()
        assert call(helper, "GET")[0] == 200 and time.monotonic() - t0 < 2
    finally:
        idle.close()


def test_parallel_saves_are_all_kept(helper):
    ids = [f"item{i}" for i in range(20)]
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(call(helper, "POST", body=doc(rec(i)))[0])) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == [200] * 20
    assert sorted(json.loads(helper.path.read_text())["items"]) == sorted(ids)


def test_stop_and_hold_control_the_loop(tmp_path):
    srv = Server(("127.0.0.1", 0), tmp_path / "l.json", TOKEN, idle_seconds=0.2)
    srv.poll = 0.1
    srv.hold = True
    done = threading.Event()
    threading.Thread(target=lambda: (srv.run(), done.set()), daemon=True).start()
    assert not done.wait(1.0), "while held it must not expire for idleness"
    srv.stop()
    assert done.wait(3), "stop() ends the loop"
    srv.server_close()


# ---- the agent decides: timer tick or click ---------------------------------------------------------------------------
@pytest.fixture
def agent_env(monkeypatch, tmp_path):
    """A real listening socket standing in for launchd's, an isolated project root, and a very short idle time."""
    from dataclasses import replace
    from dailyread.config import load_config
    monkeypatch.setenv("DAILYREAD_HELPER_IDLE_MINUTES", "0.01")        # 0.6 s
    cfg = replace(load_config(), root=tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    monkeypatch.setattr(library_helper, "inherited_socket", lambda: listener)
    token = library_helper_token(cfg)
    yield cfg, listener, token
    listener.close()


def library_helper_token(cfg):
    from dailyread.library import ensure_token, token_path
    return ensure_token(token_path(cfg))


def post_raw(port, token, body):
    c = socket.create_connection(("127.0.0.1", port))
    data = json.dumps(body).encode()
    c.sendall(f"POST /library HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: null\r\nX-DailyRead-Token: {token}\r\n"
              f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
    return c


def test_a_click_start_serves_until_idle_and_skips_the_daily_check(monkeypatch, agent_env):
    cfg, listener, token = agent_env
    monkeypatch.setattr(schedule, "tick", lambda c: pytest.fail("a click must not run the daily check"))
    client = post_raw(listener.getsockname()[1], token, doc(rec("a1")))      # the connection that made launchd start us
    assert schedule.run_agent(cfg) == 0
    assert b"200" in client.recv(100).split(b"\r\n")[0]
    assert "a1" in json.loads((cfg.root / "state" / "library.json").read_text())["items"]
    client.close()


def test_a_timer_start_runs_the_daily_check_and_still_serves_clicks_meanwhile(monkeypatch, agent_env):
    cfg, listener, token = agent_env
    port, seen = listener.getsockname()[1], {}

    def slow_tick(c):                       # stands for a report being generated (minutes in real life)
        client = post_raw(port, token, doc(rec("during-tick")))
        client.settimeout(5)
        seen["answer"] = client.recv(100).split(b"\r\n")[0]
        client.close()
        return 7

    monkeypatch.setattr(schedule, "tick", slow_tick)
    assert schedule.run_agent(cfg) == 7                                     # the tick's result is the exit code
    assert b"200" in seen["answer"], "a click during the daily check must be answered, not queued"
    assert "during-tick" in json.loads((cfg.root / "state" / "library.json").read_text())["items"]


def test_a_timer_start_nobody_clicked_exits_right_after_the_check(monkeypatch, agent_env):
    cfg, listener, token = agent_env
    monkeypatch.setattr(schedule, "tick", lambda c: 0)
    t0 = time.monotonic()
    assert schedule.run_agent(cfg) == 0
    assert time.monotonic() - t0 < 5, "no lingering when nothing used the helper"


def test_without_a_launchd_socket_it_is_just_the_daily_check(monkeypatch, tmp_path):
    from dataclasses import replace
    from dailyread.config import load_config
    monkeypatch.setattr(library_helper, "inherited_socket", lambda: None)
    monkeypatch.setattr(schedule, "tick", lambda c: 5)
    assert schedule.run_agent(replace(load_config(), root=tmp_path)) == 5


def test_a_failing_daily_check_still_shuts_the_helper_thread_down(monkeypatch, agent_env):
    cfg, listener, token = agent_env
    def boom(c):
        raise RuntimeError("tick failed")
    monkeypatch.setattr(schedule, "tick", boom)
    with pytest.raises(RuntimeError):
        schedule.run_agent(cfg)
    assert not any(t.name == "library-helper" and t.is_alive() for t in threading.enumerate())


def test_a_request_arriving_just_as_the_idle_time_ends_is_still_answered(tmp_path):
    """Regression (found with a real launchd run): a connection accepted after the idle time has passed, but before the
    loop's next check, must still be answered. It used to end the loop and kill the handler thread mid-request."""
    srv = Server(("127.0.0.1", 0), tmp_path / "l.json", TOKEN, idle_seconds=0.3)
    srv.poll = 1.0                                      # the loop is waiting in accept() when the idle time passes
    done = threading.Event()
    threading.Thread(target=lambda: (srv.run(), done.set()), daemon=True).start()
    time.sleep(0.5)                                     # idle time (0.3 s) has passed, the loop has not looked yet
    assert call(srv, "GET")[0] == 200
    assert done.wait(5), "and it still exits by itself afterwards"
    srv.server_close()


def test_the_light_entry_point_does_not_import_the_pipeline():
    import subprocess, sys
    code = ("import sys; import dailyread.agent_main, dailyread.library_helper, dailyread.library; "
            "heavy = [m for m in ('httpx', 'trafilatura', 'feedparser', 'lxml', 'bs4', 'jinja2', 'dailyread.pipeline') if m in sys.modules]; "
            "print(heavy)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


# ---- found by the security and code reviews (2026-10-04): each of these used to crash, hang or exhaust memory ----------
def raw_post(srv, headers: bytes, body: bytes = b""):
    port = srv.server_address[1]
    s = socket.create_connection(("127.0.0.1", port), timeout=9)
    s.sendall(f"POST /library HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: null\r\n".encode() + headers + b"\r\n" + body)
    try:
        return s.recv(200).split(b"\r\n")[0]
    finally:
        s.close()


def test_negative_content_length_is_refused_not_read_to_the_end(helper):
    t0 = time.monotonic()
    line = raw_post(helper, f"X-DailyRead-Token: {TOKEN}\r\nContent-Length: -1\r\n".encode(), b"x" * 10)
    assert b" 400" in line and time.monotonic() - t0 < 2          # it used to read until EOF: gigabytes of memory


def test_a_short_body_is_a_400_not_a_hang(helper):
    line = raw_post(helper, f"X-DailyRead-Token: {TOKEN}\r\nContent-Length: 500\r\n".encode(), b"{}")
    assert b" 408" in line or b" 400" in line or line == b""      # the 5 s connection timeout ends the wait, with no traceback


def test_a_non_ascii_token_is_just_a_403(helper):
    assert b" 403" in raw_post(helper, "X-DailyRead-Token: tökén\r\nContent-Length: 2\r\n".encode(), b"{}")
    assert call(helper, "GET")[0] == 200                           # and the helper is fine afterwards


def test_two_token_headers_are_refused(helper):
    two = f"X-DailyRead-Token: {TOKEN}\r\nX-DailyRead-Token: other\r\nContent-Length: 2\r\n".encode()
    assert b" 403" in raw_post(helper, two, b"{}")


def test_absurdly_nested_json_is_a_400(helper):
    assert call(helper, "POST", body=b"[" * 100_000)[0] == 400
    assert call(helper, "POST", body=b'{"items":' * 50_000)[0] == 400
    assert call(helper, "GET")[0] == 200


def test_impossible_and_future_timestamps_cannot_break_or_pin_a_record(helper):
    from datetime import datetime, timedelta, timezone
    bad = rec("bad", updated_at="0000-00-00T99:99:99Z")
    future = rec("future", read_later=False, updated_at="9999-12-31T23:59:59.000Z")
    assert call(helper, "POST", body=doc(bad, future))[0] == 200
    saved = json.loads(helper.path.read_text())["items"]
    assert saved["bad"]["updated_at"] == EPOCH
    stamp = datetime.fromisoformat(saved["future"]["updated_at"].replace("Z", "+00:00"))
    assert stamp <= datetime.now(timezone.utc) + timedelta(minutes=6)          # clamped: a later change can still win


def test_a_lone_surrogate_from_a_cut_emoji_is_saved_cleanly(helper):
    body = b'{"items": {"s1": {"id": "s1", "title": "cut emoji \\ud83d", "read_later": true}}}'
    assert call(helper, "POST", body=body)[0] == 200
    assert "cut emoji" in json.loads(helper.path.read_text())["items"]["s1"]["title"]


def test_whole_number_stars_are_accepted_like_the_browser_does(helper):
    call(helper, "POST", body=b'{"items": {"a": {"id": "a", "title": "t", "stars": 3.0, "read_later": true}}}')
    assert json.loads(helper.path.read_text())["items"]["a"]["stars"] == 3


def test_many_idle_connections_are_capped_and_the_helper_recovers(helper):
    from dailyread.library_helper import MAX_CONNECTIONS
    port = helper.server_address[1]
    idle = []
    for _ in range(MAX_CONNECTIONS + 40):
        try:
            idle.append(socket.create_connection(("127.0.0.1", port), timeout=2))
        except OSError:
            break
    time.sleep(0.3)
    assert helper.active <= MAX_CONNECTIONS
    for s in idle:
        s.close()
    deadline = time.time() + 8
    while time.time() < deadline and helper.active:
        time.sleep(0.1)
    assert call(helper, "GET")[0] == 200


def test_the_token_is_created_owner_only_and_never_follows_a_symlink(tmp_path):
    import os
    from dailyread.library import ensure_token
    path = tmp_path / "state" / "library.token"
    token = ensure_token(path)
    assert path.read_text() == token and (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert ensure_token(path) == token                                  # stable: old reports keep working
    planted = tmp_path / "state2" / "library.token"
    planted.parent.mkdir()
    os.symlink(tmp_path / "elsewhere", planted)                          # dangling symlink put there by someone else
    with pytest.raises(OSError):
        ensure_token(planted)
    assert not (tmp_path / "elsewhere").exists()


def test_the_agent_drains_a_waiting_connection_when_it_cannot_serve(monkeypatch, tmp_path):
    """No token + a waiting connection: it must not leave the connection queued (launchd could keep restarting us)."""
    from dataclasses import replace
    from dailyread import agent_main, config
    cfg = replace(config.load_config(), root=tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    monkeypatch.setattr(library_helper, "inherited_socket", lambda: listener)
    client = socket.create_connection(("127.0.0.1", listener.getsockname()[1]))
    assert agent_main.main() == 1
    client.settimeout(3)
    assert client.recv(10) == b""                                        # closed by the agent, not left hanging
    assert not library_helper.connection_pending(listener)
    listener.close()


def test_the_agent_drains_and_reraises_on_an_unexpected_error(monkeypatch, tmp_path):
    from dailyread import agent_main, config
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    monkeypatch.setattr(library_helper, "inherited_socket", lambda: listener)
    client = socket.create_connection(("127.0.0.1", listener.getsockname()[1]))

    def broken():
        raise RuntimeError("typo in config.local.yaml")
    monkeypatch.setattr(config, "load_config", broken)
    with pytest.raises(RuntimeError):
        agent_main.main()
    client.settimeout(3)
    assert client.recv(10) == b""
    listener.close()


def test_a_failed_self_test_leaves_no_token_and_no_socket(monkeypatch, tmp_path):
    from dataclasses import replace
    from dailyread.config import load_config
    cfg = replace(load_config(), root=tmp_path)
    loaded = []
    monkeypatch.setattr(schedule, "_load", lambda c, data: loaded.append("Sockets" in data))
    monkeypatch.setattr(schedule, "helper_selftest", lambda c, timeout=20: (False, "connection refused"))
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr(schedule, "port_free", lambda p: True)
    message = schedule.install(cfg)
    assert loaded == [True, False]                       # first with the socket, then again without it
    assert not (tmp_path / "state" / "library.token").exists() and "NOT enabled" in message


def test_status_looks_for_the_process_the_plist_actually_starts(monkeypatch, tmp_path):
    from dataclasses import replace
    from dailyread.config import load_config
    cfg = replace(load_config(), root=tmp_path)
    seen = []
    monkeypatch.setattr(schedule.subprocess, "run", lambda args, **k: seen.append(args) or type("R", (), {"stdout": "", "returncode": 1})())
    schedule._helper_running()
    pattern = seen[0][-1]
    assert pattern in " ".join(schedule.build_plist(cfg, env={})["ProgramArguments"])


# ---- found by the process review (2026-10-04) --------------------------------------------------------------------------
def _cfg(tmp_path):
    from dataclasses import replace
    from dailyread.config import load_config
    return replace(load_config(), root=tmp_path)


def _write_plist(cfg, **changes):
    import plistlib
    data = {"ProgramArguments": [str(Path(__file__)), "-m", "dailyread.agent_main"], "WorkingDirectory": str(cfg.root),
            "Sockets": {"Listener": {}}, **changes}
    path = schedule.plist_path(cfg)
    return path, data, plistlib


def test_status_spots_an_install_that_points_at_a_moved_project_or_a_missing_python(monkeypatch, tmp_path):
    import plistlib
    cfg = _cfg(tmp_path)
    plist = tmp_path / "job.plist"
    monkeypatch.setattr(schedule, "plist_path", lambda c: plist)
    assert schedule.plist_problems(cfg) == []                                    # not installed: nothing to complain about
    data = {"ProgramArguments": [str(tmp_path / "gone" / "python3"), "-m", "dailyread.agent_main"],
            "WorkingDirectory": "/somewhere/else", "StartInterval": 1800}
    plist.write_bytes(plistlib.dumps(data))
    problems = " | ".join(schedule.plist_problems(cfg))
    assert "does not exist" in problems and "another project folder" in problems and "without the Read Later helper" in problems
    plist.write_bytes(plistlib.dumps({**data, "ProgramArguments": [__file__], "WorkingDirectory": str(tmp_path), "Sockets": {"Listener": {}}}))
    assert schedule.plist_problems(cfg) == []                                    # a healthy install has no problems


def test_calendar_checks_follow_the_configured_report_time():
    assert schedule.calendar_checks({"not_before": "07:00"}) == [{"Hour": 7, "Minute": 5}, {"Hour": 12, "Minute": 0}]
    assert schedule.calendar_checks({"not_before": "23:58"}) == [{"Hour": 0, "Minute": 3}, {"Hour": 12, "Minute": 0}]


def test_install_refuses_while_a_report_is_being_generated(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from dailyread.state import LockedError
    cfg = _cfg(tmp_path)

    class Busy:
        def __init__(self, directory): pass
        @contextmanager
        def lock(self):
            raise LockedError("busy")
            yield
    monkeypatch.setattr(schedule, "StateStore", Busy)
    monkeypatch.setattr(schedule, "_load", lambda *a: pytest.fail("must not touch launchd while a report runs"))
    with pytest.raises(RuntimeError, match="being generated"):
        schedule.install(cfg)


def test_install_skips_the_helper_when_the_port_is_taken_and_keeps_the_daily_schedule(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    loaded = []
    monkeypatch.setattr(schedule, "_load", lambda c, data: loaded.append("Sockets" in data))
    monkeypatch.setattr(schedule, "port_free", lambda port: False)
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    message = schedule.install(cfg)
    assert loaded == [False] and "port 47821 is used by another program" in message
    assert not (tmp_path / "state" / "library.token").exists()


def test_install_keeps_the_daily_schedule_if_launchd_cannot_open_the_port(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    def load(c, data):
        calls.append("Sockets" in data)
        if "Sockets" in data:
            raise RuntimeError("launchctl bootstrap failed")
    monkeypatch.setattr(schedule, "_load", load)
    monkeypatch.setattr(schedule, "port_free", lambda port: True)
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    message = schedule.install(cfg)
    assert calls == [True, False] and "NOT enabled" in message
    assert not (tmp_path / "state" / "library.token").exists()


def test_port_free_detects_a_listening_program():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    assert not schedule.port_free(port)
    s.close()
    assert schedule.port_free(port)


def test_uninstall_removes_the_token_but_keeps_the_lists(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "library.token").write_text("t")
    (tmp_path / "state" / "library.json").write_text(json.dumps(doc(rec())))
    monkeypatch.setattr(schedule, "plist_path", lambda c: tmp_path / "x.plist")
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    schedule.uninstall(cfg)
    assert not (tmp_path / "state" / "library.token").exists() and (tmp_path / "state" / "library.json").exists()


def test_a_huge_library_file_is_never_parsed(tmp_path):
    from dailyread.library import MAX_FILE_BYTES
    f = tmp_path / "library.json"
    f.write_text("{" + " " * (MAX_FILE_BYTES + 10) + "}")
    assert load_library(f)["items"] == {}
    assert quarantine_if_damaged(f) is not None                  # and the next save sets it aside instead of reading it


def test_only_a_few_damaged_copies_are_kept(tmp_path):
    from dailyread.library import KEEP_DAMAGED
    f = tmp_path / "library.json"
    for n in range(KEEP_DAMAGED + 4):
        f.write_text("{broken")
        dest = quarantine_if_damaged(f)
        assert dest is not None
        dest.rename(dest.with_name(f"library.damaged-2026010{n % 10}-00000{n}.json"))        # distinct, ordered names
        f.write_text("{broken again")
        quarantine_if_damaged(f)
        for extra in tmp_path.glob("library.damaged-*"):
            pass
    assert len(list(tmp_path.glob("library.damaged-*.json"))) <= KEEP_DAMAGED + 1


# ---- POST /add: paste a link, it is read, rated and saved (the page reading itself is faked; see test_add_link.py) ----------------
def _new_record(url="https://medium.com/google-cloud/some-post-abc123", **changes):
    from dailyread import add_link
    meta = {"url": url, "domain": "medium.com", "page_title": "Raw", "site_name": "Medium", "publisher": "", "author": "", "description": "", "published": ""}
    out = {"readable": True, "title": "A fine article", "source": "Google Cloud Community (Medium)", "summary": "It argues a thing.",
           "stars": 4, "verdict": "read", "reason": "Deep practitioner write-up."}
    return {**add_link.make_record(_cfg_for_add(), url, meta, out), **changes}


def _cfg_for_add():
    from dailyread.config import load_config
    return load_config()


def test_add_needs_the_token_and_a_local_origin_like_every_other_request(helper):
    helper.add_processor = lambda url: pytest.fail("must not run for a refused request")
    assert call(helper, "POST", "/add", body={"url": "https://x.dev/p"}, token=None)[0] == 403
    assert call(helper, "POST", "/add", body={"url": "https://x.dev/p"}, token="wrong")[0] == 403
    assert call(helper, "POST", "/add", body={"url": "https://x.dev/p"}, origin="https://evil.example")[0] == 403
    assert call(helper, "POST", "/add", body={"url": "https://x.dev/p"}, host="evil.example")[0] == 403
    assert not helper.path.exists()


def test_add_saves_the_record_to_read_later_and_answers_with_it(helper):
    rec = _new_record()
    seen = []
    helper.add_processor = lambda url: seen.append(url) or rec
    status, _, body = call(helper, "POST", "/add", body={"url": "  https://medium.com/google-cloud/some-post-abc123?source=social.tw "})
    answer = json.loads(body)
    assert status == 200 and seen == ["  https://medium.com/google-cloud/some-post-abc123?source=social.tw "]      # the raw text goes to the reader
    assert answer["existing"] is False and answer["record"]["id"] == rec["id"] and answer["record"]["manual"] is True
    assert rec["id"] in answer["doc"]["items"]
    saved = json.loads(helper.path.read_text())["items"][rec["id"]]
    assert saved["read_later"] and saved["manual"] and saved["source"] == "Google Cloud Community (Medium)" and saved["title"] == "A fine article"
    assert (helper.path.stat().st_mode & 0o777) == 0o600


def test_adding_the_same_link_again_keeps_its_favorite_and_says_it_was_already_there(helper):
    rec = _new_record()
    helper.add_processor = lambda url: rec
    call(helper, "POST", "/add", body={"url": "x"})
    saved = json.loads(helper.path.read_text())["items"][rec["id"]]
    call(helper, "POST", "/library", body=doc({**saved, "favorite": True, "updated_at": stamp_after(saved["updated_at"])}))
    answer = json.loads(call(helper, "POST", "/add", body={"url": "x"})[2])
    assert answer["existing"] is True and answer["record"]["favorite"] is True and answer["record"]["read_later"] is True
    assert len(json.loads(helper.path.read_text())["items"]) == 1


def test_a_link_you_removed_earlier_comes_back_when_you_add_it_again(helper):
    rec = _new_record()
    helper.add_processor = lambda url: rec
    call(helper, "POST", "/add", body={"url": "x"})
    saved = json.loads(helper.path.read_text())["items"][rec["id"]]
    call(helper, "POST", "/library", body=doc({**saved, "read_later": False, "updated_at": stamp_after(saved["updated_at"])}))
    answer = json.loads(call(helper, "POST", "/add", body={"url": "x"})[2])
    assert answer["existing"] is False and answer["record"]["read_later"] is True


@pytest.mark.parametrize("body", [b"{broken", b"[]", b'{"nope": 1}', b'{"url": ""}', b'{"url": "   "}', b'{"url": 5}', b'{"url": null}'])
def test_add_without_a_link_is_a_400_with_a_plain_message(helper, body):
    helper.add_processor = lambda url: pytest.fail("no link, no work")
    status, _, answer = call(helper, "POST", "/add", body=body)
    assert status == 400 and json.loads(answer)["code"] == "invalid_url" and json.loads(answer)["error"]


def test_a_page_that_cannot_be_read_is_reported_and_nothing_is_saved(helper):
    from dailyread.add_link import AddLinkError

    def refuse(url):
        raise AddLinkError("member_only", "This is a member-only story: it needs a login. Nothing was added.", 422)
    helper.add_processor = refuse
    status, _, answer = call(helper, "POST", "/add", body={"url": "https://medium.com/x"})
    assert status == 422 and json.loads(answer) == {"code": "member_only", "error": "This is a member-only story: it needs a login. Nothing was added."}
    assert not helper.path.exists()
    helper.add_processor = lambda url: _new_record()
    assert call(helper, "POST", "/add", body={"url": "https://medium.com/x"})[0] == 200          # and the next link still works


def test_an_unexpected_crash_is_a_generic_500_that_leaks_nothing(helper):
    def crash(url):
        raise RuntimeError("secret /Users/someone/path and token abc")
    helper.add_processor = crash
    status, _, answer = call(helper, "POST", "/add", body={"url": "https://x.dev/p"})
    assert status == 500 and b"secret" not in answer and b"/Users" not in answer and json.loads(answer)["code"] == "failed"
    assert call(helper, "GET")[0] == 200                                   # the helper itself is fine


def test_only_one_link_is_read_at_a_time(helper):
    gate = threading.Event()
    helper.add_processor = lambda url: (gate.wait(10), _new_record())[1]
    first = []
    t = threading.Thread(target=lambda: first.append(call(helper, "POST", "/add", body={"url": "https://x.dev/a"})[0]))
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and helper.add_gate._value != 0:
        time.sleep(0.02)
    status, _, answer = call(helper, "POST", "/add", body={"url": "https://x.dev/b"})
    assert status == 429 and json.loads(answer)["code"] == "busy"
    gate.set()
    t.join(10)
    assert first == [200] and call(helper, "POST", "/add", body={"url": "https://x.dev/c"})[0] == 200      # released afterwards


def test_the_helper_stays_up_while_a_page_is_being_read_and_other_clicks_still_work(helper):
    seen = {}

    def slow(url):
        seen["active"] = helper.active
        seen["other"] = call(helper, "POST", "/library", body=doc(rec("clicked")))[0]      # a click during the (long) read
        return _new_record()
    helper.add_processor = slow
    assert call(helper, "POST", "/add", body={"url": "https://x.dev/p"})[0] == 200
    assert seen["active"] >= 1 and seen["other"] == 200
    assert {"clicked"} <= set(json.loads(helper.path.read_text())["items"])


def test_add_bodies_are_small(helper):
    assert call(helper, "POST", "/add", body=b"{}", raw_length=library_helper.MAX_ADD_BODY + 1)[0] == 413
    assert call(helper, "POST", "/add", body=b'{"url": "' + b"a" * 20_000 + b'"}')[0] == 413


def test_the_reader_is_only_loaded_when_a_link_is_added():
    """The helper must stay small: httpx, trafilatura and the Claude runner are imported lazily, on the first /add."""
    import subprocess, sys
    code = ("import sys; import dailyread.agent_main, dailyread.library_helper; "
            "print([m for m in ('dailyread.add_link', 'httpx', 'trafilatura', 'bs4', 'dailyread.llm') if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))
    assert out.stdout.strip() == "[]", out.stdout + out.stderr
