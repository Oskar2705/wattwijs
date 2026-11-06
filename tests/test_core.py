import asyncio
import json
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import pytest

from app.integrations.aecc import AeccClient, parse_energy_parameter
from app.integrations.dewarmte import parse_product
from app.integrations.solaredge import panels_from_structure
from app.panels import LOW, OK, SKIP, evaluate_day, streak
from app.recorder import MinuteAccumulator
from app.state import LiveState

SAMPLE = {
    "Response": "EnergyParameter",
    "SerialNumber": 1,
    "Storage_list": [{"DevAddr": 1, "BatterySoc": 55, "AcChargingPower": 12000, "BatteryDischargingPower": 0}],
    "SSumInfoList": {
        "MeterTotalActivePower": -320,
        "TotalPVPower": 0,
        "AverageBatteryAverageSOC": 55,
        "TotalGridOutputPower": -1200,
        "TotalBackUpPower": 0,
    },
}


def test_parse_battery_charging_from_surplus():
    v = parse_energy_parameter(SAMPLE)
    assert v["soc"] == 55
    assert v["grid_w"] == -320  # teruglevering
    assert v["battery_w"] == 1200  # laadt
    assert v["battery_ac_w"] == -1200  # neemt af van het huisnet


def test_parse_battery_fallback_without_summary_output():
    raw = json.loads(json.dumps(SAMPLE))
    del raw["SSumInfoList"]["TotalGridOutputPower"]
    v = parse_energy_parameter(raw)
    assert v["battery_w"] == pytest.approx(1200)
    assert v["battery_ac_w"] == pytest.approx(-1200)


def test_grid_invert():
    assert parse_energy_parameter(SAMPLE, grid_invert=True)["grid_w"] == 320


def test_house_energy_balance():
    s = LiveState()
    s.solar.configured = True
    s.battery.configured = True
    s.battery.update(**parse_energy_parameter(SAMPLE))
    s.solar.update(power_w=2000)
    s.heatpump.update(power_w=300)
    snap = s.snapshot()
    # huis = net (-320) + zon (2000) + batterij AC (-1200) = 480
    assert snap["house_w"] == 480
    assert snap["other_w"] == 180
    assert snap["solar_w"] == 2000


def test_house_unknown_when_solar_missing():
    s = LiveState()
    s.solar.configured = True
    s.battery.update(**parse_energy_parameter(SAMPLE))
    assert s.snapshot()["house_w"] is None


def test_aecc_client_against_fake_server():
    async def scenario():
        async def handle(reader, writer):
            line = await reader.readline()
            req = json.loads(line)
            # stale antwoord eerst, daarna het echte, opgesplitst over twee writes
            writer.write(json.dumps({"SerialNumber": 999, "x": 1}).encode())
            body = json.dumps({**SAMPLE, "SerialNumber": req["SerialNumber"]}, indent=2).encode()
            writer.write(body[:40])
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(body[40:])
            await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        c = AeccClient("127.0.0.1", port, timeout=3)
        data = await c.get("EnergyParameter")
        await c.close()
        server.close()
        return data

    data = asyncio.run(scenario())
    assert data["SSumInfoList"]["MeterTotalActivePower"] == -320


def test_dewarmte_parse_kw_to_w():
    v = parse_product({"id": "x", "name": "AO", "status": {"electricity_consumption": "1.25", "heat_output": 4.1}})
    assert v["power_w"] == 1250
    assert v["heat_output_w"] == pytest.approx(4100)


def test_panels_from_structure():
    structure = {
        "type": "SITE",
        "children": [{"type": "FOLDER", "children": [{
            "type": "INVERTER", "name": "Inverter 1", "serial": "INV",
            "children": [{"type": "STRING", "name": "String 1.1", "children": [
                {"type": "OPTIMIZER", "name": "1.1.1", "serial": "A-1", "displayOrder": "1.1"},
                {"type": "OPTIMIZER", "name": "1.1.2", "serial": "A-2", "properties": {"status": "INACTIVE"}},
            ]}],
        }]}],
    }
    p = panels_from_structure(structure)
    assert list(p) == ["A-1"]
    assert p["A-1"]["group"] == "String 1.1"


def _rows(values, grp="S1"):
    return [{"serial": f"P{i}", "name": f"P{i}", "grp": grp, "wh": v} for i, v in enumerate(values)]


def test_without_layout_only_dead_panels_are_flagged():
    ev = evaluate_day(_rows([1000, 1010, 990, 450, 1005]), 20, 100, layout=[])
    assert ev["P3"]["status"] == OK  # zonder ligging geen 'te laag'-oordeel
    ev = evaluate_day(_rows([1000, 1010, 990, 30, 1005]), 20, 100, layout=[])
    assert ev["P3"]["status"] == LOW and ev["P3"]["reason"] == "dead"


def test_dark_day_skips_but_catches_dead_panel():
    ev = evaluate_day(_rows([60, 55, 58, 0]), 20, 100, layout=[])
    assert ev["P0"]["status"] == SKIP
    assert ev["P3"]["status"] == LOW


