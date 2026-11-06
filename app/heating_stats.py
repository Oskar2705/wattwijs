"""Verwarming per week: graaddagen, stroom en warmte van de warmtepomp, COP en gas.

Bedoeld om instellingen (stooklijn, eco, slimme sturing) eerlijk te vergelijken: koude weken
kosten meer energie, dus alles wordt ook per graaddag uitgedrukt.
Graaddagen: per dag max(0, 18 - gemiddelde buitentemperatuur), zonder weging.

Gas gaat ook naar douchen en koken. Dat deel schatten we als een vaste basis per dag (bijv. het
gemiddelde per dag in de zomer, als er niet gestookt wordt) en trekken we af; wat overblijft telt als
gas voor verwarming. Per dag schommelt dat (wel of niet gedoucht), per week middelt het grotendeels uit.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .config import config
from .db import BASE_TEMP_C, get_db

MIN_TEMP_MINUTES = 600  # minder buitentemperatuur gemeten op een dag: geen graaddagen voor die dag


def degree_days(outdoor_mean: float | None) -> float | None:
    if outdoor_mean is None:
        return None
    return max(0.0, BASE_TEMP_C - outdoor_mean)


def gas_base_m3_day() -> float:
    """Gas per dag voor douchen en koken. In de app in te stellen, anders GAS_BASE_M3_PER_DAY."""
    v = get_db().get_kv("gas_base_m3_day")
    try:
        return max(0.0, float(v)) if v not in (None, "") else max(0.0, config.gas_base_m3_per_day)
    except ValueError:
        return 0.0


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def gas_per_day(periods: list[dict[str, Any]]) -> dict[str, float]:
    """Verdeelt elke ingevulde periode gelijk over de dagen (start t/m end)."""
    out: dict[str, float] = {}
    for p in periods:
        s, e = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        n = (e - s).days + 1
        if n <= 0:
            continue
        for i in range(n):
            k = (s + timedelta(days=i)).isoformat()
            out[k] = out.get(k, 0.0) + float(p["m3"]) / n
    return out


def _r(v: float | None, n: int = 1) -> float | None:
    return None if v is None else round(v, n)


def summarize_week(start: date, days: list[dict[str, Any]], gas: dict[str, float], today: date,
                   base: float = 0.0) -> dict[str, Any]:
    by_day = {r["day"]: r for r in days}
    n_days = min(7, (today - start).days + 1)
    keys = [(start + timedelta(days=i)).isoformat() for i in range(n_days)]

    hp_wh = heat_wh = cop_hp_wh = cop_heat_wh = supply_w = gd = gd_hp_wh = 0.0
    gas_m3 = gd_gas = gd_gas_m3 = 0.0
    gd_gas_days = 0
    hp_cost = 0.0
    priced = minutes = gas_days = temp_days = 0
    rooms: list[float] = []
    for k in keys:
        r = by_day.get(k) or {}
        h, q = float(r.get("hp_wh") or 0.0), float(r.get("heat_wh") or 0.0)
        hp_wh += h
        heat_wh += q
        if q > 0:  # COP alleen over dagen waarop de warmte gemeten is
            cop_hp_wh += h
            cop_heat_wh += q
            if r.get("supply_temp") is not None:
                supply_w += float(r["supply_temp"]) * q
        hp_cost += float(r.get("hp_cost") or 0.0)
        priced += int(r.get("priced_minutes") or 0)
        minutes += int(r.get("minutes") or 0)
        if r.get("room_temp") is not None:
            rooms.append(float(r["room_temp"]))
        g = degree_days(r.get("outdoor_temp")) if (r.get("outdoor_minutes") or 0) >= MIN_TEMP_MINUTES else None
        if g is not None:
            temp_days += 1
            gd += g
            gd_hp_wh += h
        if k in gas:
            gas_days += 1
            gas_m3 += gas[k]
            if g is not None:
                gd_gas += g
                gd_gas_m3 += gas[k]
                gd_gas_days += 1

    return {
        "week_start": start.isoformat(),
        "days": n_days,
        "temp_days": temp_days,
        "gas_days": gas_days,
        "degree_days": _r(gd) if temp_days else None,
        "outdoor_temp": _r(sum(float(by_day[k]["outdoor_temp"]) for k in keys
                               if k in by_day and by_day[k].get("outdoor_temp") is not None) / temp_days)
        if temp_days else None,
        "hp_kwh": _r(hp_wh / 1000),
        "heat_kwh": _r(heat_wh / 1000) if heat_wh else None,
        "cop": _r(cop_heat_wh / cop_hp_wh, 2) if cop_hp_wh > 0 and cop_heat_wh > 0 else None,
        "supply_temp": _r(supply_w / cop_heat_wh) if cop_heat_wh > 0 and supply_w > 0 else None,
        "room_temp": _r(sum(rooms) / len(rooms)) if rooms else None,
        "gas_m3": _r(gas_m3, 2) if gas_days else None,
        "gas_heating_m3": _r(max(gas_m3 - base * gas_days, 0.0), 2) if gas_days else None,
        "hp_kwh_per_dd": _r(gd_hp_wh / 1000 / gd, 2) if gd > 0.5 else None,
        "gas_m3_per_dd": _r(max(gd_gas_m3 - base * gd_gas_days, 0.0) / gd_gas, 3) if gd_gas > 0.5 else None,
        "hp_cost": _r(hp_cost, 2) if minutes and priced >= 0.9 * minutes else None,
        "note": get_db().get_kv(f"heating_note_{start.isoformat()}") or "",
    }


def weeks(today: date, n: int = 10) -> list[dict[str, Any]]:
    """De laatste n weken, nieuwste eerst; de lopende week telt tot en met vandaag."""
    db = get_db()
    first = week_start(today) - timedelta(weeks=n - 1)
    days = db.heating_days(first.isoformat(), today.isoformat())
    gas = gas_per_day(db.gas_periods(limit=1000))
    base = gas_base_m3_day()
    out = []
    for i in range(n):
        s = first + timedelta(weeks=i)
        wd = [r for r in days if s.isoformat() <= r["day"] < (s + timedelta(days=7)).isoformat()]
        if not wd and not any(s.isoformat() <= k < (s + timedelta(days=7)).isoformat() for k in gas):
            continue
        out.append(summarize_week(s, wd, gas, today, base))
    return list(reversed(out))
