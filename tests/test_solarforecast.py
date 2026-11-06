import math
from datetime import date, datetime, timedelta

from app import notifier, solarforecast as S
from app.config import config
from app.db import get_db

FACTOR = 2.5  # deze nep-installatie levert 2,5 W per W/m²
HOUSE_W = 300.0


def _rad_for(d: date, scale: float = 1.0) -> dict[int, float]:
    """Straling per uur (begin van het uur): een boog van 08:00 tot 17:00, piek 600 W/m² rond 12:30."""
    out = {}
    for h in range(24):
        ts = int(datetime.combine(d, datetime.min.time(), tzinfo=config.tz).replace(hour=h).timestamp())
        out[ts] = max(0.0, 600 * scale * math.sin(math.pi * (h + 0.5 - 8) / 9)) if 8 <= h < 17 else 0.0
    return out


def _seed(d: date, rad: dict[int, float]) -> None:
    rows = []
    for h_ts, r in rad.items():
        for m in range(60):
            ts = h_ts + m * 60
            local = datetime.fromtimestamp(ts, config.tz)
            solar = r * FACTOR / 60
            house = HOUSE_W / 60
            imp, exp = max(house - solar, 0), max(solar - house, 0)
            rows.append((ts, d.isoformat(), local.hour, imp, exp, solar, 0, 0, 0, house, 0,
                         None, None, None, None, None, None, None))
    get_db().write_minutes(rows)


def test_parse_open_meteo_shifts_to_hour_start():
    data = {"hourly": {"time": [3600, 7200, 10800], "shortwave_radiation": [0, 120.5, None]}}
    assert S.parse_open_meteo(data) == {0: 0.0, 3600: 120.5}


def test_plan_today_learns_from_history(monkeypatch):
    monkeypatch.setattr(config, "latitude", 53.2)
    monkeypatch.setattr(config, "longitude", 6.6)
    today = date(2032, 6, 15)
    rad: dict[int, float] = {}
    for i in range(1, 11):
        d = today - timedelta(days=i)
        r = _rad_for(d, scale=0.6 + 0.04 * i)
        _seed(d, r)
        rad.update(r)
    rad.update(_rad_for(today))  # vandaag: zonnig

    now = datetime.combine(today, datetime.min.time(), tzinfo=config.tz).replace(hour=7, minute=30)
    nowts = int(now.timestamp())
    actual = S.hourly_actuals(nowts - 11 * 86400, nowts)
    cal = S.calibrate(rad, actual)
    assert abs(cal["overall"] - FACTOR) < 0.01
    assert abs(S.baseline(actual)[12] - HOUSE_W) < 1

    blk = S.plan_today(rad, now)
    assert notifier.hm(blk["start"]) == "11:00" and notifier.hm(blk["end"]) == "13:00"
    # piek 600 W/m² × 2,5 = 1500 W, min 300 W huis: zo'n 1,2 kW over
    assert 1100 < blk["surplus_w"] < 1250

    # Bewolkt: te weinig over voor een tip
    cloudy = dict(rad)
    cloudy.update(_rad_for(today, scale=0.2))
    assert S.plan_today(cloudy, now) is None


def test_calibrate_needs_history():
    assert S.calibrate({0: 500.0}, {0: {"solar_wh": 1000.0, "house_wh": 300.0, "minutes": 60}}) is None


def test_best_block_skips_gaps():
    hours = [{"start": 0, "solar_w": 2000, "surplus_w": 1500}, {"start": 7200, "solar_w": 2000, "surplus_w": 1500}]
    assert S.best_block(hours, 2, 600) is None
    hours.insert(1, {"start": 3600, "solar_w": 1000, "surplus_w": 500})
    assert S.best_block(hours, 2, 600)["surplus_w"] == 1000


