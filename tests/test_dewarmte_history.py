import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app import heating_stats as H
from app.config import config
from app.db import get_db
from app.integrations import dewarmte as D


def _utc(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def test_label_is_closing_hour_and_gaps_are_skipped():
    # winter (CET = UTC+1): "08:00" = 07:00-08:00 lokaal = 06:00-07:00 UTC
    data = {"data": [{"key": "08:00", "heat_supplied": 4.0, "electricity_consumed": 1.0},
                     {"key": "10:00", "heat_supplied": "3.5", "electricity_consumed": "0.9", "electric_backup_usage": 0}]}
    rows = D.parse_insights(date(2026, 11, 4), data)
    assert rows == [(_utc("2026-11-04T06:00:00"), 4.0, 1.0, 0.0), (_utc("2026-11-04T08:00:00"), 3.5, 0.9, 0.0)]


def test_dst_day_has_25_hours_and_02_twice():
    labels = ["00:00", "01:00", "02:00", "02:00", "03:00"] + [f"{h:02d}:00" for h in range(4, 24)]
    assert len(labels) == 25
    rows = D.parse_insights(date(2026, 10, 25), {"data": [{"key": k, "heat_supplied": 1, "electricity_consumed": 0.25}
                                                          for k in labels]})
    assert len(rows) == 25
    ts = [r[0] for r in rows]
    assert ts == sorted(ts) and all(b - a == 3600 for a, b in zip(ts, ts[1:]))  # elk uur precies één keer


def test_unknown_label_stops_without_crash():
    rows = D.parse_insights(date(2026, 11, 4), {"data": [{"key": "05:00"}, {"key": "25:00"}, {"key": "07:00"}]})
    assert len(rows) == 1


class _FakeClient:
    def __init__(self):
        self.asked = []

    async def insights(self, pid, day):
        self.asked.append(day)
        if day < date(2026, 10, 22):  # vóór de installatie: leeg
            return {"data": []}
        return {"data": [{"key": f"{h:02d}:00", "heat_supplied": 0.5, "electricity_consumed": 0.125} for h in range(24)]}


def test_sync_remembers_finished_days(monkeypatch):
    monkeypatch.setattr(D, "HISTORY_DAYS", 20)
    async def no_sleep(_s):
        return None

    monkeypatch.setattr(D.asyncio, "sleep", no_sleep)
    c = _FakeClient()
    today = date(2026, 11, 6)
    asyncio.run(D.sync_history(c, "A-2890", today))
    assert len(c.asked) == 21
    c.asked.clear()
    asyncio.run(D.sync_history(c, "A-2890", today))
    assert c.asked == [today - timedelta(days=1), today]  # alleen gisteren en vandaag opnieuw
    d = get_db().dw_days("2026-10-20", "2026-11-06")
    # het label "00:00" van 22 oktober is het laatste uur van 21 oktober (labels = sluitingsuur)
    assert d["2026-10-21"]["hours"] == 1 and d["2026-10-23"]["hours"] == 24
    assert d["2026-10-23"]["heat_kwh"] == pytest.approx(12.0) and d["2026-10-23"]["elec_kwh"] == pytest.approx(3.0)


def test_week_uses_pump_meter_when_available():
    mon = date(2042, 1, 6)
    assert mon.weekday() == 0
    rows = []
    for i in range(7):
        d0 = int(datetime.combine(mon + timedelta(days=i), datetime.min.time(), tzinfo=config.tz).timestamp())
        rows += [(d0 + h * 3600, 1.2, 0.3, 0.0) for h in range(24)]  # COP 4, 7,2 kWh stroom per dag
    get_db().save_dw_hours(rows)
    get_db().save_weather_days([((mon + timedelta(days=i)).isoformat(), 8.0) for i in range(7)], "open-meteo")
    w = next(x for x in H.weeks(mon + timedelta(days=7), n=2) if x["week_start"] == mon.isoformat())
    assert w["pump_source"] == "dewarmte"
    assert w["cop"] == 4.0 and w["hp_kwh"] == pytest.approx(50.4) and w["heat_kwh"] == pytest.approx(201.6)
    assert w["hp_kwh_per_dd"] == pytest.approx(50.4 / 70, abs=0.01)
