"""Planner en teksten voor de pushmeldingen (ochtendtip, avondrecords, weekoverzicht)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from . import push
from .config import config
from .db import consumption_wh, get_db, self_sufficiency
from .prices import QUARTER, cheapest_block, windows

log = logging.getLogger(__name__)

DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


# ---------------- opmaak ----------------


def cent(v: float) -> str:
    c = v * 100
    s = f"{c:.1f}" if abs(c) < 10 else f"{c:.0f}"
    return s.replace(".", ",") + " cent"


def kwh(wh: float) -> str:
    return f"{wh / 1000:.1f}".replace(".", ",") + " kWh"


def eur(v: float) -> str:
    s = f"{abs(v):.2f}".replace(".", ",")
    return f"-€ {s}" if v < 0 else f"€ {s}"


def hm(ts: int) -> str:
    return datetime.fromtimestamp(ts, config.tz).strftime("%H:%M")


def _hhmm(text: str) -> tuple[int, int]:
    h, m = text.strip().split(":")
    return int(h), int(m)


def _day_bounds(d: date) -> tuple[int, int]:
    start = int(datetime.combine(d, dtime.min, tzinfo=config.tz).timestamp())
    end = int(datetime.combine(d + timedelta(days=1), dtime.min, tzinfo=config.tz).timestamp())
    return start, end


def _totals(day: str) -> dict[str, Any]:
    t = get_db().totals_for_day(day)
    t["house_wh"] = consumption_wh(t)
    t["self_sufficiency"] = self_sufficiency(t, min_wh=100)
    return t


# ---------------- ochtend ----------------


def best_block_today(now: datetime) -> dict[str, Any] | None:
    """Goedkoopste aaneengesloten blok binnen het venster (bv. 07:00-23:00), vanaf nu."""
    start_s, end_s = config.push_block_window.split("-")
    d = now.date()
    w_start = datetime.combine(d, dtime(*_hhmm(start_s)), tzinfo=config.tz)
    w_end = datetime.combine(d, dtime(*_hhmm(end_s)), tzinfo=config.tz)
    frm = max(int(w_start.timestamp()), int(now.timestamp()) - int(now.timestamp()) % QUARTER)
    rows = get_db().prices_between(frm, int(w_end.timestamp()))
    q = max(1, int(round(config.push_block_hours * 4)))
    return cheapest_block(rows, q)


def build_morning(now: datetime) -> tuple[str, str] | None:
    db = get_db()
    today = now.date()
    lines: list[str] = []
    title = "Goedemorgen"

    s, e = _day_bounds(today)
    rows = db.prices_between(s, e)
    block = best_block_today(now) if rows else None
    if block:
        title = f"Vandaag het goedkoopst: {hm(block['start'])}–{hm(block['end'])}"
        rest = [r for r in rows if r["ts"] >= int(now.timestamp()) - QUARTER]
        hi = max(rest, key=lambda r: r["all_in"]) if rest else None
        line = f"Beste moment voor vaatwasser of wasmachine: {hm(block['start'])}–{hm(block['end'])}, gemiddeld {cent(block['avg'])} per kWh."
        if hi:
            line += f" Duurst rond {hm(hi['ts'])} ({cent(hi['all_in'])})."
        lines.append(line)
        neg = windows([r for r in rows if r["ts"] >= int(now.timestamp()) - QUARTER], lambda r: r["export"] < 0)
        for w in neg:
            lines.append(f"Tussen {hm(w['start'])} en {hm(w['end'])} is de prijs negatief: stroom gebruiken levert dan zelfs iets op.")

    y = (today - timedelta(days=1)).isoformat()
    t = _totals(y)
    if (t.get("minutes") or 0) > 600:
        parts = [f"{kwh(t['solar_wh'])} opgewekt"]
        if t["self_sufficiency"] is not None:
            parts.append(f"{t['self_sufficiency']:.0f}% zelf voorzien")
        if t.get("priced_minutes"):
            net = (t.get("cost_import") or 0) - (t.get("revenue_export") or 0)
            parts.append(f"{eur(net)} aan stroom")
        lines.append("Gisteren: " + ", ".join(parts) + ".")

    for a in db.open_alerts():
        name = a["name"] or a["serial"]
        lines.append(f"⚠ Zonnepaneel {name} doet het nog steeds niet goed.")

    if not lines:
        return None
    return title, "\n".join(lines)


# ---------------- avond ----------------


def build_evening(day: date) -> tuple[str, str] | None:
    """Dagrecord, (bijna) zelfvoorzienende dag of mijlpaal. Hooguit één bericht."""
    db = get_db()
    d = day.isoformat()
    t = _totals(d)
    if (t.get("minutes") or 0) < 600:
        return None
    hist = db.q(
        "SELECT day, SUM(solar_wh) AS s FROM minute WHERE day < ? GROUP BY day HAVING COUNT(*) > 600", (d,)
    )
    solar = t.get("solar_wh") or 0.0
    if len(hist) >= 14 and solar > 500 and solar > max(h["s"] or 0 for h in hist):
        return "☀ Nieuw record!", f"Vandaag {kwh(solar)} opgewekt: de beste dag sinds de meting begon."

    if t["self_sufficiency"] is not None and t["self_sufficiency"] >= 95:
        return "🏡 Bijna helemaal zelfvoorzienend", (
            f"Vandaag kwam {t['self_sufficiency']:.0f}% van de stroom van eigen zon of batterij."
        )

    total = db.q("SELECT SUM(solar_wh) AS s FROM minute")[0]["s"] or 0.0
    before = total - solar
    step = 500_000  # elke 500 kWh
    if total >= step and int(before // step) < int(total // step):
        mijl = int(total // step) * step
        return "🎉 Mijlpaal", f"Sinds de meting begon, hebben de panelen samen {mijl / 1000:.0f} kWh opgewekt."
    return None


# ---------------- week ----------------


def build_weekly(day: date) -> tuple[str, str] | None:
    db = get_db()
    start = day - timedelta(days=6)

    def week(s: date, e: date) -> dict[str, Any]:
        r = db.q(
            "SELECT SUM(solar_wh) solar_wh, SUM(house_wh) house_wh, SUM(import_wh) import_wh, "
            "SUM(export_wh) export_wh, SUM(charge_wh) charge_wh, SUM(discharge_wh) discharge_wh, COUNT(*) n "
            "FROM minute WHERE day BETWEEN ? AND ?",
            (s.isoformat(), e.isoformat()),
        )[0]
        r = {k: (v or 0.0) for k, v in r.items()}
        return {"s": r["solar_wh"], "h": consumption_wh(r), "i": r["import_wh"], "n": r["n"]}

    w = week(start, day)
    if w["n"] < 5 * 1440:  # minstens 5 dagen gemeten
        return None
    prev = week(start - timedelta(days=7), start - timedelta(days=1))
    lines = [f"Opgewekt {kwh(w['s'])}, verbruikt {kwh(w['h'])}."]
    if w["h"] > 0:
        lines.append(f"{max(0.0, 1 - w['i'] / w['h']) * 100:.0f}% kwam van eigen zon of batterij.")
    if prev["n"] >= 6 * 1440 and prev["s"] > 0:  # vorige week bijna compleet
        diff = (w["s"] - prev["s"]) / prev["s"] * 100
        if abs(diff) < 3:
            lines.append("Ongeveer evenveel zon als vorige week.")
        else:
            lines.append(f"Dat is {abs(diff):.0f}% {'meer' if diff > 0 else 'minder'} zon dan vorige week.")
    days = db.q(
        "SELECT day, SUM(solar_wh) s FROM minute WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY s DESC LIMIT 1",
        (start.isoformat(), day.isoformat()),
    )
    if days and days[0]["s"]:
        best = date.fromisoformat(days[0]["day"])
        lines.append(f"Zonnigste dag: {DAGEN[best.weekday()]} ({kwh(days[0]['s'])}).")
    return "📊 Jouw energieweek", " ".join(lines)


# ---------------- planner ----------------


async def _once(key: str, fn) -> None:
    """Voer fn één keer per sleutel uit (bv. per dag), ook na een herstart."""
    db = get_db()
    if db.get_kv(key):
        return
    db.set_kv(key, "1")
    await fn()


async def run() -> None:
    while True:
        try:
            now = datetime.now(config.tz)
            today = now.date()
            hhmm = (now.hour, now.minute)

            if hhmm >= _hhmm(config.push_morning_time) and now.hour < 12:
                async def morning() -> None:
                    msg = build_morning(now)
                    if msg:
                        await push.broadcast("morning", *msg, url="/#nu", tag="morning")
                await _once(f"push_morning_{today}", morning)

            if hhmm >= _hhmm(config.push_evening_time):
                async def evening() -> None:
                    msg = build_evening(today)
                    if msg:
                        await push.broadcast("records", *msg, url="/#geschiedenis", tag="records")
                await _once(f"push_evening_{today}", evening)

            if today.weekday() == 6 and hhmm >= _hhmm(config.push_weekly_time):
                async def weekly() -> None:
                    msg = build_weekly(today)
                    if msg:
                        await push.broadcast("weekly", *msg, url="/#geschiedenis", tag="weekly")
                await _once(f"push_weekly_{today}", weekly)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Pushplanner")
        await asyncio.sleep(60)
