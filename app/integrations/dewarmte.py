"""DeWarmte warmtepomp via de (onofficiële) cloud-API van mydewarmte.com.

Zelfde endpoints als de Home Assistant-integratie ronald-willems/dewarmte-homeassistant:
  POST /v1/auth/token/          {"email", "password"} -> {"access"}
  GET  /v1/customer/products/   -> results[] met status (vermogens in kW)
  GET  /v1/customer/products/{id}/insights/?timespan=hourly&start_date=JJJJ-MM-DD
                                -> data[] per uur: key "HH:00", heat_supplied, electricity_consumed (kWh)

De warmtepomp meet warmte en stroom zelf; die uurwaarden halen we op (ook terug tot de
installatie), zodat het weekoverzicht de COP ook kent voor de tijd vóór Wattwijs ze opsloeg.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..config import config
from ..db import get_db
from ..state import Source
from . import register

log = logging.getLogger(__name__)
BASE = "https://api.mydewarmte.com/v1"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://mydewarmte.com",
    "Referer": "https://mydewarmte.com/",
}


def _num(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def parse_product(product: dict[str, Any]) -> dict[str, Any]:
    s = {**product, **(product.get("status") or {})}
    kw = _num(s.get("electricity_consumption"))
    heat = _num(s.get("heat_output"))
    return {
        "power_w": None if kw is None else kw * 1000.0,
        "heat_output_w": None if heat is None else heat * 1000.0,
        "supply_temperature": _num(s.get("supply_temperature")),
        "outdoor_temperature": _num(s.get("outdoor_temperature")),
        "is_on": s.get("is_on"),
        "fault_code": s.get("fault_code"),
        "product_name": product.get("name"),
    }


# ---------------- geschiedenis per uur (insights) ----------------

API_TZ = ZoneInfo("Europe/Amsterdam")  # de labels zijn Nederlandse kloktijd
HISTORY_DAYS = 120  # zo ver terug kijken bij de eerste keer
HISTORY_EVERY_S = 3600
HOUR = timedelta(hours=1)


def _midnight_utc(day: date) -> datetime:
    return datetime.combine(day, dtime.min, tzinfo=API_TZ).astimezone(timezone.utc)


def parse_insights(day: date, payload: dict[str, Any]) -> list[tuple[int, float, float, float]]:
    """Antwoord voor één dag -> [(begin van het uur in UTC, warmte, stroom, bijverwarming)] in kWh.

    Een label noemt het uur waarop een vak SLUIT: "08:00" is 07:00-08:00 (zo nagemeten door de
    Home Assistant-integratie). De API laat uren zonder gegevens weg, en op de dag van de
    wintertijd komt "02:00" twee keer voor; daarom lopen we de echte uren van de dag langs en
    koppelen we op label, in volgorde."""
    hours = []
    cur, end = _midnight_utc(day), _midnight_utc(day + timedelta(days=1))
    while cur < end:
        hours.append((cur, cur.astimezone(API_TZ).strftime("%H:00")))
        cur += HOUR
    out, pos = [], 0
    num = lambda v: float(v or 0.0)  # noqa: E731
    for b in payload.get("data") or []:
        while pos < len(hours) and hours[pos][1] != b.get("key"):
            pos += 1
        if pos >= len(hours):
            log.warning("DeWarmte: onbekend uur %s op %s, rest van de dag overgeslagen", b.get("key"), day)
            break
        start = hours[pos][0] - HOUR
        out.append((int(start.timestamp()), num(b.get("heat_supplied")), num(b.get("electricity_consumed")),
                    num(b.get("electric_backup_usage"))))
        pos += 1
    return out


async def sync_history(client: "DeWarmteClient", product_id: str, today: date) -> int:
    """Haalt ontbrekende dagen op. Afgesloten dagen (ouder dan gisteren) worden onthouden,
    ook als ze leeg zijn, zodat ze niet steeds opnieuw gevraagd worden; vandaag en gisteren
    worden elke keer ververst (de pomp levert uren soms later aan)."""
    db = get_db()
    n = 0
    for i in range(HISTORY_DAYS, -1, -1):
        d = today - timedelta(days=i)
        key = f"dw_hist_done_{d.isoformat()}"
        final = d < today - timedelta(days=1)
        if final and db.get_kv(key):
            continue
        rows = parse_insights(d, await client.insights(product_id, d))
        db.save_dw_hours(rows)
        n += len(rows)
        if final:
            db.set_kv(key, "1")
        await asyncio.sleep(0.3)
    return n


class DeWarmteClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.token: str | None = None
        self.token_at = 0.0

    async def login(self) -> None:
        async with self.session.post(
            f"{BASE}/auth/token/",
            json={"email": config.dw_username, "password": config.dw_password},
            headers=HEADERS,
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"inloggen mislukt (HTTP {r.status})")
            data = await r.json()
        self.token = data.get("access")
        if not self.token:
            raise RuntimeError("geen token ontvangen")
        self.token_at = time.time()

    async def products(self) -> list[dict[str, Any]]:
        if not self.token or time.time() - self.token_at > 50 * 60:
            await self.login()
        for attempt in range(2):
            async with self.session.get(
                f"{BASE}/customer/products/", headers={**HEADERS, "Authorization": f"Bearer {self.token}"}
            ) as r:
                if r.status == 401 and attempt == 0:
                    await self.login()
                    continue
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = await r.json()
                return data.get("results") or []
        return []

    async def insights(self, product_id: str, day: date) -> dict[str, Any]:
        if not self.token or time.time() - self.token_at > 50 * 60:
            await self.login()
        params = {"timespan": "hourly", "start_date": day.isoformat()}
        for attempt in range(2):
            async with self.session.get(
                f"{BASE}/customer/products/{product_id}/insights/", params=params,
                headers={**HEADERS, "Authorization": f"Bearer {self.token}"},
            ) as r:
                if r.status == 401 and attempt == 0:
                    await self.login()
                    continue
                if r.status != 200:
                    raise RuntimeError(f"insights HTTP {r.status}")
                return await r.json()
        return {}


@register("heatpump", "dewarmte", "DeWarmte (Pomp AO, MP, T, Heatcycle)", env=("DEWARMTE_USERNAME", "DEWARMTE_PASSWORD", "DEWARMTE_POLL_SECONDS"),
          description="Cloud-API van mydewarmte.com (onofficieel).")
async def run(src: Source) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        client = DeWarmteClient(session)
        last_history = 0.0
        while True:
            try:
                products = await client.products()
                if not products:
                    raise RuntimeError("geen warmtepomp gevonden in dit account")
                src.update(**parse_product(products[0]))
                pid = products[0].get("id")
                if pid and time.time() - last_history > HISTORY_EVERY_S:
                    last_history = time.time()  # ook bij een fout pas over een uur opnieuw
                    try:
                        n = await sync_history(client, str(pid), datetime.now(config.tz).date())
                        if n:
                            log.info("DeWarmte: %d uur aan geschiedenis bijgewerkt", n)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("DeWarmte-geschiedenis ophalen mislukt: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                if src.last_error != msg:
                    log.warning("DeWarmte: %s", msg)
                src.error(msg)
                client.token = None
            await asyncio.sleep(config.dw_poll_s)
