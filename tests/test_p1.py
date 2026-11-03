import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app.integrations.p1 import Direction, parse, signed_grid
from app.state import LiveState

SMETER = {"device": {"sn": "SMS0000000", "type": "NULL", "model": "NULL", "status": {
    "Meter_version": "5.0", "total_power": 241, "la_power": 28, "lb_power": 197, "lc_power": 17,
    "la_voltage": 222, "lb_voltage": 229, "lc_voltage": 222, "la_current": 0, "lb_current": 0, "lc_current": 0,
    "Positive_total_electric": 11094.613, "Reverse_total_electric": 12676.193,
    "Off_peak_Positive_electric": 5403.877, "Flat_section_Positive_electric": 5690.736,
    "Off_peak_Reverse_electric": 3839.629, "Flat_section_Reverse_electric": 8836.564}}}


def test_parse_smeter_real_payload():
    v = parse(SMETER, "smeter")
    assert v["power_w"] == 241 and v["import_kwh"] == 11094.613 and v["export_kwh"] == 12676.193
    assert (v["l1_w"], v["l2_w"], v["l3_w"]) == (28, 197, 17)


def test_parse_homewizard():
    v = parse({"active_power_w": -512, "total_power_import_kwh": 10.5, "total_power_export_kwh": 3.2}, "homewizard")
    assert v["power_w"] == -512 and v["export_kwh"] == 3.2


def test_direction_from_counters():
    d = Direction()
    assert d.update(100.000, 50.000) is None          # eerste meting: richting nog onbekend
    assert d.update(100.001, 50.000) is False         # afnamestand loopt op
    assert d.update(100.001, 50.000) is False         # niets veranderd: richting blijft
    assert d.update(100.001, 50.002) is True          # terugleverstand loopt op
    assert signed_grid(800, True) == -800
    assert signed_grid(800, False) == 800
    assert signed_grid(800, None) == 800
    assert signed_grid(-300, False) == -300           # dongle geeft zelf al een teken
    assert signed_grid(300, False, invert=True) == -300


def test_house_without_battery_uses_p1():
    s = LiveState()
    s.grid.configured = True
    s.solar.configured = True
    s.grid.update(grid_w=-1200)
    s.solar.update(power_w=2000)
    s.heatpump.update(power_w=300)
    snap = s.snapshot()
    assert snap["grid_w"] == -1200
    assert snap["house_w"] == 800  # 2000 zon - 1200 teruglevering
    assert snap["battery_configured"] is False and snap["battery_w"] is None


def test_battery_configured_but_offline_makes_house_unknown():
    s = LiveState()
    s.grid.configured = True
    s.battery.configured = True
    s.grid.update(grid_w=500)
    assert s.snapshot()["house_w"] is None
