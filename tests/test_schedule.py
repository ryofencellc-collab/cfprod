import datetime as dt
import random
from zoneinfo import ZoneInfo

from core.autopilot.schedule import next_daily_slot, next_weekly_slot

CFG = {"shorts_per_day": 2, "tiktok_per_day": 1, "long_per_week": 2,
       "window_start": 9, "window_end": 21, "timezone": "America/New_York"}
NY = ZoneInfo("America/New_York")


def local(d):
    return d.replace(tzinfo=dt.timezone.utc).astimezone(NY)


def test_daily_slots_stay_in_window_and_respect_cap():
    now = dt.datetime(2026, 10, 3, 12, 0)  # 08:00 New York
    rnd = random.Random(1)
    taken = []
    for _ in range(6):
        slot = next_daily_slot(CFG, "youtube", "short", now=now, existing=taken, rnd=rnd)
        assert slot > now
        assert 9 <= local(slot).hour < 21
        taken.append(slot)
    per_day = {}
    for t in taken:
        per_day[local(t).date()] = per_day.get(local(t).date(), 0) + 1
    assert max(per_day.values()) <= 2
    assert len(per_day) == 3


def test_daily_slot_skips_past_times():
    now = dt.datetime(2026, 10, 4, 0, 30)  # 20:30 New York — window nearly over
    slot = next_daily_slot(CFG, "tiktok", "tiktok", now=now, existing=[], rnd=random.Random(2))
    assert slot >= now + dt.timedelta(minutes=10)
    assert 9 <= local(slot).hour < 21


def test_zero_cap_means_no_slot():
    cfg = dict(CFG, tiktok_per_day=0)
    assert next_daily_slot(cfg, "tiktok", "tiktok", existing=[]) is None


def test_weekly_spacing():
    now = dt.datetime(2026, 10, 3, 15, 0)
    first = next_weekly_slot(CFG, now=now, existing=[], rnd=random.Random(3))
    second = next_weekly_slot(CFG, now=now, existing=[first], rnd=random.Random(4))
    assert second - first >= dt.timedelta(days=3)
    assert 9 <= local(second).hour < 21
