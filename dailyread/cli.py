"""Command line: `bin/dailyread <command>`.

  mockup                       render the design with sample data (no network, no Claude)
  run --dry-run [--since ...]  full pipeline, never touches real state (Phase 1 default)
  run                          real run: window from state, archives previous report, advances state
  render <data.json>           re-render a saved report JSON (iterate on design without Claude calls)
  usage [--dry-run]            print token usage history
  schedule install|uninstall|status   daily automation via launchd (Phase 2)
  scheduled                    one launchd tick: runs the real pipeline if today's report is due
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import load_config
from .pipeline import RunFailed, RunOptions, open_in_browser, paths, run
from .render import render_index, render_report
from .state import LockedError, StateStore


def _setup_logging(cfg, verbose: bool) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(cfg.logs_dir / f"{datetime.now():%Y-%m-%d}.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO if verbose else logging.WARNING)
    root.handlers = [fh, sh]
    for noisy in ("httpx", "httpcore", "trafilatura", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dailyread", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the pipeline")
    r.add_argument("--dry-run", action="store_true", help="don't touch real state; window = yesterday 00:00 → now")
    r.add_argument("--since", help="dry-run window start, e.g. 2026-10-01T00:00 (local time)")
    r.add_argument("--only", help="comma-separated section keys to include (testing)")
    r.add_argument("--no-open", action="store_true", help="don't open the report in the browser")
    r.add_argument("-v", "--verbose", action="store_true")
    m = sub.add_parser("mockup", help="render the design with sample data")
    m.add_argument("--no-open", action="store_true")
    rr = sub.add_parser("render", help="re-render a saved report JSON")
    rr.add_argument("data", type=Path)
    rr.add_argument("--no-open", action="store_true")
    u = sub.add_parser("usage", help="print token usage history")
    u.add_argument("--dry-run", action="store_true")
    sc = sub.add_parser("schedule", help="daily automation via launchd")
    sc.add_argument("action", choices=["install", "uninstall", "status"])
    sub.add_parser("scheduled", help="one launchd tick (used by the LaunchAgent)")
    args = ap.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg, getattr(args, "verbose", False))

    if args.cmd == "mockup":
        report = json.loads((cfg.root / "fixtures" / "sample_report.json").read_text())
        report["generated_at"] = datetime.now(cfg.tz).isoformat()
        out = cfg.root / "reports" / "mockup.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_report(cfg, report))
        print(out)
        if not args.no_open:
            open_in_browser(cfg, out)
        return 0

    if args.cmd == "render":
        report = json.loads(args.data.read_text())
        dry = report.get("mode") != "real"
        p = paths(cfg, dry)
        name = args.data.stem + ".html"
        archived = p["archive"] / name[:4] / name
        out = archived if not dry and archived.exists() else p["reports"] / name
        out.parent.mkdir(parents=True, exist_ok=True)
        index = p["reports"] / "index.html"
        out.write_text(render_report(cfg, report, index_href=index.as_uri()))
        index.write_text(render_index(cfg, p["reports"], StateStore(p["state"]), dry))
        print(out)
        if not args.no_open:
            open_in_browser(cfg, out)
        return 0

    if args.cmd == "schedule":
        from . import schedule
        try:
            print({"install": schedule.install, "uninstall": schedule.uninstall, "status": schedule.status}[args.action](cfg))
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "scheduled":
        from . import schedule
        return schedule.tick(cfg)

    if args.cmd == "usage":
        store = StateStore(paths(cfg, args.dry_run)["state"])
        print(f"{'started':17} {'ok':3} {'days':>5} {'items':>5} {'read':>4}  {'model':7} {'calls':>5} {'in':>9} {'out':>7} {'≈$':>7} {'secs':>6}")
        for rec in store.runs():
            first = True
            for model, u in rec.get("usage", {}).get("by_model", {}).items():
                head = (f"{rec['started_at'][:16]:17} {'✓' if rec['ok'] else '✗':3} {rec['window_days']:>5} "
                        f"{rec['totals']['items']:>5} {rec['totals']['read']:>4}") if first else " " * 40
                inp = u["input_tokens"] + u["cache_read_tokens"] + u["cache_creation_tokens"]
                print(f"{head}  {model:7} {u['calls']:>5} {inp:>9,} {u['output_tokens']:>7,} {u['cost_usd']:>7.3f} "
                      f"{rec.get('duration_s', 0) if first else '':>6}")
                first = False
        return 0

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        since = since if since.tzinfo else since.replace(tzinfo=cfg.tz)
    opts = RunOptions(dry_run=args.dry_run, since=since, open_browser=not args.no_open,
                      only=set(args.only.split(",")) if args.only else None)
    try:
        result = run(cfg, opts)
    except LockedError as e:
        print(f"skipped: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RunFailed as e:
        print(f"run failed (state not advanced): {e}", file=sys.stderr)
        return 2
    t = result.record["totals"]
    print(f"{result.report_path}\n{t['items']} items, {t['read']} ✅, {result.record['duration_s']}s"
          + (f"\nissues: {'; '.join(result.errors)}" if result.errors else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
