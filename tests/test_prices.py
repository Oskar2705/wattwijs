import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from datetime import datetime, timezone

import pytest

from app.db import get_db
from app.integrations.frank import parse_prices
from app.prices import cheapest_block, price_info, windows


def _resp(items):
    return {"data": {"marketPrices": {"electricityPrices": items}}}


def test_parse_quarter_and_hour_slots():
    resp = _resp([
        {"from": "2026-11-01T23:00:00.000Z", "till": "2026-11-01T23:15:00.000Z", "marketPrice": 0.1, "allInPrice": 0.25},
        # uurprijs zonder allInPrice: som van de onderdelen, verdeeld over 4 kwartieren
        {"from": "2026-11-02T00:00:00.000Z", "till": "2026-11-02T01:00:00.000Z", "marketPrice": 0.08,
         "marketPriceTax": 0.0168, "sourcingMarkupPrice": 0.02, "energyTaxPrice": 0.1108},
    ])
    rows = parse_prices(resp)
    assert len(rows) == 5
    assert rows[0][1] == "2026-11-02"  # 23:00 UTC = 00:00 lokaal
    assert rows[0][3] == 0.25
    assert rows[1][3] == pytest.approx(0.2276)
    assert [r[0] for r in rows[1:]] == [rows[1][0] + i * 900 for i in range(4)]


def test_costs_join_minutes_with_quarter_prices():
    db = get_db()
    ts = int(datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp())
    db.save_prices([(ts, "2026-11-10", 0.10, 0.30)])
    # 10 minuten: 100 Wh afname per minuut, daarna 10 minuten 200 Wh teruglevering (zelfde kwartier? nee: 2e kwartier zonder prijs)
    rows = []
    for i in range(10):
        rows.append((ts + 60 * i, "2026-11-10", 13, 100.0, 0.0, 0, 50.0, 20.0, 0, 100, None, None, None, None, None, None))
    for i in range(3):
        rows.append((ts + 600 + 60 * i, "2026-11-10", 13, 0.0, 200.0, 0, 0, 0, 0, 0, None, None, None, None, None, None))
    rows.append((ts + 1000, "2026-11-10", 13, 100.0, 0.0, 0, 0, 0, 0, 0, None, None, None, None, None, None))  # 2e kwartier, geen prijs
    db.write_minutes(rows)
    t = db.totals_for_day("2026-11-10")
    # afname 1000 Wh x 0,30 = 0,30 (de minuut zonder prijs telt niet mee)
    assert t["cost_import"] == pytest.approx(0.30)
    # teruglevering 600 Wh x 0,10 = 0,06
    assert t["revenue_export"] == pytest.approx(0.06)
    # batterij: ontladen 200 Wh x 0,30 - laden 500 Wh uit net x 0,30 = -0,09
    assert t["battery_value"] == pytest.approx(0.06 - 0.15)
    assert t["priced_minutes"] == 13 and t["minutes"] == 14


def _rows(prices, start=0):
    return [{"ts": start + i * 900, "all_in": p, "export": p - 0.15, "day": "d"} for i, p in enumerate(prices)]


def test_cheapest_block():
    rows = _rows([0.3] * 10 + [0.1] * 8 + [0.3] * 10)
    b = cheapest_block(rows, 8)
    assert b["start"] == 10 * 900 and b["end"] == 18 * 900 and b["avg"] == pytest.approx(0.1)


def test_negative_windows():
    rows = _rows([0.2, 0.1, 0.12, 0.2, 0.1])
    w = windows(rows, lambda r: r["export"] < 0)
    assert [(x["start"], x["end"]) for x in w] == [(900, 2700), (3600, 4500)]
    assert w[0]["min_export"] == pytest.approx(-0.05)


def test_price_info_levels():
    db = get_db()
    day_start = int(datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc).timestamp()) - 3600  # 00:00 lokaal
    prices = [0.2 + 0.01 * (i % 24) for i in range(96)]
    db.save_prices([(day_start + i * 900, "2026-11-20", p - 0.15, p) for i, p in enumerate(prices)])
    info = price_info(now=day_start + 23 * 900 + 10)  # duurste kwartier van het blok
    assert info["now"]["level"] == "duur"
    info = price_info(now=day_start + 24 * 900 + 10)
    assert info["now"]["level"] == "goedkoop"
    assert info["has_tomorrow"] is False


def test_switch_from_demo_clears_prices(monkeypatch):
    from app import prices
    from app.config import config

    db = get_db()
    monkeypatch.setattr(config, "demo", True)
    prices.ensure_source("demo" if config.demo else "frank")
    db.save_prices([(1, "2026-01-01", 0.1, 0.2)])
    monkeypatch.setattr(config, "demo", False)
    prices.ensure_source("demo" if config.demo else "frank")
    assert db.q("SELECT COUNT(*) AS n FROM price")[0]["n"] == 0
    db.save_prices([(1, "2026-01-01", 0.1, 0.2)])
    prices.ensure_source("demo" if config.demo else "frank")  # zelfde bron: niets wissen
    assert db.q("SELECT COUNT(*) AS n FROM price")[0]["n"] == 1


def test_naive_timestamp_is_utc():
    from app.integrations.frank import _ts

    assert _ts("2026-11-02T12:00:00") == _ts("2026-11-02T12:00:00Z") == _ts("2026-11-02T13:00:00+01:00")
