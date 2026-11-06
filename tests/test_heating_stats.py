from datetime import date, datetime, timedelta

import pytest

from app import heating_stats as H
from app.config import config
from app.db import MINUTE_COLS, get_db
from app.recorder import MinuteAccumulator


def _seed(d: date, outdoor: float, hp_w: float = 1200.0, cop: float = 4.0, supply: float = 40.0) -> None:
    """Eén dag: de warmtepomp draait de helft van elke uur, buiten constant `outdoor` graden."""
    start = int(datetime.combine(d, datetime.min.time(), tzinfo=config.tz).timestamp())
    idx = {c: i for i, c in enumerate(MINUTE_COLS)}
    rows = []
    for m in range(1440):
        ts = start + m * 60
        on = m % 60 < 30
        r = [None] * len(MINUTE_COLS)
        r[idx["ts"]], r[idx["day"]], r[idx["hour"]] = ts, d.isoformat(), datetime.fromtimestamp(ts, config.tz).hour
        for c in ("import_wh", "export_wh", "solar_wh", "charge_wh", "discharge_wh", "house_wh", "aircon_wh"):
            r[idx[c]] = 0.0
        r[idx["heatpump_wh"]] = hp_w / 60 if on else 0.0
        r[idx["heat_wh"]] = hp_w * cop / 60 if on else 0.0
        r[idx["supply_temp"]] = supply if on else None
        r[idx["outdoor_temp"]] = outdoor
        r[idx["room_temp"]] = 19.5
        rows.append(tuple(r))
    get_db().write_minutes(rows)


def test_degree_days_and_gas_spread():
    assert H.degree_days(8.0) == 10.0
    assert H.degree_days(20.0) == 0.0
    assert H.degree_days(None) is None
    g = H.gas_per_day([{"start": "2040-01-01", "end": "2040-01-07", "m3": 14.0}])
    assert len(g) == 7 and g["2040-01-03"] == pytest.approx(2.0)


def test_week_summary_per_degree_day():
    mon = date(2040, 1, 2)  # maandag
    assert mon.weekday() == 0
    for i in range(7):
        _seed(mon + timedelta(days=i), outdoor=8.0)
    db = get_db()
    db.add_gas(mon.isoformat(), (mon + timedelta(days=6)).isoformat(), 14.0)
    db.set_kv("gas_base_m3_day", "0.5")
    db.set_kv(f"heating_note_{mon.isoformat()}", "stooklijn 55/35")
    weeks = H.weeks(mon + timedelta(days=6), n=1)
    assert len(weeks) == 1
    w = weeks[0]
    assert w["days"] == 7 and w["temp_days"] == 7
    assert w["degree_days"] == 70.0  # 7 x (18 - 8)
    assert w["hp_kwh"] == pytest.approx(7 * 24 * 0.6, abs=0.1)  # 1,2 kW, half van de tijd
    assert w["cop"] == 4.0 and w["supply_temp"] == 40.0
    assert w["hp_kwh_per_dd"] == pytest.approx(100.8 / 70, abs=0.01)
    assert w["gas_m3"] == 14.0 and w["gas_heating_m3"] == 10.5  # 14 - 7 x 0,5 voor douchen en koken
    assert w["gas_m3_per_dd"] == pytest.approx(10.5 / 70, abs=0.001)
    assert w["note"] == "stooklijn 55/35"


def test_partial_week_and_missing_heat():
    mon = date(2040, 2, 6)
    assert mon.weekday() == 0
    _seed(mon, outdoor=4.0, cop=0.0)  # warmte niet gemeten (oude data): geen COP
    w = H.weeks(mon + timedelta(days=2), n=1)[0]
    assert w["days"] == 3 and w["temp_days"] == 1
    assert w["cop"] is None and w["heat_kwh"] is None
    assert w["degree_days"] == 14.0
    assert w["gas_m3"] is None and w["gas_m3_per_dd"] is None


def test_recorder_logs_heat_and_temperatures():
    acc = MinuteAccumulator(0)
    acc.add({"heat_w": 3600, "supply_temp": 38.0, "outdoor_temp": 6.0, "room_temp": 19.0}, 30)
    acc.add({"heat_w": 0, "supply_temp": 30.0, "outdoor_temp": 6.0, "room_temp": 19.0}, 30)
    assert acc.energy["heat_wh"] == pytest.approx(30.0)
    p = acc.power()
    assert p["supply_temp"] == pytest.approx(34.0) and p["outdoor_temp"] == 6.0


def test_api_gas_and_notes():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.post("/api/heatstats/gas", json={"start": "2026-10-26", "end": "2026-11-01", "m3": "12,5"})
    assert r.status_code == 200
    gid = r.json()["id"]
    assert c.post("/api/heatstats/gas", json={"start": "2026-11-01", "end": "2026-10-26", "m3": 1}).status_code == 400
    assert c.post("/api/heatstats/gas", json={"start": "2026-10-26", "end": "2026-11-01", "m3": "veel"}).status_code == 400
    assert c.post("/api/heatstats/gas-base", json={"m3": "0,6"}).json()["gas_base_m3_day"] == 0.6
    assert c.post("/api/heatstats/note", json={"week_start": "2026-10-27", "text": "x"}).status_code == 400  # dinsdag
    assert c.post("/api/heatstats/note", json={"week_start": "2026-10-26", "text": "eco aan"}).status_code == 200
    d = c.get("/api/heatstats").json()
    assert any(g["id"] == gid for g in d["gas"]) and d["gas_base_m3_day"] == 0.6
    assert c.delete(f"/api/heatstats/gas/{gid}").status_code == 200
    get_db().set_kv("gas_base_m3_day", "")


def test_weather_parse_and_preference():
    from app import weather as W

    times = [f"2041-01-0{d}T{h:02d}:00" for d in (1, 2, 3) for h in range(24)]
    temps = [2.0 if h < 12 else 4.0 for d in (1, 2, 3) for h in range(24)]
    temps[24:34] = [None] * 10  # 2 januari: te weinig uren
    data = {"hourly": {"time": times, "temperature_2m": temps}}
    assert W.parse_daily(data, date(2041, 1, 3)) == [("2041-01-01", 3.0), ("2041-01-03", 3.0)]
    # Open-Meteo gaat voor de sensor; zonder Open-Meteo telt de sensor alleen bij genoeg metingen
    r = {"outdoor_temp": 9.0, "outdoor_minutes": 1440}
    assert H.day_temp(r, {"2041-01-01": 3.2}, "2041-01-01") == 3.2
    assert H.day_temp(r, {}, "2041-01-01") == 9.0
    assert H.day_temp({"outdoor_temp": 9.0, "outdoor_minutes": 100}, {}, "2041-01-01") is None


def test_old_weeks_get_degree_days_from_weather():
    mon = date(2041, 3, 4)
    assert mon.weekday() == 0
    for i in range(7):
        _seed(mon + timedelta(days=i), outdoor=None, cop=0.0)  # oude data: geen sensor, geen warmte
    get_db().save_weather_days([((mon + timedelta(days=i)).isoformat(), 6.0) for i in range(7)], "open-meteo")
    w = H.weeks(mon + timedelta(days=6), n=1)[0]
    assert w["degree_days"] == 84.0 and w["outdoor_temp"] == 6.0
    assert w["hp_kwh_per_dd"] == pytest.approx(100.8 / 84, abs=0.01)
    assert w["cop"] is None
