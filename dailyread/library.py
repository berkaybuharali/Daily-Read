"""Read Later / Favorites library.

The browser owns the lists (clicks save instantly in browser storage and, in Chrome, to a linked file). The file is
`state/library.json` (git-ignored). Python only READS it, to bake the saved lists into every report it renders so the
tabs show content before any script runs. Nothing here calls Claude.

File format (version 1):  {"version": 1, "updated_at": "<iso>", "items": {"<item id>": <record>}}
A record is one saved item with everything the list pages need (title, link, source, publish date, stars, verdict,
summary) plus flags `read_later`, `favorite`, `read`, `manual` (added by pasting a link: its date is the day it was added). A record with both list flags off is a tombstone that keeps
"removed" changes mergeable between browser and file. Keep this in sync with `templates/library.js` (cleanRecord)."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

VERSION = 1
EPOCH = "1970-01-01T00:00:00.000Z"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_ITEMS = 5000
MAX_FILE_BYTES = 20_000_000          # a real library is a few hundred KB; never parse something huge
KEEP_DAMAGED = 5
STEM = re.compile(r"^\d{4}-\d{2}-\d{2}(_\d{4})?$")      # report file stem: 2026-10-04 (real) or 2026-10-04_1149 (dry run)
PRUNE_DAYS = 180
CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def library_path(cfg: Config) -> Path:
    return cfg.root / "state" / "library.json"


def _text(v, limit: int) -> str:
    if not isinstance(v, str):
        return ""
    v = v.encode("utf-8", "ignore").decode()           # a lone surrogate (an emoji cut in half) cannot be saved as UTF-8
    v = re.sub(r"\s+", " ", v)
    return CONTROL.sub("", v).strip()[:limit]          # no control or bidi-override characters (they can disguise text)


def _stamp(v) -> str:
    """A valid ISO-8601 UTC timestamp, else EPOCH. A time in the future is clamped so a bad clock (or a hostile file)
    cannot pin a record that no later change could ever override."""
    if not isinstance(v, str) or not ISO.match(v):
        return EPOCH
    try:
        when = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return EPOCH
    limit = datetime.now(timezone.utc) + timedelta(minutes=5)
    return v if when <= limit else limit.strftime("%Y-%m-%dT%H:%M:%S.") + f"{limit.microsecond // 1000:03d}Z"


def _http_url(v) -> str:
    u = _text(v, 2000)
    return u if re.match(r"^https?://", u, re.I) else ""


def clean_record(raw) -> dict | None:
    """Validate one record from an untrusted file/snapshot. Returns None when it is unusable."""
    if not isinstance(raw, dict):
        return None
    rid, title = _text(raw.get("id"), 200), _text(raw.get("title"), 300)
    if not rid or not title:
        return None
    stars = raw.get("stars")
    return {
        "id": rid,
        "title": title,
        "url": _http_url(raw.get("url")),
        "source": _text(raw.get("source"), 80),
        "published": raw["published"] if isinstance(raw.get("published"), str) and DAY.match(raw["published"]) else "",
        "stars": int(stars) if isinstance(stars, (int, float)) and not isinstance(stars, bool) and float(stars).is_integer() and 0 <= stars <= 5 else 0,
        "verdict": raw["verdict"] if raw.get("verdict") in ("read", "skip") else "",
        "report": raw["report"] if isinstance(raw.get("report"), str) and STEM.match(raw["report"]) else "",
        "summary": _text(raw.get("summary"), 800),
        "reason": _text(raw.get("reason"), 300),
        "read_later": raw.get("read_later") is True,
        "favorite": raw.get("favorite") is True,
        "read": raw.get("read") is True,
        "manual": raw.get("manual") is True,          # added by pasting a link (its date is the day it was added)
        "added_at": _stamp(raw.get("added_at")),
        "updated_at": _stamp(raw.get("updated_at")),
    }


def load_library(path: Path) -> dict:
    """The saved lists as a clean document. A missing or damaged file is an empty library, never an error."""
    empty = {"version": VERSION, "updated_at": EPOCH, "items": {}}
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("file is far larger than any library")
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, RecursionError) as e:
        log.warning("library file %s unreadable (%s); starting with an empty library", path, e)
        return empty
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return empty
    out = {}
    for key, raw in list(items.items())[:MAX_ITEMS]:
        rec = clean_record(raw)
        if rec:
            out[rec["id"]] = rec
    return {"version": VERSION, "updated_at": _stamp(data.get("updated_at")), "items": out}


def seed_doc(doc: dict) -> dict:
    """What is baked into a report page: only items that are currently saved. Removal records (tombstones) stay in the
    file and the browser; a page that never had the item cannot need them, and this keeps every report small."""
    return {**doc, "items": {rid: r for rid, r in doc["items"].items() if r["read_later"] or r["favorite"]}}


def counts(doc: dict) -> dict:
    recs = doc["items"].values()
    return {"later": sum(r["read_later"] for r in recs), "favorites": sum(r["favorite"] for r in recs)}


def item_snapshots(sections: list[dict], shorts: dict[str, str], report: str = "") -> dict:
    """Everything the lists need about each report row, keyed by item id (embedded in the page; no LLM later).
    The source column is the writer's name for grouped blogs, otherwise the section's short name."""
    snaps = {}
    for s in sections:
        for i in s["items"]:
            stars = i.get("stars") or (4 if i.get("verdict") == "read" else 2)
            rec = clean_record({
                "id": i["id"], "title": i.get("title") or i.get("gen_title") or "(untitled)", "url": i.get("url"),
                "source": (i.get("extra") or {}).get("writer") or shorts.get(s["key"]) or s["title"],
                "published": i.get("day"), "stars": stars, "verdict": i.get("verdict"), "report": report,
                "summary": (i.get("summary") or "").replace("`", ""), "reason": i.get("reason"),
            })
            if rec:
                snaps[rec["id"]] = rec
    return snaps


def embed_json(obj) -> str:
    """JSON for an inline <script type="application/json">: no sequence can end the script or start a comment."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029")):
        s = s.replace(ch, esc)
    return s


