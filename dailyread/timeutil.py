"""Time-window helpers. All comparisons happen on timezone-aware datetimes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    tz: ZoneInfo
    # Date-only sources (release notes) are labelled in US time and sometimes backdated. Real runs look back a few
    # days and rely on seen.json to drop repeats; dry runs use 0 so they show exactly the window.
    day_lookback: int = 0

    def contains(self, dt: datetime | None) -> bool:
        return dt is not None and self.start <= dt < self.end

    def contains_day(self, d: date) -> bool:
        """For date-only sources (release notes): include every label date the window touches."""
        first = self.start.astimezone(self.tz).date() - timedelta(days=self.day_lookback)
        return first <= d <= self.end.astimezone(self.tz).date()

    @property
    def days(self) -> float:
        return max((self.end - self.start).total_seconds() / 86400, 1 / 24)

    def label(self) -> str:
        s, e = self.start.astimezone(self.tz), self.end.astimezone(self.tz)
        return f"{s:%a %-d %b %H:%M} → {e:%a %-d %b %H:%M}"


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_rfc822(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return to_utc(parsedate_to_datetime(s.strip()))
    except (TypeError, ValueError):
        return None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return to_utc(datetime.fromisoformat(s.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_any(s: str | None) -> datetime | None:
    return parse_iso(s) or parse_rfc822(s)


def parse_human_date(s: str) -> date | None:
    """'October 01, 2026' / 'Oct 1, 2026' / 'September 30, 2026' -> date."""
    s = s.strip().replace("Sept ", "Sep ")
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def yesterday_midnight(tz: ZoneInfo, now: datetime | None = None) -> datetime:
    now = (now or datetime.now(tz)).astimezone(tz)
    return datetime.combine(now.date() - timedelta(days=1), datetime.min.time(), tzinfo=tz)


def local_day(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()
