"""Demomodus: realistische nepdata, zodat de app zonder apparaten te testen is."""

from __future__ import annotations

import asyncio
import math
import random
import time
from datetime import date, datetime, timedelta
from typing import Any

from ..config import config
from ..db import ENERGY_COLS, POWER_COLS, get_db
from ..prices import QUARTER
from ..recorder import MinuteAccumulator
from ..state import Source, state
from . import register

PEAK_SOLAR_W = 4200.0
CAPACITY_WH = 5120.0


def sun(t: datetime, cloud_seed: int = 0) -> float:
    """0..1 zonkracht voor een lokaal tijdstip (najaar: ~8:00-17:00)."""
    h = t.hour + t.minute / 60
    rise, set_ = 8.0, 17.2
    if h <= rise or h >= set_:
        return 0.0
    base = math.sin(math.pi * (h - rise) / (set_ - rise)) ** 1.4
    rnd = random.Random(cloud_seed * 10000 + int(h * 12))
    clouds = 0.55 + 0.45 * rnd.random()
    return max(base * clouds, 0.0)


def house_base(t: datetime) -> float:
    h = t.hour + t.minute / 60
    w = 180.0
    if 7 <= h < 9:
        w += 450
    if 12 <= h < 13:
        w += 300
    if 17.5 <= h < 19.5:
        w += 1100  # koken
    if 19.5 <= h < 23:
        w += 250
    return w


def heatpump(t: datetime) -> float:
    # Pomp draait in blokken; harder 's nachts/ochtends.
    slot = (t.hour * 60 + t.minute) // 40
    on = random.Random(slot * 7 + t.toordinal()).random() < (0.65 if t.hour < 9 or t.hour >= 18 else 0.4)
    return 1150.0 + 250 * math.sin(t.minute / 9) if on else 12.0


def aircon_history(t: datetime) -> float:
    """Geschiedenis: 's avonds verwarmt één airco een slaapkamer bij."""
    h = t.hour + t.minute / 60
    return 520.0 + 120 * math.sin(t.minute / 7) if 19 <= h < 22.5 else 0.0


class Sim:
    def __init__(self, live: bool = False) -> None:
        self.soc_wh = CAPACITY_WH * 0.45
        self.live = live

    def step(self, t: datetime, dt: float) -> dict[str, float]:
        solar = PEAK_SOLAR_W * sun(t, t.toordinal())
        hp = heatpump(t)
        ac = demo_aircons.total_w() if self.live else aircon_history(t)
        house = house_base(t) + hp + ac + random.uniform(-30, 60)
        surplus = solar - house
        soc_pct = self.soc_wh / CAPACITY_WH * 100
        if surplus > 50 and soc_pct < 100:
            batt = min(surplus, 2400.0)
        elif surplus < -50 and soc_pct > 10:
            batt = -min(-surplus, 2400.0)
        else:
            batt = 0.0
        self.soc_wh = min(max(self.soc_wh + batt * dt / 3600 * (0.95 if batt > 0 else 1.0), 0), CAPACITY_WH)
        grid = house - solar + batt
        return {"solar": solar, "hp": hp, "ac": ac, "house": house, "batt": batt, "grid": grid,
                "soc": self.soc_wh / CAPACITY_WH * 100}


# ---------------- demo-airco's ----------------


class DemoAircons:
    """Drie gesimuleerde binnenunits die echt reageren op de knoppen in het tabblad Airco."""

    def __init__(self) -> None:
        from pywfrac import Aircon

        from .wfrac import PowerEstimator, Unit, UnitConfig

        names = [("Slaapkamer", 1, 2, 20.0, 18.4), ("Kinderkamer", 0, 2, 21.0, 19.6), ("Zolder", 0, 1, 23.0, 22.1)]
        self.units = []
        for name, on, mode, target, room in names:
            u = Unit(UnitConfig(name.lower(), name, "demo"), est=PowerEstimator())
            u.aircon = Aircon(Operation=bool(on), OperationMode=mode, PresetTemp=target, IndoorTemp=room,
                              OutdoorTemp=8.5, Electric=0.0)
            u.airco_id = name.lower()
            self.units.append(u)

    def step(self, dt: float) -> None:
        for u in self.units:
            a = u.aircon
            gap = a.PresetTemp - a.IndoorTemp
            want = (a.OperationMode == 2 and gap > 0.3) or (a.OperationMode == 1 and gap < -0.3) or (
                a.OperationMode == 0 and abs(gap) > 0.3)
            a.CompressorRunning = bool(a.Operation and want)
            if a.CompressorRunning:
                u.power_w = min(380 + 160 * abs(gap), 900) + random.uniform(-20, 20)
                a.IndoorTemp += math.copysign(min(abs(gap), 0.02 * dt / 10), gap)
            else:
                u.power_w = 25.0 if a.Operation else 0.0
                a.IndoorTemp += (17.0 - a.IndoorTemp) * 0.0004 * dt / 10  # koelt langzaam af naar 17 graden
            a.IndoorTemp = round(a.IndoorTemp, 2)
            u.updated = time.time()

    def total_w(self) -> float:
        return sum(u.power_w for u in self.units)


