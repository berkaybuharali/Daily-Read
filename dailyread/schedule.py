"""Phase 2: daily automation with launchd (macOS's built-in scheduler).

A LaunchAgent runs `bin/dailyread agent` at login and every `interval_minutes` (a tick missed during sleep fires
once on wake). Each tick is cheap: it exits at once unless today's report is still missing and it's past
`not_before`. The same job also holds a localhost port (launchd socket activation): when a report page connects to save
Read Later / Favorites, launchd starts the agent, which then serves library_helper until it has been idle a few minutes
and exits. Nothing is running between those moments. A failed run doesn't advance state, so the next tick retries; after `max_failures_per_day` it shows a
macOS notification and waits for tomorrow. Usage-limit failures and being offline don't count as attempts."""
from __future__ import annotations

import logging
import os
import plistlib
import socket
import subprocess
import sys
import threading
import time as _time
from datetime import datetime, time
from pathlib import Path

from . import library, library_helper
from .config import Config
from .llm import USAGE_LIMIT
from .pipeline import RunFailed, RunOptions, paths, run
from .state import LockedError, StateStore

log = logging.getLogger(__name__)

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
DEFAULTS = {"not_before": "07:00", "interval_minutes": 30, "max_failures_per_day": 3, "label": "com.dailyread.agent"}


def settings(cfg: Config) -> dict:
    return {**DEFAULTS, **(cfg.raw.get("schedule") or {})}


def skip_reason(state: dict, now: datetime, not_before: time, max_failures: int) -> str | None:
    """Why this tick should not run (None = run now)."""
    today = now.date().isoformat()
    if state.get("last_report_date") == today:
        return "today's report already exists"
    if now.time() < not_before:
        return f"before {not_before:%H:%M}"
    if state.get("last_failure_date") == today and state.get("failed_attempts_today", 0) >= max_failures:
        return f"gave up for today after {state['failed_attempts_today']} failed attempts"
    return None


def online(host: str = "github.com", port: int = 443, timeout: float = 5) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def notify(title: str, message: str) -> None:
    script = f'display notification {_as_str(message)} with title {_as_str(title)}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def _as_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def tick(cfg: Config, now: datetime | None = None) -> int:
    """One launchd tick. Always exits 0 unless something unexpected crashed (launchd just logs it)."""
    s = settings(cfg)
    now = (now or datetime.now(cfg.tz)).astimezone(cfg.tz)
    store = StateStore(paths(cfg, dry_run=False)["state"])
    reason = skip_reason(store.state(), now, time.fromisoformat(s["not_before"]), s["max_failures_per_day"])
    if reason:
        if not reason.startswith("today's report"):
            log.info("scheduler: skip (%s)", reason)
        return 0
    if not online():
        log.info("scheduler: offline, will retry next tick")
        return 0
    log.info("scheduler: starting the daily run")
    try:
        result = run(cfg, RunOptions(dry_run=False))
    except LockedError as e:
        log.info("scheduler: %s", e)
        return 0
    except RunFailed as e:
        st = store.state()
        if USAGE_LIMIT.search(str(e)):          # resets within hours: keep retrying, don't burn an attempt
            st["failed_attempts_today"] = max(0, st.get("failed_attempts_today", 1) - 1)
            store.save_state(st)
            log.warning("scheduler: Claude usage limit reached, will retry next tick")
            return 0
        n = st.get("failed_attempts_today", 0)
        log.error("scheduler: run failed (%d/%d): %s", n, s["max_failures_per_day"], e)
        if n >= s["max_failures_per_day"]:
            notify("Daily Read failed", f"{n} attempts failed today; trying again tomorrow. See logs/{now:%Y-%m-%d}.log")
        return 0
    t = result.record["totals"]
    log.info("scheduler: done: %s items, %s must-reads", t["items"], t["read"])
    return 0


# ---- LaunchAgent install / uninstall / status --------------------------------------------------------------------
def plist_path(cfg: Config) -> Path:
    return AGENTS_DIR / f"{settings(cfg)['label']}.plist"


