"""Zonverwachting voor vandaag: wanneer is er naar verwachting stroom over?

Bron: Open-Meteo (gratis, geen account), uurwaarden van de zonnestraling
(`shortwave_radiation`, W/m², gemiddelde over het voorgaande uur) voor LATITUDE/LONGITUDE.

We rekenen de straling niet om met paneelgegevens. In plaats daarvan leren we uit de
eigen geschiedenis hoeveel deze installatie per uur opwekt bij een bepaalde straling
(per uur van de dag, zodat de ligging van het dak en vaste schaduw meetellen). Daarvan
trekken we het gebruikelijke verbruik van het huis op dat uur af: wat overblijft is het
verwachte overschot.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime, time as dtime
from typing import Any

import aiohttp

from .config import config
from .db import consumption_wh, get_db

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
HOUR = 3600
PAST_DAYS = 21
KV_KEY = "solar_forecast"
REFRESH_S = 3600
MIN_RAD = 50.0  # W/m²: daaronder zegt een uur weinig over de installatie
MIN_SAMPLES = 3  # per uur van de dag, anders de gemiddelde factor
MIN_MINUTES = 50  # een uur telt alleen mee als het (bijna) helemaal is gemeten


def enabled() -> bool:
    return config.latitude is not None and config.longitude is not None


# ---------------- Open-Meteo ----------------


def parse_open_meteo(data: dict[str, Any], key: str = "shortwave_radiation") -> dict[int, float]:
    """Antwoord -> {begin van het uur (unix): waarde}. Open-Meteo geeft het einde van het uur.

    `shortwave_radiation` is in W/m², `sunshine_duration` in seconden zon in dat uur."""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    vals = hourly.get(key) or []
    out: dict[int, float] = {}
    for t, v in zip(times, vals):
        if v is None:
            continue
        out[int(t) - HOUR] = max(0.0, float(v))
    return out


async def fetch(session: aiohttp.ClientSession) -> dict[str, dict[int, float]]:
    params = {
        "latitude": f"{config.latitude:.4f}",
        "longitude": f"{config.longitude:.4f}",
        "hourly": "shortwave_radiation,sunshine_duration",
        "past_days": str(PAST_DAYS),
        "forecast_days": "2",
        "timeformat": "unixtime",
        "timezone": "UTC",
    }
    async with session.get(URL, params=params) as r:
        r.raise_for_status()
        data = await r.json()
    return {"rad": parse_open_meteo(data), "sun": parse_open_meteo(data, "sunshine_duration")}


# ---------------- leren uit de eigen geschiedenis ----------------


def hourly_actuals(start: int, end: int) -> dict[int, dict[str, float]]:
    """{begin van het uur: {"solar_wh", "house_wh", "minutes"}} uit de minutentabel."""
    rows = get_db().q(
        "SELECT (ts/3600)*3600 AS h, COUNT(*) AS n, SUM(import_wh) AS import_wh, SUM(export_wh) AS export_wh,"
        " SUM(solar_wh) AS solar_wh, SUM(charge_wh) AS charge_wh, SUM(discharge_wh) AS discharge_wh,"
        " SUM(house_wh) AS house_wh FROM minute WHERE ts >= ? AND ts < ? GROUP BY h",
        (start, end),
    )
    return {int(r["h"]): {"solar_wh": float(r["solar_wh"] or 0.0), "house_wh": consumption_wh(r),
                          "minutes": int(r["n"])} for r in rows}


def local_hour(ts: int) -> int:
    return datetime.fromtimestamp(ts, config.tz).hour


def calibrate(rad: dict[int, float], actual: dict[int, dict[str, float]]) -> dict[str, Any] | None:
    """Opwek per W/m² straling, per uur van de dag. None als er te weinig geschiedenis is."""
    per_hour: dict[int, list[tuple[float, float]]] = {}
    peak = 0.0
    for h, a in actual.items():
        if a["minutes"] < MIN_MINUTES:
            continue
        peak = max(peak, a["solar_wh"])
        r = rad.get(h)
        if r is None or r < MIN_RAD:
            continue
        per_hour.setdefault(local_hour(h), []).append((a["solar_wh"], r))
    pairs = [p for v in per_hour.values() for p in v]
    if len(pairs) < 10:
        return None
    total = sum(r for _, r in pairs)
    overall = sum(w for w, _ in pairs) / total if total else 0.0
    factors = {hr: sum(w for w, _ in v) / sum(r for _, r in v)
               for hr, v in per_hour.items() if len(v) >= MIN_SAMPLES and sum(r for _, r in v) > 0}
    return {"overall": overall, "factors": factors, "peak_w": peak}


def baseline(actual: dict[int, dict[str, float]]) -> dict[int, float]:
    """Gebruikelijk verbruik (mediaan, W) per uur van de dag."""
    per_hour: dict[int, list[float]] = {}
    for h, a in actual.items():
        if a["minutes"] >= MIN_MINUTES:
            per_hour.setdefault(local_hour(h), []).append(a["house_wh"])
    return {hr: statistics.median(v) for hr, v in per_hour.items()}


def predict(rad: dict[int, float], cal: dict[str, Any], base: dict[int, float],
            start: int, end: int) -> list[dict[str, Any]]:
    """Per uur tussen start en end: verwachte opwek, verbruik en overschot (W)."""
    fallback = statistics.median(base.values()) if base else 0.0
    out = []
    for h in sorted(t for t in rad if start <= t < end):
        hr = local_hour(h)
        solar = rad[h] * cal["factors"].get(hr, cal["overall"])
        if cal.get("peak_w"):
            solar = min(solar, cal["peak_w"] * 1.1)  # nooit meer dan de installatie ooit deed
        house = base.get(hr, fallback)
        out.append({"start": h, "solar_w": round(solar), "house_w": round(house), "surplus_w": round(solar - house)})
    return out


def best_block(hours: list[dict[str, Any]], length: int, min_surplus_w: float) -> dict[str, Any] | None:
    """Aaneengesloten blok van `length` uur met het grootste gemiddelde overschot (of None)."""
    best = None
    for i in range(len(hours) - length + 1):
        blk = hours[i:i + length]
        if any(blk[j + 1]["start"] - blk[j]["start"] != HOUR for j in range(length - 1)):
            continue
        avg = sum(h["surplus_w"] for h in blk) / length
        if best is None or avg > best["surplus_w"]:
            best = {"start": blk[0]["start"], "end": blk[-1]["start"] + HOUR, "surplus_w": round(avg),
                    "solar_w": round(sum(h["solar_w"] for h in blk) / length)}
    if best is None or best["surplus_w"] < min_surplus_w:
        return None
    return best


# ---------------- samen ----------------


def _load(cached: dict[str, Any]) -> dict[str, dict[int, float]]:
    return {k: {int(t): v for t, v in (cached.get(k) or {}).items()} for k in ("rad", "sun")}


async def refresh(force: bool = False) -> dict[str, dict[int, float]] | None:
    """Haalt straling en zonuren op (hooguit één keer per uur) en bewaart ze in de database."""
    if not enabled():
        return None
    db = get_db()
    cached = None
    try:
        cached = json.loads(db.get_kv(KV_KEY) or "null")
    except ValueError:
        cached = None
    fresh = cached and "sun" in cached and time.time() - cached.get("at", 0) < REFRESH_S
    if fresh and not force:
        return _load(cached)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            data = await fetch(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("Zonverwachting ophalen mislukt: %s", exc)
        return _load(cached) if cached else None
    db.set_kv(KV_KEY, json.dumps({"at": time.time(), **data}))
    return data


def _window(now: datetime) -> tuple[int, int]:
    start_s, end_s = config.push_block_window.split("-")
    d = now.date()
    w_start = int(datetime.combine(d, dtime(*map(int, start_s.split(":"))), tzinfo=config.tz).timestamp())
    w_end = int(datetime.combine(d, dtime(*map(int, end_s.split(":"))), tzinfo=config.tz).timestamp())
    return w_start, w_end


def sun_window(sun: dict[int, float], d0: int, d1: int, min_s: float = 900) -> dict[str, int] | None:
    """Van het eerste tot het laatste uur met minstens een kwartier zon, of None."""
    hours = sorted(t for t, v in sun.items() if d0 <= t < d1 and v >= min_s)
    return {"start": hours[0], "end": hours[-1] + HOUR} if hours else None


def outlook(data: dict[str, dict[int, float]], now: datetime) -> dict[str, Any]:
    """Verwachting voor vandaag: zonuren, opwek (kWh) en het blok met het meeste overschot.

    `solar_kwh` en `block` zijn None als er nog te weinig eigen geschiedenis is om te ijken."""
    rad, sun = data.get("rad") or {}, data.get("sun") or {}
    nowts = int(now.timestamp())
    d0 = int(datetime.combine(now.date(), dtime.min, tzinfo=config.tz).timestamp())
    d1 = d0 + 24 * HOUR  # (op de dagen van de zomertijdwissel klopt dat op een uur na; geen bezwaar)
    out: dict[str, Any] = {
        "sun_hours": round(sum(v for t, v in sun.items() if d0 <= t < d1) / 3600, 1) if sun else None,
        "sun_window": sun_window(sun, d0, d1),
        "solar_kwh": None,
        "block": None,
    }
    actual = hourly_actuals(nowts - PAST_DAYS * 86400, nowts - nowts % HOUR)
    cal = calibrate(rad, actual)
    if cal is None:
        return out
    base = baseline(actual)
    day = predict(rad, cal, base, d0, d1)
    out["solar_kwh"] = round(sum(h["solar_w"] for h in day) / 1000, 1)
    out["hours"] = [{"start": h["start"], "solar_w": h["solar_w"]} for h in day]  # voor het dagrapport
    w_start, w_end = _window(now)
    hours = [h for h in day if max(w_start, nowts - nowts % HOUR) <= h["start"] < w_end]
    out["block"] = best_block(hours, max(1, round(config.push_block_hours)), config.solar_tip_min_w)
    return out


def plan_today(rad: dict[int, float], now: datetime) -> dict[str, Any] | None:
    """Het zonnigste blok van vandaag, binnen PUSH_BLOCK_WINDOW en vanaf nu."""
    return outlook({"rad": rad}, now)["block"]


async def day_outlook(now: datetime) -> dict[str, Any] | None:
    """Voor het ochtendbericht. Bewaart de verwachte opwek, zodat het avondoverzicht kan vergelijken."""
    if not enabled():
        return None
    data = await refresh()
    if not data:
        return None
    try:
        o = outlook(data, now)
    except Exception:  # noqa: BLE001
        log.exception("Zonverwachting berekenen mislukt")
        return None
    log.info("Zonverwachting: %s zonuren, %s kWh, blok %s", o["sun_hours"], o["solar_kwh"],
             f"{datetime.fromtimestamp(o['block']['start'], config.tz):%H:%M} ({o['block']['surplus_w']} W over)"
             if o["block"] else "-")
    db = get_db()
    if o["solar_kwh"] is not None:
        db.set_kv(f"solar_expected_{now.date().isoformat()}", str(o["solar_kwh"]))
    db.set_kv(f"outlook_{now.date().isoformat()}", json.dumps(o))  # het dagrapport laat de verwachting zien
    return o


def saved_outlook(day: str) -> dict[str, Any] | None:
    """De verwachting zoals die 's ochtends voor deze dag werd gemaakt (of None)."""
    try:
        return json.loads(get_db().get_kv(f"outlook_{day}") or "null")
    except ValueError:
        return None


async def sun_block(now: datetime) -> dict[str, Any] | None:
    o = await day_outlook(now)
    return o["block"] if o else None
