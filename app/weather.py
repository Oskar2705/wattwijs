"""Gemiddelde buitentemperatuur per dag van Open-Meteo, voor de graaddagen.

Waarom niet de sensor van de warmtepomp? Die meet pas sinds deze versie, en hangt vlak bij de
buitenunit (zon, ontdooien). Voor een eerlijke vergelijking tussen weken telt vooral dat élke week
dezelfde bron gebruikt. Open-Meteo levert tot 92 dagen terug, dus ook de weken van vóór het meten.
Zonder LATITUDE/LONGITUDE valt het weekoverzicht terug op de sensor van de warmtepomp.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

import aiohttp

from .config import config
from .db import get_db

log = logging.getLogger(__name__)
URL = "https://api.open-meteo.com/v1/forecast"
PAST_DAYS = 92  # het maximum van de gewone (gratis) API
REFRESH_S = 6 * 3600


def enabled() -> bool:
    return config.latitude is not None and config.longitude is not None


def parse_daily(data: dict[str, Any], today: date) -> list[tuple[str, float]]:
    """Uurtemperaturen (lokale tijd) -> [(dag, gemiddelde)], alleen dagen t/m vandaag met
    minstens 20 uurwaarden. Vandaag telt deels als verwachting en wordt later bijgewerkt."""
    hourly = data.get("hourly") or {}
    per_day: dict[str, list[float]] = {}
    for t, v in zip(hourly.get("time") or [], hourly.get("temperature_2m") or []):
        if v is None:
            continue
        per_day.setdefault(str(t)[:10], []).append(float(v))
    return [(d, round(sum(vs) / len(vs), 2)) for d, vs in sorted(per_day.items())
            if d <= today.isoformat() and len(vs) >= 20]


def today_temp() -> float | None:
    """Gemiddelde buitentemperatuur van vandaag (deels verwachting), of None."""
    d = datetime.now(config.tz).date().isoformat()
    return get_db().weather_days(d, d).get(d)


async def fetch(session: aiohttp.ClientSession) -> dict[str, Any]:
    params = {
        "latitude": f"{config.latitude:.4f}",
        "longitude": f"{config.longitude:.4f}",
        "hourly": "temperature_2m",
        "past_days": str(PAST_DAYS),
        "forecast_days": "1",
        "timezone": config.tz_name,
    }
    async with session.get(URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        return await r.json()


async def refresh_once(session: aiohttp.ClientSession, today: date) -> int:
    rows = parse_daily(await fetch(session), today)
    get_db().save_weather_days(rows, "open-meteo")
    return len(rows)


async def run() -> None:
    """Bij de start en daarna elke 6 uur; de laatste dagen worden zo nog bijgewerkt."""
    if not enabled():
        return
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                n = await refresh_once(session, datetime.now(config.tz).date())
                log.info("Buitentemperatuur per dag bijgewerkt (%d dagen, Open-Meteo)", n)
                await asyncio.sleep(REFRESH_S)
            except Exception as exc:  # noqa: BLE001
                log.warning("Buitentemperatuur ophalen mislukt: %s", exc)
                await asyncio.sleep(1800)