def backup_library(path: Path, today: str, keep: int = 14) -> Path | None:
    """Copy state/library.json to state/library-backups/library-<date>.json (after a successful real run), keeping the
    newest `keep` copies. A safety net next to the other state files; a missing or damaged file is not backed up."""
    if not path.exists():
        return None
    try:
        json.loads(path.read_text())
    except (OSError, ValueError):
        log.warning("library file %s is damaged; not backing it up", path)
        return None
    dest_dir = path.parent / "library-backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"library-{today}.json"
    shutil.copy2(path, dest)
    for old in sorted(dest_dir.glob("library-*.json"))[:-keep]:
        old.unlink()
    return dest


# ---- the helper's side: merge, atomic save, token ---------------------------------------------------------------------
def merge_docs(a: dict, b: dict, now: datetime | None = None) -> dict:
    """Union of two library documents. Newest change per item wins (a tie goes to `b`); old tombstones are dropped.
    Mirrors mergeMaps/pruneMap in templates/library.js."""
    now = now or datetime.now(timezone.utc)
    merged: dict[str, dict] = {}
    for doc in (a, b):
        for rid, r in doc["items"].items():
            if rid not in merged or r["updated_at"] >= merged[rid]["updated_at"]:
                merged[rid] = r
    keep = {}
    for rid, r in merged.items():
        age = now - datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
        if r["read_later"] or r["favorite"] or age.total_seconds() <= PRUNE_DAYS * 86400:
            keep[rid] = r
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return {"version": VERSION, "updated_at": stamp, "items": keep}


def stamp_after(stamp: str) -> str:
    """The same time plus one millisecond, so a change is guaranteed to be newer than the record it replaces."""
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(milliseconds=1)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def clean_doc(data) -> dict | None:
    """A library document from untrusted JSON, or None when it is not one."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return None
    out = {}
    for raw in list(items.values())[:MAX_ITEMS]:
        rec = clean_record(raw)
        if rec:
            out[rec["id"]] = rec
    return {"version": VERSION, "updated_at": EPOCH, "items": out}


def save_doc(path: Path, doc: dict) -> None:
    """Atomic write: a crash or a second writer never leaves a half-written library."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".library-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def token_path(cfg: Config) -> Path:
    return cfg.root / "state" / "library.token"


def ensure_token(path: Path) -> str:
    """The secret reports use to talk to the helper (so a random web page cannot write to your library). Created with
    owner-only permissions from the first byte, refusing to follow a planted symlink."""
    existing = read_token(path)
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    token = secrets.token_urlsafe(24)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def read_token(path: Path) -> str | None:
    """The helper token if `schedule install` has created one. Never creates it: a token means "helper installed"."""
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def helper_settings(cfg: Config) -> dict:
    out = {"port": 47821, "helper_idle_minutes": 3, **(cfg.raw.get("library") or {})}
    override = os.environ.get("DAILYREAD_HELPER_IDLE_MINUTES")      # testing aid (see tests / README)
    if override:
        out["helper_idle_minutes"] = float(override)
    return out


def helper_config(cfg: Config) -> dict | None:
    """What the report page needs to reach the helper: only when the helper has been installed (token exists)."""
    path = token_path(cfg)
    if not path.exists() or not path.read_text().strip():
        return None
    return {"url": f"http://127.0.0.1:{helper_settings(cfg)['port']}", "token": path.read_text().strip()}


def find_report_stems(ids: set[str], data_dir: Path) -> dict[str, str]:
    """For saved items that predate the `report` field: the earliest report (by file stem) that contained each id."""
    found: dict[str, str] = {}
    for f in sorted(data_dir.glob("????-??-??*.json")):
        if not ids - found.keys():
            break
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for s in data.get("sections", []):
            for i in s.get("items", []):
                if i.get("id") in ids and i["id"] not in found:
                    found[i["id"]] = f.stem
    return found


def quarantine_if_damaged(path: Path) -> Path | None:
    """A library file that exists but cannot be read is moved aside (never silently overwritten by the next save)."""
    if not path.exists():
        return None
    try:
        ok = path.stat().st_size <= MAX_FILE_BYTES and isinstance(json.loads(path.read_text()).get("items"), dict)
    except (OSError, ValueError, AttributeError, RecursionError):
        ok = False
    if ok:
        return None
    dest = path.with_name(f"{path.stem}.damaged-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
    path.replace(dest)
    log.warning("library file %s was damaged; kept as %s", path, dest.name)
    for old in sorted(path.parent.glob(f"{path.stem}.damaged-*{path.suffix}"))[:-KEEP_DAMAGED]:
        old.unlink(missing_ok=True)
    return dest
