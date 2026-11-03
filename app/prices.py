"""Dynamische stroomprijzen: opslaan, afgeleide info (niveau, goedkoopste blok) en planner.

De prijzen zelf komen van een integratie met rol 'prices' (zie app/integrations,
bv. frank.py). Die levert per dag rijen (kwartier_ts, lokale_dag, marktprijs, all_in)
in EUR/kWh. Teruglevering = marktprijs x EXPORT_MULTIPLIER + EXPORT_FEE_EUR.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from . import mailer
from .config import config
from .db import get_db

log = logging.getLogger(__name__)

QUARTER = 900


# ---------------- afgeleide info voor de app ----------------


def export_price(market: float) -> float:
    return market * config.export_multiplier + config.export_fee_eur


def windows(rows: list[dict[str, Any]], pred) -> list[dict[str, Any]]:
    """Aaneengesloten kwartierblokken waarvoor pred(row) waar is."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if pred(r):
            if out and out[-1]["end"] == r["ts"]:
                out[-1]["end"] = r["ts"] + QUARTER
                out[-1]["rows"].append(r)
            else:
                out.append({"start": r["ts"], "end": r["ts"] + QUARTER, "rows": [r]})
    for w in out:
        w["min_export"] = round(min(x["export"] for x in w.pop("rows")), 4)
    return out


def cheapest_block(rows: list[dict[str, Any]], quarters: int = 8) -> dict[str, Any] | None:
    best = None
    for i in range(0, len(rows) - quarters + 1):
        block = rows[i : i + quarters]
        if block[-1]["ts"] - block[0]["ts"] != (quarters - 1) * QUARTER:
            continue
        avg = sum(r["all_in"] for r in block) / quarters
        if best is None or avg < best["avg"]:
            best = {"start": block[0]["ts"], "end": block[-1]["ts"] + QUARTER, "avg": round(avg, 4)}
    return best


