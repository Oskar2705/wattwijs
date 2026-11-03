"""DeWarmte warmtepomp via de (onofficiële) cloud-API van mydewarmte.com.

Zelfde endpoints als de Home Assistant-integratie ronald-willems/dewarmte-homeassistant:
  POST /v1/auth/token/          {"email", "password"} -> {"access"}
  GET  /v1/customer/products/   -> results[] met status (vermogens in kW)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from ..config import config
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


@register("heatpump", "dewarmte", "DeWarmte (Pomp AO, MP, T, Heatcycle)", env=("DEWARMTE_USERNAME", "DEWARMTE_PASSWORD", "DEWARMTE_POLL_SECONDS"),
          description="Cloud-API van mydewarmte.com (onofficieel).")
async def run(src: Source) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        client = DeWarmteClient(session)
        while True:
            try:
                products = await client.products()
                if not products:
                    raise RuntimeError("geen warmtepomp gevonden in dit account")
                src.update(**parse_product(products[0]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                if src.last_error != msg:
                    log.warning("DeWarmte: %s", msg)
                src.error(msg)
                client.token = None
            await asyncio.sleep(config.dw_poll_s)
