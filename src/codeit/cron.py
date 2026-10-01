"""Five-field cron expressions (`minute hour day month weekday`), enough for CodeIt's
schedules: `*`, `*/n`, `a-b`, `a-b/n`, lists, and weekday names (SUN to SAT; 0 and 7 are
Sunday). As in classic cron, when both day and weekday are restricted, either may match."""

from __future__ import annotations

from datetime import datetime, timedelta

_NAMES = {n: i for i, n in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
MAX_SCAN = timedelta(days=8)


class CronError(ValueError):
    pass


def _value(text: str, field: int) -> int:
    text = text.upper()
    if field == 4 and text in _NAMES:
        return _NAMES[text]
    if not text.isdigit():
        raise CronError(f"bad cron value {text!r}")
    value = int(text)
    low, high = _RANGES[field]
    if not low <= value <= high:
        raise CronError(f"cron value {value} out of range {low}-{high}")
    return value


def _field(text: str, field: int) -> set[int]:
    low, high = _RANGES[field]
    out: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"bad cron step in {part!r}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = _value(a, field), _value(b, field)
        else:
            start = _value(base, field)
            end = high if step_text else start
        out.update(range(start, end + 1, step))
    if field == 4 and 7 in out:
        out = (out - {7}) | {0}
    return out


class Cron:
    def __init__(self, expr: str) -> None:
        parts = expr.split()
        if len(parts) != 5:
            raise CronError(f"cron {expr!r} must have 5 fields")
        self.minutes, self.hours, self.days, self.months, self.weekdays = (
            _field(p, i) for i, p in enumerate(parts)
        )
        self.any_day = parts[2] == "*"
        self.any_weekday = parts[4] == "*"

    def matches(self, t: datetime) -> bool:
        if t.minute not in self.minutes or t.hour not in self.hours or t.month not in self.months:
            return False
        weekday = (t.weekday() + 1) % 7  # Python: Monday 0; cron: Sunday 0
        day_ok, weekday_ok = t.day in self.days, weekday in self.weekdays
        if self.any_day or self.any_weekday:
            return day_ok and weekday_ok
        return day_ok or weekday_ok

    def fired_between(self, after: datetime, until: datetime) -> bool:
        """Did a scheduled minute pass in (after, until]? Times are compared in the zone
        they carry (pass local times). Scans at most `MAX_SCAN` back from `until`."""
        start = max(after, until - MAX_SCAN).replace(second=0, microsecond=0)
        t = start + timedelta(minutes=1)
        while t <= until:
            if self.matches(t):
                return True
            t += timedelta(minutes=1)
        return False
