"""Persistent state: control (state.json), per-source health (sources.json), dedup (seen.json),
history (runs.jsonl) and a run lock. Real runs use state/, dry runs use state-dryrun/."""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import SourceStats


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False))
    tmp.replace(path)     # atomic on the same filesystem


class LockedError(Exception):
    pass


class StateStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.state_path = directory / "state.json"
        self.sources_path = directory / "sources.json"
        self.seen_path = directory / "seen.json"
        self.runs_path = directory / "runs.jsonl"
        self.lock_path = directory / "run.lock"

    # --- control -------------------------------------------------------------------------------------------
    def state(self) -> dict:
        return _read_json(self.state_path, {})

    def save_state(self, data: dict) -> None:
        _write_json(self.state_path, data)

    def record_failure(self, today: str, error: str) -> None:
        """Failed real run: count attempts per day (Phase 2 notifies after 3) without advancing the window."""
        st = self.state()
        st["failed_attempts_today"] = (st.get("failed_attempts_today", 0) + 1) if st.get("last_failure_date") == today else 1
        st["last_failure_date"] = today
        st["last_error"] = error
        _write_json(self.state_path, st)

    # --- per-source health -----------------------------------------------------------------------------------
    def sources(self) -> dict:
        return _read_json(self.sources_path, {})

    def update_sources(self, stats: dict[str, SourceStats], fetched_at: datetime, window, advance: bool = True) -> None:
        """Merge this run's stats. `covered_until` only advances for sources that fetched completely (ok/empty), so
        a source that failed or was partial today is fetched from its own last good point next time.
        advance=False (failed real run): record health only; nothing was reported, so coverage must not move."""
        data = self.sources()
        for key, st in stats.items():
            prev = data.get(key, {})
            ok = st.status in ("ok", "empty")
            entry = {
                **prev,
                **{k: v for k, v in asdict(st).items() if k not in ("latest_item_date", "latest_item_title")},
                "last_fetch_at": fetched_at.isoformat(timespec="seconds"),
                "consecutive_failures": 0 if ok else prev.get("consecutive_failures", 0) + 1,
                "last_error": st.error if not ok else None,
            }
            entry.pop("error", None)
            if ok and advance:
                entry["covered_until"] = window.end.isoformat(timespec="seconds")
                entry["last_success_at"] = fetched_at.isoformat(timespec="seconds")
            else:   # first-ever run failed: remember where this source still has to start from
                entry.setdefault("covered_until", window.start.isoformat(timespec="seconds"))
            if st.latest_item_date:     # keep the last known item even on empty days
                entry["latest_item_date"] = st.latest_item_date
                entry["latest_item_title"] = st.latest_item_title
            data[key] = entry
        _write_json(self.sources_path, data)

    # --- dedup -----------------------------------------------------------------------------------------------
    def seen(self) -> dict[str, str]:
        """item id -> report date (YYYY-MM-DD) in which it was first reported."""
        return _read_json(self.seen_path, {})

    def add_seen(self, ids: list[str], report_date: str, keep_days: int = 60) -> None:
        """Record ids; forget entries older than keep_days (no feed reaches back that far)."""
        data = self.seen()
        for i in ids:
            data.setdefault(i, report_date)
        cutoff = (date.fromisoformat(report_date) - timedelta(days=keep_days)).isoformat()
        _write_json(self.seen_path, {k: v for k, v in data.items() if v >= cutoff})

    # --- history ---------------------------------------------------------------------------------------------
    def append_run(self, record: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.runs_path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def runs(self) -> list[dict]:
        try:
            lines = self.runs_path.read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # --- lock ------------------------------------------------------------------------------------------------
    @contextmanager
    def lock(self):
        """Directory lock (mkdir is atomic) holding the owner's PID. A lock is only broken when its owner process
        is gone, and only the owner removes it."""
        self.dir.mkdir(parents=True, exist_ok=True)
        pid_file = self.lock_path / "pid"
        for _ in range(3):
            try:
                os.mkdir(self.lock_path)
                pid_file.write_text(str(os.getpid()))
                break
            except FileExistsError:
                try:
                    owner = int(pid_file.read_text())
                except (FileNotFoundError, ValueError):
                    owner = None
                if owner is None:
                    # being created/removed right now, or left without a pid: give it a moment
                    time.sleep(0.5)
                    if not pid_file.exists() and self.lock_path.exists() and time.time() - self.lock_path.stat().st_mtime > 60:
                        shutil.rmtree(self.lock_path, ignore_errors=True)
                    continue
                if _alive(owner):
                    raise LockedError(f"another run (pid {owner}) is in progress")
                shutil.rmtree(self.lock_path, ignore_errors=True)     # owner died: stale lock
        else:
            raise LockedError(f"could not acquire {self.lock_path}")
        try:
            yield
        finally:
            try:
                if int(pid_file.read_text()) == os.getpid():
                    shutil.rmtree(self.lock_path, ignore_errors=True)
            except (FileNotFoundError, ValueError):
                pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
