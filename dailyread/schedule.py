"""Phase 2: daily automation with launchd (macOS's built-in scheduler).

A LaunchAgent runs `bin/dailyread scheduled` at login and every `interval_minutes` (a tick missed during sleep fires
once on wake). Each tick is cheap: it exits at once unless today's report is still missing and it's past
`not_before`. A failed run doesn't advance state, so the next tick retries; after `max_failures_per_day` it shows a
macOS notification and waits for tomorrow. Usage-limit failures and being offline don't count as attempts."""
from __future__ import annotations

import logging
import os
import plistlib
import socket
import subprocess
from datetime import datetime, time
from pathlib import Path

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


def build_plist(cfg: Config, env: dict[str, str] | None = None) -> dict:
    env = os.environ if env is None else env
    s = settings(cfg)
    home = Path.home()
    variables = {"PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    if env.get("CLAUDE_CONFIG_DIR"):        # use the same Claude login as the shell that installed the job
        variables["CLAUDE_CONFIG_DIR"] = env["CLAUDE_CONFIG_DIR"]
    return {
        "Label": s["label"],
        "ProgramArguments": [str(cfg.root / "bin" / "dailyread"), "scheduled"],
        "WorkingDirectory": str(cfg.root),
        "EnvironmentVariables": variables,
        "RunAtLoad": True,
        "StartInterval": int(s["interval_minutes"]) * 60,
        "StandardOutPath": str(cfg.logs_dir / "launchd.log"),
        "StandardErrorPath": str(cfg.logs_dir / "launchd.log"),
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(cfg: Config) -> str:
    data = build_plist(cfg)
    path = plist_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{data['Label']}"], capture_output=True)
    with path.open("wb") as f:
        plistlib.dump(data, f)
    r = subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.returncode}")
    claude = data["EnvironmentVariables"].get("CLAUDE_CONFIG_DIR", "~/.claude (default)")
    return f"installed {path}\nClaude login used: {claude}"


def uninstall(cfg: Config) -> str:
    path = plist_path(cfg)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{settings(cfg)['label']}"], capture_output=True)
    if path.exists():
        path.unlink()
        return f"removed {path}"
    return "not installed"


def status(cfg: Config) -> str:
    s = settings(cfg)
    loaded = subprocess.run(["launchctl", "print", f"{_domain()}/{s['label']}"], capture_output=True).returncode == 0
    st = StateStore(paths(cfg, dry_run=False)["state"]).state()
    now = datetime.now(cfg.tz)
    reason = skip_reason(st, now, time.fromisoformat(s["not_before"]), s["max_failures_per_day"])
    lines = [
        f"launchd job: {'loaded' if loaded else 'not loaded'} ({plist_path(cfg)})",
        f"schedule: first tick after {s['not_before']}, checks every {s['interval_minutes']} min, "
        f"max {s['max_failures_per_day']} failures/day",
        f"last report: {st.get('last_report_date', 'none')} (window end {st.get('last_window_end', '-')})",
        f"failures today: {st.get('failed_attempts_today', 0) if st.get('last_failure_date') == now.date().isoformat() else 0}"
        + (f" (last error: {st['last_error']})" if st.get("last_error") else ""),
        f"next tick would: {'skip: ' + reason if reason else 'RUN'}",
    ]
    return "\n".join(lines)
