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
    # gekeken op de maandag erna: de hele week is dan afgelopen
    w = next(x for x in H.weeks(mon + timedelta(days=7), n=2) if x["week_start"] == mon.isoformat())
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
    w = H.weeks(mon + timedelta(days=2), n=1)[0]  # woensdag: alleen maandag heeft gegevens
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


class _Req:
    """Nep-Request: de endpoints lezen alleen `await request.json()`."""

    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def test_api_gas_and_notes():
    # Endpoints rechtstreeks aanroepen: geen testclient nodig (die vraagt een extra pakket).
    import asyncio

    from fastapi import HTTPException

    from app import main as M

    run = asyncio.run
    gid = run(M.gas_add(_Req({"start": "2026-10-26", "end": "2026-11-01", "m3": "12,5"})))["id"]
    for bad in ({"start": "2026-11-01", "end": "2026-10-26", "m3": 1},
                {"start": "2026-10-26", "end": "2026-11-01", "m3": "veel"}):
        with pytest.raises(HTTPException) as e:
            run(M.gas_add(_Req(bad)))
        assert e.value.status_code == 400
    assert run(M.gas_base(_Req({"m3": "0,6"})))["gas_base_m3_day"] == 0.6
    with pytest.raises(HTTPException):
        run(M.heat_note(_Req({"week_start": "2026-10-27", "text": "x"})))  # dinsdag
    assert run(M.heat_note(_Req({"week_start": "2026-10-26", "text": "eco aan"})))["text"] == "eco aan"
    d = M.heatstats()
    assert any(g["id"] == gid for g in d["gas"]) and d["gas_base_m3_day"] == 0.6
    assert M.gas_delete(gid) == {"deleted": gid}
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
    w = next(x for x in H.weeks(mon + timedelta(days=7), n=2) if x["week_start"] == mon.isoformat())
    assert w["degree_days"] == 84.0 and w["outdoor_temp"] == 6.0
    assert w["hp_kwh_per_dd"] == pytest.approx(100.8 / 84, abs=0.01)
    assert w["cop"] is None


def test_cop_only_over_minutes_with_heat_measurement():
    """Dag waarop de warmtemeting halverwege begint (zoals bij de update): COP mag niet zakken."""
    d = date(2041, 5, 7)
    start = int(datetime.combine(d, datetime.min.time(), tzinfo=config.tz).timestamp())
    idx = {c: i for i, c in enumerate(MINUTE_COLS)}
    rows = []
    for m in range(1440):
        r = [None] * len(MINUTE_COLS)
        ts = start + m * 60
        r[idx["ts"]], r[idx["day"]], r[idx["hour"]] = ts, d.isoformat(), datetime.fromtimestamp(ts, config.tz).hour
        for c in ("import_wh", "export_wh", "solar_wh", "charge_wh", "discharge_wh", "house_wh", "aircon_wh", "heat_wh"):
            r[idx[c]] = 0.0
        r[idx["heatpump_wh"]] = 10.0  # hele dag 600 W
        if m >= 1200:  # pas de laatste 4 uur warmte gemeten
            r[idx["heat_wh"]] = 40.0
            r[idx["supply_temp"]] = 38.0
        rows.append(tuple(r))
    get_db().write_minutes(rows)
    w = H.weeks(d, n=1)[0]
    assert w["cop"] == 4.0
    assert w["hp_kwh"] == pytest.approx(14.4)


def test_today_not_in_degree_days():
    """De lopende dag telt niet mee: temperatuur voor de hele dag, verbruik pas tot nu."""
    mon = date(2041, 9, 2)
    assert mon.weekday() == 0
    _seed(mon, outdoor=8.0)
    _seed(mon + timedelta(days=1), outdoor=8.0)
    w = H.weeks(mon + timedelta(days=1), n=1)[0]  # 'vandaag' is dinsdag
    assert w["temp_days"] == 1 and w["degree_days"] == 10.0
    assert w["hp_kwh_per_dd"] == pytest.approx(14.4 / 10, abs=0.01)  # alleen maandag


def test_boiler_target_and_supply_weighting():
    from app.integrations.dewarmte import parse_product
    from app.recorder import MinuteAccumulator

    v = parse_product({"status": {"gas_boiler": False, "thermostat": True, "target_temperature": 43,
                                  "supply_temperature": 36, "heat_output": 0.0, "electricity_consumption": 0.01}})
    assert v["gas_boiler"] is False and v["thermostat_demand"] is True and v["target_supply_temperature"] == 43.0
    acc = MinuteAccumulator(0)
    acc.add({"boiler_on": 1.0, "target_supply_temp": 44.0}, 15)
    acc.add({"boiler_on": 0.0, "target_supply_temp": 42.0}, 45)
    p = acc.power()
    assert p["boiler_on"] == pytest.approx(0.25) and p["target_supply_temp"] == pytest.approx(42.5)

    # week: maandag alleen pompmeter (geen temperaturen), dinsdag ook eigen minuten met aanvoer/streef/ketel
    mon = date(2043, 1, 5)
    assert mon.weekday() == 0
    d0 = int(datetime.combine(mon, datetime.min.time(), tzinfo=config.tz).timestamp())
    get_db().save_dw_hours([(d0 + h * 3600, 1.0, 0.25, 0.0) for h in range(48)])
    idx = {c: i for i, c in enumerate(MINUTE_COLS)}
    rows = []
    for m in range(1440):
        ts = d0 + 86400 + m * 60
        r = [None] * len(MINUTE_COLS)
        r[idx["ts"]], r[idx["day"]], r[idx["hour"]] = ts, (mon + timedelta(days=1)).isoformat(), m // 60
        for c in ("import_wh", "export_wh", "solar_wh", "charge_wh", "discharge_wh", "house_wh", "aircon_wh"):
            r[idx[c]] = 0.0
        r[idx["heatpump_wh"]], r[idx["heat_wh"]] = 4.0, 16.0
        r[idx["supply_temp"]], r[idx["target_supply_temp"]] = 38.0, 43.0
        r[idx["boiler_on"]] = 1.0 if m < 90 else 0.0  # anderhalf uur ketel
        rows.append(tuple(r))
    get_db().write_minutes(rows)
    w = H.weeks(mon + timedelta(days=2), n=1)[0]
    assert w["supply_temp"] == 38.0 and w["target_supply_temp"] == 43.0  # niet verdund door maandag
    assert w["boiler_h"] == 1.5
    assert w["cop"] == 4.0


def test_evening_mentions_boiler(monkeypatch):
    from app import notifier

    d = date(2043, 1, 6)  # dinsdag uit de vorige test: 1,5 uur ketel
    monkeypatch.setattr(config, "contract_start", "2099-01-01")
    assert "De cv-ketel sprong 1,5 uur bij." in "\n\n".join(notifier.story(d))