def calendar_checks(s: dict) -> list[dict]:
    hour, minute = (int(x) for x in str(s["not_before"]).split(":")[:2])
    minute += 5
    return [{"Hour": (hour + minute // 60) % 24, "Minute": minute % 60}, {"Hour": 12, "Minute": 0}]


def agent_python(cfg: Config) -> str:
    """The interpreter launchd runs: the project's venv Python started DIRECTLY. Going through `uv run` would make Python
    a child of uv, and launchd only hands its listening socket to the job's own process."""
    venv = cfg.root / ".venv" / "bin" / "python3"
    return str(venv if venv.exists() else sys.executable)


def build_plist(cfg: Config, env: dict[str, str] | None = None, helper: bool = True) -> dict:
    env = os.environ if env is None else env
    s = settings(cfg)
    home = Path.home()
    variables = {"PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if env.get("CLAUDE_CONFIG_DIR"):        # use the same Claude login as the shell that installed the job
        variables["CLAUDE_CONFIG_DIR"] = env["CLAUDE_CONFIG_DIR"]
    plist = {
        "Label": s["label"],
        "ProgramArguments": [agent_python(cfg), "-m", "dailyread.agent_main"],
        "WorkingDirectory": str(cfg.root),
        "EnvironmentVariables": variables,
        "RunAtLoad": True,
        "StartInterval": int(s["interval_minutes"]) * 60,
        "ThrottleInterval": 5,        # launchd's default is 10 s; 5 s also caps a respawn loop if the job ever cannot start

        # StartInterval ticks that fall while the Mac sleeps are missed (man launchd.plist), so also ask launchd for a
        # check shortly after the report time and at noon: calendar intervals missed during sleep fire once on wake.
        "StartCalendarInterval": calendar_checks(s),
        "ProcessType": "Background",     # lower CPU/IO priority: the daily run and the helper must never slow down your work
        "LowPriorityIO": True,
        "StandardOutPath": str(cfg.logs_dir / "launchd.log"),
        "StandardErrorPath": str(cfg.logs_dir / "launchd.log"),
    }
    if helper:       # Read Later / Favorites helper: launchd listens here and starts the agent on the first connection
        plist["Sockets"] = {"Listener": {"SockNodeName": "127.0.0.1", "SockServiceName": str(library.helper_settings(cfg)["port"])}}
    return plist


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _load(cfg: Config, data: dict) -> None:
    path = plist_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{data['Label']}"], capture_output=True)
    with path.open("wb") as f:
        plistlib.dump(data, f)
    r = subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.returncode}")


def helper_selftest(cfg: Config, timeout: float = 20) -> tuple[bool, str]:
    """Ask the helper for the library like a report page does. This also proves launchd starts it on demand."""
    import time
    import urllib.request
    info = library.helper_config(cfg)
    if not info:
        return False, "no helper token"
    req = urllib.request.Request(info["url"] + "/library", headers={"X-DailyRead-Token": info["token"], "Origin": "null"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200, f"answered in {time.monotonic() - t0:.1f}s"
    except OSError as e:
        return False, str(e)


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def install(cfg: Config) -> str:
    store = StateStore(paths(cfg, dry_run=False)["state"])
    try:
        with store.lock():
            pass
    except LockedError:                # reinstalling stops the job: that would kill a report that is being generated
        raise RuntimeError("a report is being generated right now: run `bin/dailyread schedule install` again in a few minutes")
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    port = library.helper_settings(cfg)["port"]
    job_running = subprocess.run(["launchctl", "print", f"{_domain()}/{settings(cfg)['label']}"], capture_output=True).returncode == 0
    if not job_running and not port_free(port):          # (when our own job holds the port it is not "taken")
        _load(cfg, build_plist(cfg, helper=False))
        return (f"installed {plist_path(cfg)}\nWARNING: port {port} is used by another program, so the Read Later helper was NOT "
                f"enabled. Set `library: port:` in config.yaml to a free port and run this command again.")
    library.ensure_token(library.token_path(cfg))      # the secret reports use to talk to the helper
    data = build_plist(cfg)
    try:
        _load(cfg, data)
    except RuntimeError:               # e.g. launchd could not bind the port: keep the daily schedule, skip the helper
        _load(cfg, build_plist(cfg, helper=False))
        library.token_path(cfg).unlink(missing_ok=True)
        return f"installed {plist_path(cfg)}\nWARNING: launchd could not open port {port}; the Read Later helper was NOT enabled."
    claude = data["EnvironmentVariables"].get("CLAUDE_CONFIG_DIR", "~/.claude (default)")
    hs = library.helper_settings(cfg)
    ok, detail = helper_selftest(cfg)
    if ok:
        helper = (f"Read Later / Favorites auto-save: self-test passed ({detail}). launchd listens on 127.0.0.1:{hs['port']} "
                  f"and starts the agent on the first click; it exits after {hs['helper_idle_minutes']} idle minutes.\n"
                  "Reports rendered from now on can reach it (re-render older ones with `bin/dailyread render --all`).")
    else:
        _load(cfg, build_plist(cfg, helper=False))      # never leave a socket that nothing answers (launchd would respawn)
        library.token_path(cfg).unlink(missing_ok=True)  # and no token: reports must not point at a helper that is not there
        helper = (f"WARNING: the Read Later helper failed its self-test ({detail}), so it was NOT enabled. The daily schedule is "
                  "installed; lists still work in the browser (reminder bar, Export). See logs/launchd.log.")
    return f"installed {plist_path(cfg)}\nClaude login used: {claude}\n{helper}"


def uninstall(cfg: Config) -> str:
    path = plist_path(cfg)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{settings(cfg)['label']}"], capture_output=True)
    library.token_path(cfg).unlink(missing_ok=True)       # reports rendered from now on carry no helper; state/library.json stays
    if path.exists():
        path.unlink()
        return f"removed {path} (your lists in state/library.json are kept)"
    return "not installed"


def _helper_running() -> bool:
    out = subprocess.run(["pgrep", "-f", "dailyread.agent_main"], capture_output=True, text=True).stdout.split()
    return bool(out)


_AUTO = object()


def run_agent(cfg: Config, sock=_AUTO) -> int:
    """Entry point of the LaunchAgent. launchd starts it for one of two reasons:
    - a click: a report page is connecting, so serve the Read Later helper until it has been idle a few minutes;
    - a timer tick: run the daily check. While it runs (a report takes minutes) the helper serves in a background
      thread, so a click never waits for the pipeline; afterwards the process exits at once unless it was used."""
    if sock is _AUTO:
        sock = library_helper.inherited_socket()
    token = library.read_token(library.token_path(cfg))
    if sock is None or token is None:
        return tick(cfg)
    server = library_helper.make_server(library.library_path(cfg), token,
                                        library.helper_settings(cfg)["helper_idle_minutes"], sock=sock)
    if library_helper.connection_pending(sock):
        server.run()
        return 0
    server.hold = True
    thread = threading.Thread(target=server.run, name="library-helper", daemon=True)
    thread.start()
    try:
        return tick(cfg)
    finally:
        server.hold = False
        if server.requests == 0 and not library_helper.connection_pending(sock):
            server.stop()
        else:
            server.last_activity = _time.monotonic()      # someone clicked meanwhile: stay up for the idle time
        thread.join()


def plist_problems(cfg: Config) -> list[str]:
    """Why the installed job would not work (project moved, venv rebuilt) or lacks the helper (installed before it existed)."""
    path = plist_path(cfg)
    try:
        with path.open("rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return []
    problems = []
    program = (data.get("ProgramArguments") or [""])[0]
    if not Path(program).exists():
        problems.append(f"the Python it starts does not exist ({program})")
    if data.get("WorkingDirectory") != str(cfg.root):
        problems.append(f"it points at another project folder ({data.get('WorkingDirectory')})")
    if "Sockets" not in data:
        problems.append("it was installed without the Read Later helper")
    return problems


def launchd_counts(label: str) -> str:
    out = subprocess.run(["launchctl", "print", f"{_domain()}/{label}"], capture_output=True, text=True).stdout
    found = {k: v.strip() for k, _, v in (line.strip().partition(" = ") for line in out.splitlines()) if k in ("runs", "last exit code", "state")}
    return ", ".join(f"{k}: {v}" for k, v in found.items())


def status(cfg: Config) -> str:
    s = settings(cfg)
    loaded = subprocess.run(["launchctl", "print", f"{_domain()}/{s['label']}"], capture_output=True).returncode == 0
    st = StateStore(paths(cfg, dry_run=False)["state"]).state()
    now = datetime.now(cfg.tz)
    reason = skip_reason(st, now, time.fromisoformat(s["not_before"]), s["max_failures_per_day"])
    problems = plist_problems(cfg)
    lines = [
        f"launchd job: {'loaded' if loaded else 'not loaded'} ({plist_path(cfg)})" + (f" [{launchd_counts(s['label'])}]" if loaded else ""),
        *([f"PROBLEM: {p}. Run `bin/dailyread schedule install` again." for p in problems]),
        f"schedule: first tick after {s['not_before']}, checks every {s['interval_minutes']} min, "
        f"max {s['max_failures_per_day']} failures/day",
        f"last report: {st.get('last_report_date', 'none')} (window end {st.get('last_window_end', '-')})",
        f"failures today: {st.get('failed_attempts_today', 0) if st.get('last_failure_date') == now.date().isoformat() else 0}"
        + (f" (last error: {st['last_error']})" if st.get("last_error") else ""),
        f"next tick would: {'skip: ' + reason if reason else 'RUN'}",
        f"Read Later helper: 127.0.0.1:{library.helper_settings(cfg)['port']}, exits after "
        f"{library.helper_settings(cfg)['helper_idle_minutes']} idle min; token {'present' if library.helper_config(cfg) else 'missing (run schedule install)'}; "
        f"{'serving now' if _helper_running() else 'not running (starts on the first click)'}",
    ]
    return "\n".join(lines)