def test_streak_rules():
    assert streak([LOW, LOW], 2) == (True, False)
    assert streak([LOW, SKIP, LOW], 2) == (True, False)  # donkere dag telt niet mee
    assert streak([LOW, OK, LOW], 2) == (False, False)
    assert streak([OK, LOW, LOW], 2) == (False, True)
    assert streak([LOW], 2) == (False, False)


def test_accumulator_splits_signs():
    acc = MinuteAccumulator(0)
    acc.add({"grid_w": 600, "battery_w": -300, "solar_w": 0, "house_w": 900}, 30)
    acc.add({"grid_w": -600, "battery_w": 300, "solar_w": 1800, "house_w": 900}, 30)
    assert acc.energy["import_wh"] == pytest.approx(5.0)
    assert acc.energy["export_wh"] == pytest.approx(5.0)
    assert acc.energy["charge_wh"] == pytest.approx(2.5)
    assert acc.energy["discharge_wh"] == pytest.approx(2.5)
    assert acc.energy["house_wh"] == pytest.approx(15.0)
    assert acc.power()["grid_w"] == pytest.approx(0)


def test_run_check_opens_and_closes_alert():
    from app import panels as P
    from app.db import get_db

    db = get_db()
    names = {f"P{i}": {"name": f"1.1.{i}", "group": "S1", "order": str(i)} for i in range(5)}

    def day(d, broken):
        e = {s: 1000.0 for s in names}
        if broken:
            e["P2"] = 100.0  # kapot: minder dan 25% van de rest
        db.save_panel_day(d, names, e, final=True)

    day("2026-10-01", True)
    assert asyncio.run(P.run_check("2026-10-01")) == []  # pas 1 dag
    day("2026-10-02", True)
    msgs = asyncio.run(P.run_check("2026-10-02"))
    assert msgs == ["Zonnepaneel 1.1.2 levert (bijna) niets meer op"]
    assert db.open_alerts()[0]["mail_status"] == "geen ontvanger ingesteld"
    day("2026-10-03", True)
    assert asyncio.run(P.run_check("2026-10-03")) == []  # geen tweede mail
    day("2026-10-04", False)
    assert asyncio.run(P.run_check("2026-10-04")) == ["Zonnepaneel 1.1.2 werkt weer normaal"]
    assert db.open_alerts() == []


# Echte waarden uit het SolarEdge-portaal (11 optimizers in één string, 3 rijen op het dak).
LAYOUT = [["1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5"], ["1.1.8", "1.1.7", "1.1.6"], ["1.1.9", "1.1.10", "1.1.11"]]
NOV2 = {"1.1.1": 405, "1.1.2": 381, "1.1.3": 408, "1.1.4": 12, "1.1.5": 297, "1.1.6": 375,
        "1.1.7": 528, "1.1.8": 536, "1.1.9": 692, "1.1.10": 651, "1.1.11": 644}
NOV1 = {"1.1.1": 250, "1.1.2": 227, "1.1.3": 245, "1.1.4": 0, "1.1.5": 231, "1.1.6": 180,
        "1.1.7": 247, "1.1.8": 254, "1.1.9": 199, "1.1.10": 170, "1.1.11": 159}
REAL = NOV2


def _day(vals):
    return [{"serial": k, "name": k, "grp": "String 1.1", "wh": v} for k, v in vals.items()]


def _baselines(days):
    from app.panels import peer_reference
    shares = {}
    for vals in days:
        rows = _day(vals)
        refs = peer_reference(rows, LAYOUT)
        for r in rows:
            shares.setdefault(r["serial"], []).append(r["wh"] / refs[r["serial"]][0])
    from app.panels import baseline_from
    return {k: baseline_from(v) for k, v in shares.items()}


def test_real_days_with_layout_only_dead_panel():
    base = _baselines([NOV1, NOV2, NOV1, NOV2, NOV1])
    for vals in (NOV1, NOV2):
        ev = evaluate_day(_day(vals), 20, 100, layout=LAYOUT, baselines=base)
        assert [k for k, e in ev.items() if e["status"] == LOW] == ["1.1.4"], vals
        assert ev["1.1.6"]["status"] == OK  # vaste schaduw, aangeleerd


def test_real_days_without_history_only_dead_panel():
    for vals in (NOV1, NOV2):
        ev = evaluate_day(_day(vals), 20, 100, layout=LAYOUT)
        assert [k for k, e in ev.items() if e["status"] == LOW] == ["1.1.4"]


def test_degraded_panel_flagged_against_row_neighbours():
    base = _baselines([NOV1, NOV2, NOV1, NOV2, NOV1])
    ev = evaluate_day(_day({**NOV2, "1.1.10": 651 * 0.6}), 20, 100, layout=LAYOUT, baselines=base)
    assert ev["1.1.10"]["status"] == LOW and ev["1.1.10"]["reason"] == "low"
    assert ev["1.1.10"]["compared_with"] == "rij 3"
    assert [k for k, e in ev.items() if e["status"] == LOW] == ["1.1.4", "1.1.10"]