def price_info(now: float | None = None) -> dict[str, Any] | None:
    now = now or time.time()
    tz = config.tz
    today = datetime.fromtimestamp(now, tz).date()
    start = int(datetime.combine(today, datetime.min.time(), tzinfo=tz).timestamp())
    end = int(datetime.combine(today + timedelta(days=2), datetime.min.time(), tzinfo=tz).timestamp())
    rows = get_db().prices_between(start, end)
    if not rows:
        return None
    cur_ts = int(now) - int(now) % QUARTER
    current = next((r for r in rows if r["ts"] == cur_ts), None)
    today_rows = [r for r in rows if r["day"] == today.isoformat()]
    upcoming = [r for r in rows if r["ts"] >= cur_ts]

    level = None
    if current and len(today_rows) >= 8:
        vals = sorted(r["all_in"] for r in today_rows)
        lo, hi = vals[len(vals) // 3], vals[(2 * len(vals)) // 3]
        level = "goedkoop" if current["all_in"] <= lo else "duur" if current["all_in"] >= hi else "gemiddeld"

    def extreme(fn):
        rest = [r for r in today_rows if r["ts"] >= cur_ts]
        if not rest:
            return None
        r = fn(rest, key=lambda x: x["all_in"])
        return {"ts": r["ts"], "all_in": r["all_in"]}

    return {
        "now": None if current is None else {**current, "level": level},
        "today_avg": round(statistics.mean(r["all_in"] for r in today_rows), 4) if today_rows else None,
        "next_low": extreme(min),
        "next_high": extreme(max),
        "cheapest_2h": cheapest_block(upcoming, 8),
        "negative": windows(upcoming, lambda r: r["export"] < 0),
        "has_tomorrow": any(r["day"] != today.isoformat() for r in rows),
    }


# ---------------- ophalen ----------------


async def _store_day(provider: Any, session: aiohttp.ClientSession, day: date) -> int:
    rows = await provider.fetch_day(session, day)
    if rows:
        get_db().save_prices(rows)
    return len(rows)


async def _negative_mail(day: date) -> None:
    db = get_db()
    key = f"neg_mail_{day.isoformat()}"
    if db.get_kv(key):
        return
    tz = config.tz
    start = int(datetime.combine(day, datetime.min.time(), tzinfo=tz).timestamp())
    rows = db.prices_between(start, start + 86400 + 3600)
    rows = [r for r in rows if r["day"] == day.isoformat()]
    neg = windows(rows, lambda r: r["export"] < 0)
    db.set_kv(key, "1")
    if not neg or not config.price_negative_mail:
        return
    fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M")  # noqa: E731
    lines = [f"  {fmt(w['start'])} - {fmt(w['end'])} (terugleveren kost tot {abs(w['min_export']) * 100:.1f} cent per kWh)" for w in neg]
    from . import push

    await push.broadcast(
        "negative",
        f"💸 Morgen negatieve stroomprijs ({fmt(neg[0]['start'])}–{fmt(neg[-1]['end'])})",
        "Stroom gebruiken levert dan geld op en terugleveren kost geld. Een goed moment voor de wasmachine, "
        "droger of vaatwasser.",
        url="/#nu",
        tag="negative",
    )
    await mailer.send(
        config.split(config.alert_to),
        f"Morgen negatieve stroomprijs ({fmt(neg[0]['start'])} - {fmt(neg[-1]['end'])})",
        "Morgen is de stroomprijs op deze momenten negatief:\n" + "\n".join(lines) + "\n\n"
        "Terugleveren kost dan geld en stroom gebruiken levert juist iets op. Een goed moment voor de "
        "wasmachine, droger of vaatwasser. Zorg dat de batterij dan ruimte heeft om te laden."
        + mailer.footer(),
    )


def ensure_source(source: str) -> None:
    """Wis opgeslagen prijzen als ze uit een andere bron komen (bv. demo -> echt)."""
    db = get_db()
    if db.get_kv("price_source") != source:
        db.x("DELETE FROM price")
        db.set_kv("price_source", source)
        log.info("Opgeslagen prijzen gewist (bron is nu %s)", source)


async def run(provider: Any) -> None:
    db = get_db()
    tz = config.tz
    ensure_source(getattr(provider, "source", type(provider).__name__))
    last_refresh: dict[str, float] = {}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Geschiedenis aanvullen zodat ook oudere dagen in euro's te zien zijn.
        try:
            if config.demo:  # wacht tot de nepgeschiedenis klaarstaat
                for _ in range(120):
                    if db.first_day():
                        break
                    await asyncio.sleep(1)
            today = datetime.now(tz).date()
            first = db.first_day()
            start = max(today - timedelta(days=config.price_backfill_days), date.fromisoformat(first) if first else today)
            d, n = start, 0
            while d <= today:
                if db.price_day_count(d.isoformat()) < 92:
                    await _store_day(provider, session, d)
                    n += 1
                    if not config.demo:
                        await asyncio.sleep(1)
                d += timedelta(days=1)
            if n:
                log.info("Stroomprijzen opgehaald voor %d dagen", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Prijsgeschiedenis ophalen mislukt: %s", exc)

        while True:
            try:
                now = datetime.now(tz)
                today, tomorrow = now.date(), now.date() + timedelta(days=1)
                stale = lambda d: time.time() - last_refresh.get(d.isoformat(), 0) > 3 * 3600  # noqa: E731
                # Vandaag en morgen elke 3 uur opnieuw ophalen, zodat correcties van Frank doorkomen.
                if db.price_day_count(today.isoformat()) < 92 or stale(today):
                    await _store_day(provider, session, today)
                    last_refresh[today.isoformat()] = time.time()
                # Prijzen voor morgen komen rond 13:00 beschikbaar.
                if now.hour >= 13 and (db.price_day_count(tomorrow.isoformat()) < 92 or stale(tomorrow)):
                    if await _store_day(provider, session, tomorrow):
                        last_refresh[tomorrow.isoformat()] = time.time()
                if db.price_day_count(tomorrow.isoformat()) >= 92:
                    await _negative_mail(tomorrow)
                db.set_kv("price_error", "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Stroomprijzen ophalen mislukt: %s", exc)
                db.set_kv("price_error", str(exc))
            await asyncio.sleep(15 * 60)
