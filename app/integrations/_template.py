"""TEMPLATE - copy this file to add a new brand. Files starting with "_" are not loaded.

Steps:
  1. Copy to app/integrations/<brand>.py (no leading underscore).
  2. Pick the role and a short unique key, fill in the @register(...) line.
  3. Read your device in a loop and call ``src.update(...)`` with the fields of
     that role (see the table below). Call ``src.error(msg)`` on failures; the
     app shows the last error in the footer and the watchdog mails/pushes when a
     source stays offline.
  4. Select it with ``<ROLE>_INTEGRATION=<key>`` in .env, e.g. HEATPUMP_INTEGRATION=example
  5. Add a test with a recorded response from the real device (see tests/).

Fields per live role (all in W unless stated; None = unknown):

  grid      grid_w        + import from the grid, - export to the grid
            import_kwh, export_kwh   (optional) meter totals
  battery   soc           state of charge, %
            battery_w     + charging, - discharging (DC side, incl. own PV)
            battery_ac_w  + delivering to the house, - taking from the house
            pv_w          (optional) panels connected directly to the battery
            grid_w        (optional) when the battery reads the smart meter itself
  solar     power_w       current AC production of the inverter
  heatpump  power_w       electrical power used
            heat_output_w, supply_temperature, outdoor_temperature, is_on, fault_code   (optional)

Provider roles (register a class or factory instead of a run coroutine):

  panels    async panels() -> {id: {"name": str, "group": str, "order": str}}
            async panel_energy(day: date) -> {id: Wh}
  prices    source: str attribute (stored prices are wiped when it changes)
            async fetch_day(session, day: date) -> [(quarter_ts_utc, "YYYY-MM-DD local", market_eur_kwh, all_in_eur_kwh)]

Rules of thumb:
  * Read only. Never change device settings from an integration.
  * Never crash the loop: catch exceptions, call src.error(), back off, retry.
  * Keep secrets in environment variables, never in code or logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

from ..state import Source
from . import register

log = logging.getLogger(__name__)

HOST = os.getenv("EXAMPLE_HOST", "")
POLL_S = float(os.getenv("EXAMPLE_POLL_SECONDS", "30"))


def parse(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep parsing separate from I/O so it can be unit-tested with a recorded response."""
    kw = raw.get("power_kw")
    return {
        "power_w": None if kw is None else float(kw) * 1000,
        "outdoor_temperature": raw.get("outdoor_c"),
    }


@register("heatpump", "example", "Example heat pump (template)", env=("EXAMPLE_HOST", "EXAMPLE_POLL_SECONDS"),
          description="Reads http://<host>/status.json")
async def run(src: Source) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        while True:
            try:
                async with session.get(f"http://{HOST}/status.json") as r:
                    r.raise_for_status()
                    src.update(**parse(await r.json(content_type=None)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                src.error(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(POLL_S)
