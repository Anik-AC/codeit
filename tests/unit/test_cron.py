from __future__ import annotations

from datetime import datetime

import pytest

from codeit.cron import Cron, CronError


def test_weekly_sunday() -> None:
    c = Cron("0 22 * * SUN")
    assert c.matches(datetime(2026, 10, 4, 22, 0))  # a Sunday
    assert not c.matches(datetime(2026, 10, 4, 22, 1))
    assert not c.matches(datetime(2026, 10, 5, 22, 0))
    assert Cron("0 22 * * 7").matches(datetime(2026, 10, 4, 22, 0))
    assert Cron("0 22 * * 0").matches(datetime(2026, 10, 4, 22, 0))


def test_steps_ranges_lists() -> None:
    c = Cron("*/15 9-17 * * MON-FRI")
    assert c.matches(datetime(2026, 9, 30, 9, 45))  # Wednesday
    assert not c.matches(datetime(2026, 9, 30, 9, 50))
    assert not c.matches(datetime(2026, 10, 3, 10, 0))  # Saturday
    assert Cron("5 1,13 * * *").matches(datetime(2026, 9, 30, 13, 5))


def test_day_or_weekday_when_both_given() -> None:
    c = Cron("0 0 1 * MON")
    assert c.matches(datetime(2026, 10, 1, 0, 0))  # the 1st, a Thursday
    assert c.matches(datetime(2026, 10, 5, 0, 0))  # a Monday


def test_fired_between() -> None:
    c = Cron("0 22 * * SUN")
    assert c.fired_between(datetime(2026, 10, 4, 21, 59), datetime(2026, 10, 4, 22, 0))
    assert not c.fired_between(datetime(2026, 10, 4, 22, 0), datetime(2026, 10, 4, 23, 0))
    assert c.fired_between(datetime(2026, 9, 1), datetime(2026, 10, 5))  # scan is capped
    assert not c.fired_between(datetime(2026, 10, 5), datetime(2026, 10, 10))


@pytest.mark.parametrize("bad", ["0 22 * *", "61 * * * *", "0 22 * * FUNDAY", "*/0 * * * *"])
def test_bad(bad: str) -> None:
    with pytest.raises(CronError):
        Cron(bad)