def test_outlook_sun_hours_and_yield(monkeypatch):
    monkeypatch.setattr(config, "latitude", 53.2)
    monkeypatch.setattr(config, "longitude", 6.6)
    today = date(2032, 6, 15)  # dezelfde geschiedenis als hierboven
    rad: dict[int, float] = {}
    for i in range(1, 11):
        rad.update(_rad_for(today - timedelta(days=i), scale=0.6 + 0.04 * i))
    rad.update(_rad_for(today))
    d0 = int(datetime.combine(today, datetime.min.time(), tzinfo=config.tz).timestamp())
    sun = {d0 + h * 3600: 3600.0 if 10 <= h < 14 else 0.0 for h in range(24)}
    now = datetime.combine(today, datetime.min.time(), tzinfo=config.tz).replace(hour=7, minute=30)
    o = S.outlook({"rad": rad, "sun": sun}, now)
    assert o["sun_hours"] == 4.0
    expected = sum(rad[t] for t in rad if d0 <= t < d0 + 86400) * FACTOR / 1000
    assert abs(o["solar_kwh"] - expected) < 0.2
    assert notifier.hm(o["block"]["start"]) == "11:00"


def test_morning_message_with_sun(monkeypatch):
    monkeypatch.setattr(config, "contract_start", "2033-01-01")
    d = date(2033, 4, 2)
    start = int(datetime.combine(d, datetime.min.time(), tzinfo=config.tz).timestamp())
    get_db().save_prices([(start + q * 900, d.isoformat(), 0.02 if 76 <= q < 84 else 0.10,
                           0.15 if 76 <= q < 84 else 0.23) for q in range(96)])
    now = datetime.combine(d, datetime.min.time(), tzinfo=config.tz).replace(hour=7, minute=30)
    blk = {"start": start + 11 * 3600, "end": start + 13 * 3600, "surplus_w": 1800, "solar_w": 2200}
    win = {"start": start + 10 * 3600, "end": start + 15 * 3600}
    title, body = notifier.build_morning(now, {"sun_hours": 3.2, "sun_window": win, "solar_kwh": 9.2, "block": blk})
    assert title == "☀️ Vandaag zo'n 3 uur zon (ongeveer 9,2 kWh)"
    assert body.startswith("Tussen 11:00 en 13:00 blijft er stroom over (tot 1,8 kW).")
    assert len(body) <= notifier.BODY_MAX  # "De zon schijnt vooral…" valt dan weg
    assert "warmtepomp een graadje hoger" in body  # april: stookseizoen
    assert "goedkoopst tussen 19:00 en 21:00" in body
    # grijze dag
    title, body = notifier.build_morning(now, {"sun_hours": 0.4, "sun_window": None, "solar_kwh": 0.8, "block": None})
    assert title == "☁️ Vandaag nauwelijks zon (ongeveer 0,8 kWh)"
    # zonder eigen geschiedenis: alleen de zonuren
    title, _ = notifier.build_morning(now, {"sun_hours": 6.6, "solar_kwh": None, "block": None})
    assert title == "☀️ Vandaag zo'n 7 uur zon"
    # geen zonverwachting: op prijs
    title, _ = notifier.build_morning(now, None)
    assert title == "Vandaag het goedkoopst: 19:00–21:00"
    # tijdens salderen zonder zon en zonder meldingen: geen bericht
    monkeypatch.setattr(config, "contract_start", "2034-01-01")
    get_db().x("DELETE FROM panel_alert")
    assert notifier.build_morning(now, None) is None


def test_sun_window():
    d0 = 1_000_000 * 3600
    sun = {d0 + h * 3600: (3600.0 if h in (10, 11, 14) else 300.0) for h in range(24)}
    w = S.sun_window(sun, d0, d0 + 86400)
    assert (w["start"] - d0) // 3600 == 10 and (w["end"] - d0) // 3600 == 15
    assert S.sun_window({}, d0, d0 + 86400) is None


def test_share_text():
    assert notifier.share_text(34) == "een derde"
    assert notifier.share_text(50) == "de helft"
    assert notifier.share_text(44) == "44%"
