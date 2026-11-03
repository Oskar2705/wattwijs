"""Frank Energie: dynamische stroomprijzen via de openbare GraphQL-API (geen login nodig).

Zelfde query als de onderhouden bibliotheek python-frank-energie:
marketPrices(date, resolution: PT15M) -> electricityPrices[] met o.a.
  marketPrice      kale marktprijs (EUR/kWh, excl. btw)
  allInPrice       wat je per kWh afname betaalt: markt + btw + opslag + energiebelasting
Uurprijzen worden in vier kwartieren verdeeld.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import aiohttp

from ..config import config
from ..prices import QUARTER
from . import register

URLS = ["https://graphql.frankenergie.nl/", "https://frank-graphql-prod.graphcdn.app/"]
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-graphql-client-name": "frank-app",
    "skip-graphcdn": "1",
}
QUERY = """
query MarketPrices($date: String!, $resolution: PriceResolution!) {
  marketPrices(date: $date, resolution: $resolution) {
    electricityPrices {
      from
      till
      marketPrice
      marketPriceTax
      sourcingMarkupPrice
      energyTaxPrice
      allInPrice
    }
  }
}
"""


def _ts(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:  # Frank stuurt UTC; nooit de tijdzone van de container aannemen
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse_prices(resp: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """GraphQL-antwoord -> [(kwartier_ts, lokale_dag, marktprijs, all_in)]."""
    items = (((resp.get("data") or {}).get("marketPrices") or {}).get("electricityPrices")) or []
    out: dict[int, tuple[int, str, float, float]] = {}
    for it in items:
        start, end = _ts(it["from"]), _ts(it["till"])
        market = float(it.get("marketPrice") or 0.0)
        all_in = it.get("allInPrice")
        if all_in is None:
            all_in = sum(float(it.get(k) or 0.0) for k in ("marketPrice", "marketPriceTax", "sourcingMarkupPrice", "energyTaxPrice"))
        for q in range(start - start % QUARTER, end, QUARTER):
            day = datetime.fromtimestamp(q, config.tz).date().isoformat()
            out[q] = (q, day, round(market, 5), round(float(all_in), 5))
    return [out[k] for k in sorted(out)]


async def fetch_day(session: aiohttp.ClientSession, day: date) -> list[tuple[int, str, float, float]]:
    payload = {"query": QUERY, "operationName": "MarketPrices", "variables": {"date": day.isoformat(), "resolution": "PT15M"}}
    last_exc: Exception | None = None
    for url in URLS:
        try:
            async with session.post(url, json=payload, headers=HEADERS) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
            if data.get("errors"):
                raise RuntimeError(str(data["errors"])[:200])
            return parse_prices(data)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise RuntimeError(f"prijzen ophalen mislukt: {last_exc}")




@register("prices", "frank", "Frank Energie (NL)", env=("PRICES_ENABLED",),
          description="Kwartierprijzen, openbare API zonder login.")
class FrankPrices:
    source = "frank"

    async def fetch_day(self, session: aiohttp.ClientSession, day: date) -> list[tuple[int, str, float, float]]:
        return await fetch_day(session, day)
