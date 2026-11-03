"""Slimme meter rechtstreeks uitlezen via een P1-dongle op het lokale netwerk.

* smeter:     SMeter-RS06 (AECC), GET http://<ip>/ -> {"device": {"status": {...}}}
              total_power (W), Positive_total_electric / Reverse_total_electric (kWh)
* homewizard: HomeWizard P1, GET http://<ip>/api/v1/data
              active_power_w, total_power_import_kwh / total_power_export_kwh

Of total_power negatief wordt bij teruglevering is per dongle niet zeker. Daarom
bepalen we de richting ook uit de meterstanden: loopt de terugleverstand op en
de afnamestand niet, dan wordt er teruggeleverd.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ..config import config
from ..state import Source
from . import register

log = logging.getLogger(__name__)


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "", "NULL") else float(v)
    except (TypeError, ValueError):
        return None


def parse(raw: dict[str, Any], kind: str) -> dict[str, float | None]:
    if kind == "homewizard":
        return {
            "power_w": _f(raw.get("active_power_w")),
            "import_kwh": _f(raw.get("total_power_import_kwh")),
            "export_kwh": _f(raw.get("total_power_export_kwh")),
            "l1_w": _f(raw.get("active_power_l1_w")),
            "l2_w": _f(raw.get("active_power_l2_w")),
            "l3_w": _f(raw.get("active_power_l3_w")),
        }
    s = (raw.get("device") or {}).get("status") or raw.get("status") or {}
    return {
        "power_w": _f(s.get("total_power")),
        "import_kwh": _f(s.get("Positive_total_electric")),
        "export_kwh": _f(s.get("Reverse_total_electric")),
        "l1_w": _f(s.get("la_power")),
        "l2_w": _f(s.get("lb_power")),
        "l3_w": _f(s.get("lc_power")),
    }


class Direction:
    """Houdt bij of er wordt afgenomen of teruggeleverd, op basis van de meterstanden."""

    def __init__(self) -> None:
        self.last_import: float | None = None
        self.last_export: float | None = None
        self.exporting: bool | None = None

    def update(self, imp: float | None, exp: float | None) -> bool | None:
        if imp is not None and exp is not None:
            if self.last_import is not None and self.last_export is not None:
                d_imp, d_exp = imp - self.last_import, exp - self.last_export
                if d_exp > 0 and d_imp <= 0:
                    self.exporting = True
                elif d_imp > 0 and d_exp <= 0:
                    self.exporting = False
            self.last_import, self.last_export = imp, exp
        return self.exporting


def signed_grid(power: float | None, exporting: bool | None, invert: bool = False) -> float | None:
    """+ = afname, - = teruglevering."""
    if power is None:
        return None
    if invert:
        power = -power
    if power < 0:
        return power  # dongle geeft zelf al een teken
    if exporting:
        return -power
    return power


async def _run(src: Source, kind: str) -> None:
    url = f"http://{config.p1_host}/api/v1/data" if kind == "homewizard" else f"http://{config.p1_host}/"
    direction = Direction()
    backoff = 2.0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        while True:
            try:
                async with session.get(url) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    raw = await r.json(content_type=None)
                v = parse(raw, kind)
                if v["power_w"] is None:
                    raise ValueError(f"geen vermogen in antwoord: {str(raw)[:200]}")
                exporting = direction.update(v["import_kwh"], v["export_kwh"])
                src.update(grid_w=signed_grid(v["power_w"], exporting, config.grid_invert), **v)
                backoff = 2.0
                await asyncio.sleep(config.p1_poll_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                if src.last_error != msg:
                    log.warning("P1-meter: %s", msg)
                src.error(msg)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


@register("grid", "smeter", "SMeter-RS06 (AECC P1-dongle)", env=("P1_HOST", "P1_POLL_SECONDS", "GRID_INVERT"),
          description="Lokale JSON op http://<ip>/.")
async def run_smeter(src: Source) -> None:
    await _run(src, "smeter")


@register("grid", "homewizard", "HomeWizard P1-meter", env=("P1_HOST", "P1_POLL_SECONDS", "GRID_INVERT"),
          description="Lokale API v1 (zet 'Lokale API' aan in de HomeWizard-app).")
async def run_homewizard(src: Source) -> None:
    await _run(src, "homewizard")