def test_dark_day_still_catches_dead_panel_real():
    ev = evaluate_day(_day({k: v * 0.15 for k, v in NOV2.items()}), 20, 100, layout=LAYOUT)
    assert ev["1.1.4"]["status"] == LOW
    assert ev["1.1.1"]["status"] == SKIP


def test_layout_parsing():
    from app.panels import parse_layout
    assert parse_layout(" 1.1.1, 1.1.2 ;1.1.8,1.1.7,1.1.6; ") == [["1.1.1", "1.1.2"], ["1.1.8", "1.1.7", "1.1.6"]]


def test_history_end_to_end_with_layout_and_revalidation(monkeypatch):
    import random
    from datetime import date, timedelta

    from app import panels as P
    from app.config import config
    from app.db import get_db

    monkeypatch.setattr(config, "panel_layout", ";".join(",".join(r) for r in LAYOUT))
    db = get_db()
    names = {k: {"name": f"Optimizer {k}", "group": "String 1.1", "order": k} for k in NOV2}
    rnd = random.Random(1)
    d0 = date(2025, 6, 1)
    for i in range(14):
        src = NOV1 if i % 2 else NOV2
        energy = {k: v * rnd.uniform(0.95, 1.05) for k, v in src.items()}
        db.save_panel_day((d0 + timedelta(days=i)).isoformat(), names, energy, final=True)
    # een oude, onterechte melding (zoals uit de vorige versie) wordt stil ingetrokken
    db.x("INSERT INTO panel_alert(serial,name,opened_day,deviation,mail_status) VALUES('1.1.10','1.1.10','2025-06-12',-51,'x')")
    assert P.revalidate_open_alerts() == 1
    last = (d0 + timedelta(days=13)).isoformat()
    msgs = asyncio.run(P.run_check(last))
    assert msgs == ["Zonnepaneel 1.1.4 levert (bijna) niets meer op"]
    ev = P.evaluate_stored_day(last)
    assert [k for k, e in ev.items() if e["status"] == LOW] == ["1.1.4"]
    assert ev["1.1.6"]["baseline"] is not None


def test_consumption_and_self_sufficiency_survive_gaps():
    """Huisverbruik ontbrak een paar uur (batterij onbereikbaar), afname en opwek niet."""
    from app.db import consumption_wh, self_sufficiency

    t = {"import_wh": 4870, "export_wh": 300, "solar_wh": 1000, "house_wh": 3160, "charge_wh": 0, "discharge_wh": 0}
    assert consumption_wh(t) == 5570  # balans is vollediger dan de opgetelde minuten
    assert round(self_sufficiency(t)) == 13  # (1000 - 300) / 5570, niet 0%
    # zonder gaten wint de gemeten waarde, en batterijverliezen maken het niet kleiner
    t2 = {"import_wh": 1000, "export_wh": 0, "solar_wh": 500, "house_wh": 1500, "charge_wh": 200, "discharge_wh": 180}
    assert consumption_wh(t2) == 1500
    assert self_sufficiency({"import_wh": 10, "house_wh": 20}) is None


def test_house_energy_not_biased_by_timing_skew():
    # P1 shows export before the inverter reading catches up: house_net briefly negative.
    acc = MinuteAccumulator(0)
    acc.add({"grid_w": -500, "solar_w": 300, "house_w": 0, "house_net_w": -200}, 30)
    acc.add({"grid_w": 100, "solar_w": 500, "house_w": 600, "house_net_w": 600}, 30)
    balance = acc.energy["import_wh"] - acc.energy["export_wh"] + acc.energy["solar_wh"]
    assert acc.energy["house_wh"] == pytest.approx(balance)


def test_panel_view_no_verdict_early_in_the_day(monkeypatch):
    """Overdag met weinig opbrengst: geen rood vak, behalve voor een paneel met een open melding."""
    from datetime import date as _date

    from app import main as M
    from app.db import get_db

    d = "2044-03-01"
    monkeypatch.setattr(M, "today", lambda: _date(2044, 3, 1))
    info = {f"S{i}": {"name": f"1.1.{i}", "group": "A", "order": i} for i in range(1, 6)}
    energy = {"S1": 140.0, "S2": 135.0, "S3": 33.0, "S4": 138.0, "S5": 2.0}
    get_db().save_panel_day(d, info, energy, final=False)
    get_db().x("INSERT INTO panel_alert(serial,name,opened_day,deviation,mail_status) VALUES('S5','1.1.5','2044-02-27',-99,'x')")
    out = {p["serial"]: p for p in M.panel_view(d)["panels"]}
    assert out["S3"]["status"] == "pending" and out["S3"]["reason"] is None  # 33 Wh: nog te vroeg
    assert out["S5"]["status"] == "low"  # open melding: blijft zichtbaar
    get_db().save_panel_day(d, info, {k: v * 5 for k, v in energy.items()}, final=False)
    out = {p["serial"]: p for p in M.panel_view(d)["panels"]}
    assert out["S3"]["status"] == "low"  # genoeg zon: nu wel een oordeel
    get_db().x("DELETE FROM panel_alert WHERE serial='S5'")
