import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from datetime import date

from app import exportwatch
from app.exportwatch import ExportWatcher, build_message, heating_season


def test_needs_sustained_export_and_respects_cooldown():
    w = ExportWatcher(min_w=500, minutes=10, cooldown_h=3)
    t = 0.0
    assert w.update(t, -800, False, None) is None
    t += 5 * 60
    assert w.update(t, -300, False, None) is None  # korte dip boven de helft telt niet als einde
    t += 5 * 60
    mins = w.update(t, -900, False, None)
    assert mins is not None and round(mins) == 10
    t += 60 * 60
    assert w.update(t, -900, False, None) is None  # binnen 3 uur geen tweede bericht
    t += 2 * 3600 + 60
    assert w.update(t, -900, False, None) is not None


def test_stops_counting_when_importing():
    w = ExportWatcher(min_w=500, minutes=10)
    w.update(0, -800, False, None)
    w.update(300, 200, False, None)  # afname: teller opnieuw
    assert w.update(700, -800, False, None) is None
    assert w.update(700 + 600, -800, False, None) is not None


def test_battery_must_be_full():
    w = ExportWatcher(min_w=500, minutes=10)
    w.update(0, -800, True, 60)
    assert w.update(700, -800, True, 60) is None
    assert w.update(760, -800, True, None) is None  # onbekend = niet sturen
    assert w.update(820, -800, True, 98) is not None


def test_messages():
    title, body = build_message(-1400, 12, 0.042, False, True, "17:00")
    assert "1,4 kW" in body and "4,2 cent" in body and "17:00" in body
    title, body = build_message(-1400, 12, -0.03, True, False, "17:00")
    assert "kost nu geld" in title and "batterij is vol" in body and "3,0 cent" in body
    assert "verwarming" not in body


def test_heating_season_and_start_date(monkeypatch):
    assert heating_season(date(2027, 7, 1), 9.0) is True
    assert heating_season(date(2027, 1, 10), 16.0) is False
    assert heating_season(date(2027, 1, 10), None) is True
    assert heating_season(date(2027, 7, 1), None) is False
    monkeypatch.setattr(exportwatch.config, "push_export_from", "")
    monkeypatch.setattr(exportwatch.config, "contract_start", "2027-01-01")
    assert exportwatch.active(date(2026, 12, 31)) is False
    assert exportwatch.active(date(2027, 1, 1)) is True
    monkeypatch.setattr(exportwatch.config, "push_export_from", "2026-11-01")
    assert exportwatch.active(date(2026, 11, 3)) is True