demo_aircons = DemoAircons()
sim = Sim(live=True)


def backfill_history(days: int = 45) -> None:
    db = get_db()
    if db.first_day():
        return
    tz = config.tz
    start = int(time.time() // 60) * 60 - days * 86400
    end = int(time.time() // 60) * 60
    rows = []
    s = Sim(live=False)
    for ts in range(start, end, 60):
        t = datetime.fromtimestamp(ts, tz)
        v = s.step(t, 60)
        acc = MinuteAccumulator(ts)
        snap = {
            "grid_w": v["grid"],
            "solar_w": v["solar"],
            "battery_w": v["batt"],
            "house_w": v["house"],
            "heatpump_w": v["hp"],
            "aircon_w": v["ac"],
            "soc": v["soc"],
        }
        acc.add(snap, 60)
        p = acc.power()
        rows.append(
            (ts, t.date().isoformat(), t.hour, *[acc.energy[c] for c in ENERGY_COLS], *[p[c] for c in POWER_COLS])
        )
    db.write_minutes(rows)
    sim.soc_wh = s.soc_wh


@register("grid", "demo", "Demo (nepgegevens)", description="Simuleert het hele huis: net, zon, batterij en warmtepomp.")
async def run(src: Source) -> None:
    """Simuleert alle live bronnen tegelijk; de andere demo-rollen hoeven niets te doen."""
    await asyncio.to_thread(backfill_history)
    last = time.time()
    while True:
        now = time.time()
        t = datetime.fromtimestamp(now, config.tz)
        v = sim.step(t, now - last)
        last = now
        state.grid.update(grid_w=v["grid"], power_w=abs(v["grid"]))
        state.battery.update(
            grid_w=v["grid"], battery_w=v["batt"], battery_ac_w=-v["batt"], pv_w=0.0, soc=v["soc"]
        )
        state.solar.update(power_w=v["solar"])
        state.heatpump.update(
            power_w=v["hp"],
            heat_output_w=v["hp"] * 3.6 if v["hp"] > 100 else 0.0,
            supply_temperature=34.5 if v["hp"] > 100 else 27.0,
            outdoor_temperature=8.5,
            is_on=v["hp"] > 100,
            fault_code=0,
        )
        await asyncio.sleep(2)


async def _idle(src: Source) -> None:
    while True:  # de demo voor 'grid' vult deze bron al
        await asyncio.sleep(3600)


register("battery", "demo", "Demo (nepgegevens)")(_idle)
register("solar", "demo", "Demo (nepgegevens)")(_idle)
register("heatpump", "demo", "Demo (nepgegevens)")(_idle)


@register("aircon", "demo", "Demo (3 airco's)")
async def run_aircon(src: Source) -> None:
    from .wfrac import command_params, publish, unit_view

    by_id = {u.cfg.id: u for u in demo_aircons.units}

    async def control(unit_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        if not config.airco_control:
            raise PermissionError("bedienen staat uit (AIRCO_CONTROL=0)")
        u = by_id[unit_id]
        params = command_params(u.aircon, changes)
        await asyncio.sleep(0.8)  # een echte unit doet er ook even over
        for k, v in params.items():
            setattr(u.aircon, str(k), v)
        demo_aircons.step(0)
        publish(src, demo_aircons.units)
        return unit_view(u)

    src.control = control
    last = time.time()
    while True:
        now = time.time()
        demo_aircons.step(now - last)
        last = now
        publish(src, demo_aircons.units)
        await asyncio.sleep(5)


@register("panels", "demo", "Demo (12 panelen, één kapot)")
class DemoPanels:
    """12 panelen in 2 strings; paneel 1.1.4 is 'kapot' en levert ~45% minder."""

    async def panels(self) -> dict[str, dict[str, Any]]:
        out = {}
        for s in (1, 2):
            for i in range(1, 7):
                sid = f"DEMO{s}{i:02d}-AB"
                out[sid] = {"name": f"1.{s}.{i}", "group": f"String 1.{s}", "order": f"1.{s}.{i:02d}"}
        return out

    async def panel_energy(self, day: date) -> dict[str, float]:
        rnd = random.Random(day.toordinal())
        today = datetime.now(config.tz).date()
        frac = 1.0
        if day == today:
            now = datetime.now(config.tz)
            frac = min(max((now.hour + now.minute / 60 - 8) / 9.2, 0), 1)
        weather = 0.25 + 0.75 * rnd.random()
        out = {}
        for sid, info in (await self.panels()).items():
            base = 1350 if info["group"].endswith(".1") else 1050  # tweede dakvlak minder gunstig
            wh = base * weather * frac * rnd.uniform(0.93, 1.05)
            if sid == "DEMO104-AB" and day >= today - timedelta(days=4):
                wh *= 0.55
            out[sid] = round(wh, 1)
        return out


# ---------------- demo-prijzen ----------------


def demo_prices(day: date) -> list[tuple[int, str, float, float]]:
    rnd = random.Random(day.toordinal() * 31)
    sunny = rnd.random() < 0.45
    start = datetime.combine(day, datetime.min.time(), tzinfo=config.tz)
    out = []
    t = start
    while t.date() == day:
        h = t.hour + t.minute / 60
        market = 0.085 + 0.02 * math.sin(h / 3)
        if 6.5 <= h < 9.5:
            market += 0.06
        if 10 <= h < 15.5:
            market -= 0.045 + (0.06 if sunny else 0.0) * math.sin(math.pi * (h - 10) / 5.5)
        if 17 <= h < 21:
            market += 0.10 + 0.04 * math.sin(math.pi * (h - 17) / 4)
        market += rnd.uniform(-0.008, 0.008)
        all_in = (market + 0.02) * 1.21 + 0.1108
        ts = int(t.timestamp())
        out.append((ts, day.isoformat(), round(market, 5), round(all_in, 5)))
        t = datetime.fromtimestamp(ts + QUARTER, config.tz)
    return out




@register("prices", "demo", "Demo (nepprijzen)")
class DemoPrices:
    source = "demo"

    async def fetch_day(self, session: Any, day: date) -> list[tuple[int, str, float, float]]:
        return demo_prices(day)


# ---------------- demo-verwarming (tado) ----------------


@register("heating", "demo", "Demo (tado, 3 kamers)")
async def run_heating(src: Source) -> None:
    from .tado import TERMINATIONS, apply_change, comfort_changes

    now = time.time()
    rooms = [
        {"id": 1, "name": "Woonkamer", "room_temp": 20.4, "humidity": 52, "on": True, "target": 20.5,
         "heating_pct": 35, "mode": "schedule", "manual_until": None, "manual_type": None,
         "next_change_at": now + 3 * 3600, "next_change_target": 18.0, "window_open": False, "online": True},
        {"id": 2, "name": "Keuken", "room_temp": 19.1, "humidity": 58, "on": True, "target": 19.0,
         "heating_pct": 0, "mode": "schedule", "manual_until": None, "manual_type": None,
         "next_change_at": now + 3 * 3600, "next_change_target": 17.0, "window_open": False, "online": True},
        {"id": 3, "name": "Slaapkamer", "room_temp": 17.2, "humidity": 55, "on": False, "target": None,
         "heating_pct": 0, "mode": "manual", "manual_until": None, "manual_type": TERMINATIONS["manual"],
         "next_change_at": None, "next_change_target": None, "window_open": False, "online": True},
    ]
    status = {"linked": True, "login": None, "remaining": 87, "termination": config.tado_manual_until}

    async def control(room_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        i = next(i for i, r in enumerate(rooms) if str(r["id"]) == str(room_id))
        if changes.get("mode") == "comfort":
            changes = comfort_changes(rooms[i], changes.get("target"), changes.get("hours"))
        if changes.get("target") is not None and not 5 <= float(changes["target"]) <= 25:
            raise ValueError("temperatuur moet tussen 5 en 25 °C liggen")
        await asyncio.sleep(0.6)
        r = apply_change(rooms[i], changes, config.tado_manual_until)
        r.pop("pending", None)
        if changes.get("mode") == "schedule":
            r.update(on=True, target=20.0)
        if r["mode"] == "manual" and r["manual_type"] == "NEXT_TIME_BLOCK" and not changes.get("hours"):
            r["manual_until"] = r.get("next_change_at")
        rooms[i] = r
        status["remaining"] -= 1
        src.update(rooms=rooms, tado=status)
        return r

    async def nothing() -> dict[str, Any]:
        return {"linked": True}

    src.control = control
    src.actions = {"login": nothing, "logout": nothing}
    while True:
        for r in rooms:
            goal = r["target"] if r["on"] and r["target"] else 16.0
            r["room_temp"] = round(r["room_temp"] + (goal - r["room_temp"]) * 0.02, 2)
            r["heating_pct"] = max(0, min(100, int((goal - r["room_temp"]) * 60))) if r["on"] else 0
        src.update(rooms=rooms, tado=status)
        await asyncio.sleep(10)
