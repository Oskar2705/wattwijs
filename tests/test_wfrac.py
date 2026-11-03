import os
import sqlite3
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import pytest
from pywfrac import AirconCommands, RacParser

from app import db as dbmod
from app.integrations.wfrac import (
    FAN_ONLY_W,
    PowerEstimator,
    Unit,
    UnitConfig,
    command_params,
    parse_units,
    unit_view,
)

# Real airconStat frames from an SRK35ZS-WF, taken from the test captures of the
# Home Assistant integration blues-sechseck/Mitsubishi-WF-RAC-Integration (MIT).
OFF = "AACqj6r/AAAIAAAUigAAAAAAAf////9hp4EECAcqmgAAiAAABAAAAAAAAAOAIJn/gBDP/5QQAAAOQQ=="
ON_COOL = "AACrj7T/AAAIAAAUigAAAAAAAf/////0NoEECQc0mwAAiAAABAAAAAAAAAOAIJv/gBDT/5QQAAD4rQ=="
ON_HEAT = "AACzj6r/AAAIAAAUigAAAAAAAf////8sM4EEEQcqmgAAgQAABAAAAAAAAAOAIJr/gBDT/5QQAACzfA=="


def decode(frame):
    return RacParser().translate_bytes(frame)


def test_parse_units():
    units = parse_units(" Slaapkamer = 192.168.1.40 ;Zolder=192.168.1.41:51444;;kapot;Zolder=10.0.0.9")
    assert [(u.id, u.name, u.host, u.port) for u in units] == [
        ("slaapkamer", "Slaapkamer", "192.168.1.40", 51443),
        ("zolder", "Zolder", "192.168.1.41", 51444),
        ("zolder-2", "Zolder", "10.0.0.9", 51443),
    ]
    assert parse_units("") == []
    assert parse_units("Kamer van Piet's=1.2.3.4")[0].id == "kamer-van-piet-s"


def test_unit_view_from_real_frames():
    u = Unit(UnitConfig("kamer", "Kamer", "x"))
    u.aircon = decode(ON_COOL)
    u.updated = 1e12  # ver in de toekomst = altijd vers
    v = unit_view(u, now=1e12)
    assert v["on"] is True and v["mode"] == "cool" and v["target"] == 26.0
    assert v["room_temp"] == 24.0 and v["outdoor_temp"] == 30.0
    assert (v["min"], v["max"]) == (16.0, 30.0)
    assert v["fault"] is None
    assert v["fan"] == "auto"
    u.aircon = decode(ON_HEAT)
    assert unit_view(u, now=1e12)["mode"] == "heat"
    u.aircon = decode(OFF)
    assert unit_view(u, now=1e12)["on"] is False


def test_command_params_and_validation():
    a = decode(OFF)
    p = command_params(a, {"on": True})
    assert p == {AirconCommands.Operation: True}
    p = command_params(a, {"mode": "heat"})
    assert p == {AirconCommands.OperationMode: 2, AirconCommands.Operation: True}
    p = command_params(a, {"target": 21.3})
    assert p == {AirconCommands.PresetTemp: 21.5}  # de unit kent alleen halve graden
    with pytest.raises(ValueError):
        command_params(a, {"target": 40})
    with pytest.raises(ValueError):
        command_params(a, {"mode": "turbo"})
    assert command_params(a, {"fan": "quiet"}) == {AirconCommands.AirFlow: 1}
    with pytest.raises(ValueError):
        command_params(a, {"fan": "eco"})
    with pytest.raises(ValueError):
        command_params(a, {})
    # Koelen mag tot 16 graden, verwarmen niet
    assert command_params(a, {"mode": "cool", "target": 16})[AirconCommands.PresetTemp] == 16.0
    with pytest.raises(ValueError):
        command_params(a, {"mode": "heat", "target": 16})


def test_command_frame_round_trip():
    """Wat we sturen, moet de bibliotheek weer kunnen lezen met de gewijzigde waarden."""
    from pywfrac import AirconStat

    a = decode(OFF)
    stat = AirconStat.from_aircon(a)
    for k, v in command_params(a, {"mode": "heat", "target": 22.5}).items():
        setattr(stat, k, v)
    frame = RacParser().to_base64(stat)
    assert isinstance(frame, str) and len(frame) > 20


def test_power_estimator():
    e = PowerEstimator(default_w=500)
    t = 0.0
    assert e.update(t, on=False, compressor=False, kwh=0.0) == 0.0
    # aangezet: teller begint op 0, nog geen meting -> standaardwaarde
    t += 60
    assert e.update(t, on=True, compressor=True, kwh=0.0) == 500
    # na 30 minuten eerste stap van 0,25 kWh -> 500 W gemiddeld
    t += 1800
    assert e.update(t, on=True, compressor=True, kwh=0.25) == pytest.approx(500)
    # volgende stap na 20 minuten -> 750 W, gemiddeld met de vorige
    t += 1200
    assert e.update(t, on=True, compressor=True, kwh=0.5) == pytest.approx(625)
    # compressor rust: alleen de ventilator
    assert e.update(t + 60, on=True, compressor=False, kwh=0.5) == FAN_ONLY_W
    assert e.update(t + 120, on=False, compressor=False, kwh=0.5) == 0.0


def test_power_estimator_started_while_running_skips_partial_step():
    e = PowerEstimator(default_w=500)
    # app start terwijl de unit al draait en de teller op 1,0 staat: begin van de stap onbekend
    assert e.update(0, on=True, compressor=True, kwh=1.0) == 500
    # eerste stap na 5 minuten zou 3 kW suggereren; die telt niet mee
    assert e.update(300, on=True, compressor=True, kwh=1.25) == 500
    # de stap daarna is wel volledig gemeten: 0,25 kWh in 25 min = 600 W
    assert e.update(300 + 1500, on=True, compressor=True, kwh=1.5) == pytest.approx(600)


def test_old_database_gets_aircon_columns(tmp_path):
    path = tmp_path / "oud.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE minute (ts INTEGER PRIMARY KEY, day TEXT NOT NULL, hour INTEGER NOT NULL, "
                 "import_wh REAL, export_wh REAL, solar_wh REAL, charge_wh REAL, discharge_wh REAL, "
                 "heatpump_wh REAL, house_wh REAL, grid_w REAL, solar_w REAL, battery_w REAL, house_w REAL, "
                 "heatpump_w REAL, soc REAL)")
    conn.execute("INSERT INTO minute(ts, day, hour, import_wh) VALUES (60, '2026-11-01', 0, 5)")
    conn.commit()
    conn.close()
    d = dbmod.DB(str(path))
    cols = {r["name"] for r in d.q("PRAGMA table_info(minute)")}
    assert {"aircon_wh", "aircon_w"} <= cols
    assert d.totals_for_day("2026-11-01")["import_wh"] == 5


def test_ids_are_short_enough_for_the_module(monkeypatch):
    from app.db import get_db
    from app.integrations import wfrac

    db = get_db()
    # oude, te lange ID's uit de eerste versie worden vervangen
    db.set_kv("wfrac_operator_id", "energiemonitor-" + "x" * 36)
    db.set_kv("wfrac_device_id", "energiemonitor-device-" + "y" * 12)
    op, dev = wfrac._ids()
    assert len(op) <= 36 and len(dev) <= 32
    assert wfrac._ids() == (op, dev)  # daarna stabiel
