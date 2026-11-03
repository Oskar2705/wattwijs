"""Vergelijk de prijs van nu met de Frank-app:
docker compose exec energiemonitor python -m app.checkprices [JJJJ-MM-DD]
"""

import asyncio
import sys
import time
from datetime import date, datetime

import aiohttp

from . import prices
from .integrations import frank
from .config import config
from .db import get_db


async def main() -> None:
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(config.tz).date()
    payload = {"query": frank.QUERY, "operationName": "MarketPrices",
               "variables": {"date": day.isoformat(), "resolution": "PT15M"}}
    print(f"Modus: {'DEMO (nepprijzen!)' if config.demo else 'Frank Energie'}   tijdzone: {config.tz_name}")
    print(f"Opgeslagen bron: {get_db().get_kv('price_source')}   laatste fout: {get_db().get_kv('price_error') or '-'}")
    print(f"Terugleverprijs = markt x {config.export_multiplier} + {config.export_fee_eur}\n")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.post(frank.URLS[0], json=payload, headers=frank.HEADERS) as r:
            data = await r.json(content_type=None)
    items = (((data.get("data") or {}).get("marketPrices") or {}).get("electricityPrices")) or []
    if not items:
        print("Geen prijzen ontvangen. Antwoord van Frank:", str(data)[:500])
        return
    first = items[0]
    print(f"{len(items)} blokken ontvangen, eerste: from={first['from']} till={first['till']}\n")
    stored = {r["ts"]: r for r in get_db().prices_between(0, 2**31)}
    now = time.time()
    print("tijd (lokaal)   markt   +btw   opslag  e-bel.  ALL-IN | opgeslagen all-in  terug")
    for it in items:
        ts = frank._ts(it["from"])
        if not (now - 3600 <= ts <= now + 3 * 3600) and day == datetime.now(config.tz).date():
            continue
        st = stored.get(ts - ts % 900, {})
        loc = datetime.fromtimestamp(ts, config.tz).strftime("%d-%m %H:%M")
        f = lambda k: f"{(it.get(k) or 0) * 100:6.2f}"  # noqa: E731
        print(f"{loc}   {f('marketPrice')} {f('marketPriceTax')} {f('sourcingMarkupPrice')} {f('energyTaxPrice')} "
              f"{f('allInPrice')} | {st.get('all_in', 0) * 100:10.2f} {st.get('export', 0) * 100:10.2f}   (cent/kWh)")


if __name__ == "__main__":
    asyncio.run(main())
