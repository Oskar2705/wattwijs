"""SolarEdge: live opwek en paneelgegevens.

* Live opwek bij voorkeur lokaal via Modbus TCP (poort 1502 op de omvormer,
  moet één keer aangezet worden via SetApp / de installateur).
* Anders via het SolarEdge-monitoringportaal (cloud), elke paar minuten.
* Opbrengst per paneel (optimizer) komt altijd uit het portaal, via de
  bibliotheek solaredge-web (dezelfde die Home Assistant gebruikt).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dtime
from typing import Any

import aiohttp

from ..config import config
from ..state import Source
from . import register

log = logging.getLogger(__name__)


class SolarEdgeWebHolder:
    """Eén gedeelde portaal-sessie voor live opwek en paneeldata."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def client(self) -> Any:
        from solaredge_web import SolarEdgeWeb

        if self._client is None:
            self._session = aiohttp.ClientSession()
            self._client = SolarEdgeWeb(
                config.se_username, config.se_password, config.se_site_id, self._session, timeout=30
            )
        return self._client

    async def live_power_w(self) -> float | None:
        async with self._lock:
            c = await self.client()
            live = await c.async_get_live_power()
            return live.current_power

    async def panels(self) -> dict[str, dict[str, Any]]:
        """Alle optimizers met naam en de string waar ze in zitten."""
        async with self._lock:
            c = await self.client()
            equipment = await c.async_get_equipment()
            structure = getattr(c, "_site_structure", {}) or {}
        return panels_from_structure(structure, equipment)

    async def panel_energy(self, day: date) -> dict[str, float]:
        """Opbrengst (Wh) per optimizer voor één dag."""
        async with self._lock:
            c = await self.client()
            start = datetime.combine(day, dtime.min)
            totals = await c.async_get_energy_totals(start, start)
        return totals

    async def close(self) -> None:
        if self._session:
            await self._session.close()


def panels_from_structure(structure: dict[str, Any], equipment: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def node_id(n: dict[str, Any]) -> str | None:
        return n.get("serial") or (n.get("properties") or {}).get("identifier") or n.get("uuid")

    def walk(n: dict[str, Any], group: str | None) -> None:
        t = n.get("type")
        if t == "STRING":
            group = n.get("name") or node_id(n) or "String"
        if t == "INVERTER" and group is None:
            group = n.get("name")
        if t == "OPTIMIZER":
            sid = node_id(n)
            if sid and (n.get("properties") or {}).get("status") != "INACTIVE":
                name = n.get("name") or sid
                if name.startswith("Optimizer "):
                    name = name[len("Optimizer "):]
                out[sid] = {
                    "name": name,
                    "group": group or "Alle panelen",
                    "order": n.get("displayOrder") or n.get("name") or sid,
                }
        for ch in n.get("children") or []:
            walk(ch, group)

    walk(structure, None)
    if equipment and not out:
        for sid, n in equipment.items():
            if n.get("type") == "OPTIMIZER":
                out[sid] = {"name": n.get("name") or sid, "group": "Alle panelen", "order": n.get("name") or sid}
    return out


web = SolarEdgeWebHolder()


@register("panels", "solaredge", "SolarEdge-portaal (per optimizer)",
          env=("SOLAREDGE_USERNAME", "SOLAREDGE_PASSWORD", "SOLAREDGE_SITE_ID"),
          description="Opbrengst per paneel via monitoring.solaredge.com (bibliotheek solaredge-web).")
def panels_provider() -> SolarEdgeWebHolder:
    return web


@register("solar", "solaredge_modbus", "SolarEdge via Modbus TCP",
          env=("SOLAREDGE_MODBUS_HOST", "SOLAREDGE_MODBUS_PORT", "SOLAREDGE_MODBUS_POLL_SECONDS"),
          description="Lokaal, elke 5 s. Modbus TCP moet aan staan op de omvormer (SetApp).")
async def run_modbus(src: Source) -> None:
    from modbus_connection.tmodbus import connect_tcp
    from solaredged import SolarEdge

    backoff = 5.0
    while True:
        conn = None
        try:
            conn = await connect_tcp(config.se_modbus_host, port=config.se_modbus_port, timeout=10)
            se = await SolarEdge.async_probe(conn.for_unit(1))
            backoff = 5.0
            while True:
                await se.async_update_readings()
                p = se.inverter.ac_power
                # 's Nachts slaapt de omvormer; dan is er gewoon geen opwek.
                src.update(power_w=max(float(p), 0.0) if p is not None else 0.0)
                await asyncio.sleep(config.se_modbus_poll_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"Modbus: {type(exc).__name__}: {exc}"
            if src.last_error != msg:
                log.warning("SolarEdge %s", msg)
            src.error(msg)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass


@register("solar", "solaredge_cloud", "SolarEdge-portaal (cloud)",
          env=("SOLAREDGE_USERNAME", "SOLAREDGE_PASSWORD", "SOLAREDGE_SITE_ID", "SOLAREDGE_CLOUD_POLL_SECONDS"),
          description="Live opwek via het monitoringportaal, elke 2 minuten.")
async def run_cloud_live(src: Source) -> None:
    failures = 0
    while True:
        try:
            p = await web.live_power_w()
            src.update(power_w=max(float(p or 0.0), 0.0))
            if failures:
                log.info("SolarEdge portaal: live opwek komt weer binnen (na %d mislukte poging(en))", failures)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            failures += 1
            msg = f"portaal: {type(exc).__name__}: {exc}".rstrip(": ")
            if failures == 1 or failures % 10 == 0:
                log.warning("SolarEdge %s (poging %d)", msg, failures)
            src.error(msg)
        # Na een mislukte poging (vaak een trage portaal-login vlak na het opstarten) sneller opnieuw.
        await asyncio.sleep(config.se_cloud_poll_s if not failures else min(config.se_cloud_poll_s, 30 * failures))
