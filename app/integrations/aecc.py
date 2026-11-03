"""Voltdeer SR5000 (AECC-platform) via lokale TCP, poort 8080.

Protocol (overgenomen van StekkerDeal/aecc-battery-local): JSON-commando's per
regel, bv. {"Get": "EnergyParameter", "SerialNumber": 1, "CommandSource": "HA"}.
Het antwoord bevat "SSumInfoList" (systeemtotalen) en "Storage_list" (per unit).

De batterij leest zelf de P1-meter (SMeter-RS06) uit en geeft die waarde terug
als SSumInfoList.MeterTotalActivePower: + = afname, - = teruglevering.

We lezen alleen; er worden nooit Set-commando's verstuurd.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..config import config
from ..state import Source
from . import register

log = logging.getLogger(__name__)
_DECODER = json.JSONDecoder()


class AeccClient:
    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buf = b""
        self._serial = 0

    async def connect(self) -> None:
        await self.close()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        self._buf = b""

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._reader = self._writer = None

    async def get(self, command: str) -> dict[str, Any]:
        if self._writer is None:
            await self.connect()
        assert self._reader and self._writer
        self._serial += 1
        serial = self._serial
        payload = {"Get": command, "SerialNumber": serial, "CommandSource": "HA"}
        self._writer.write((json.dumps(payload) + "\n").encode())
        await self._writer.drain()
        async with asyncio.timeout(self.timeout):
            while True:
                # Haal complete JSON-objecten uit de buffer (antwoorden kunnen over regels lopen).
                while True:
                    text = self._buf.decode("utf-8", errors="ignore").lstrip()
                    try:
                        data, end = _DECODER.raw_decode(text)
                    except json.JSONDecodeError:
                        break
                    self._buf = text[end:].encode()
                    if isinstance(data, dict) and data.get("SerialNumber") in (None, serial):
                        return data
                chunk = await self._reader.read(4096)
                if not chunk:
                    raise ConnectionResetError("batterij sloot de verbinding")
                self._buf += chunk


def _f(d: dict[str, Any], key: str, scale: float = 1.0) -> float | None:
    v = d.get(key)
    try:
        return None if v is None or v == "" else float(v) * scale
    except (TypeError, ValueError):
        return None


def _sum_units(units: list[dict[str, Any]], key: str, scale: float) -> float | None:
    vals = [x for x in (_f(u, key, scale) for u in units) if x is not None]
    return sum(vals) if vals else None


def parse_energy_parameter(raw: dict[str, Any], grid_invert: bool = False) -> dict[str, float | None]:
    """Zet een EnergyParameter-antwoord om naar onze waarden (W en %)."""
    s = raw.get("SSumInfoList") or {}
    units = raw.get("Storage_list") or []

    soc = _f(s, "AverageBatteryAverageSOC")
    if soc is None and units:
        socs = [x for x in (_f(u, "BatterySoc") for u in units) if x is not None]
        soc = sum(socs) / len(socs) if socs else None

    grid = _f(s, "MeterTotalActivePower")
    if grid is not None and grid_invert:
        grid = -grid

    pv = _f(s, "TotalPVPower")
    if pv is None:
        pv = _sum_units(units, "PvChargingPower", 0.1)
    pv = pv or 0.0

    backup = _f(s, "TotalBackUpPower", 10.0)  # dit veld is in eenheden van 10 W
    if backup is None:
        backup = _sum_units(units, "OffGridLoadPower", 1.0)
    backup = backup or 0.0

    grid_out = _f(s, "TotalGridOutputPower")  # + = batterij levert aan huis
    if grid_out is not None:
        battery_ac = grid_out + backup
        battery_w = pv - grid_out - backup
    else:
        # Terugval op de per-unit velden (deciwatt).
        charge = _sum_units(units, "AcChargingPower", 0.1) or 0.0
        discharge = _sum_units(units, "BatteryDischargingPower", 0.1) or 0.0
        battery_ac = discharge - charge
        battery_w = charge - discharge + pv

    return {"soc": soc, "grid_w": grid, "pv_w": pv, "battery_w": battery_w, "battery_ac_w": battery_ac}


@register(
    "battery", "aecc", "AECC-platform (Voltdeer, Sunpura, Lunergy, AEG, ...)",
    env=("BATTERY_HOST", "BATTERY_PORT", "BATTERY_POLL_SECONDS"),
    description="Lokaal via TCP poort 8080, alleen lezen. Geeft ook de P1-meterwaarde door als de batterij een meter heeft.",
)
async def run(src: Source) -> None:
    client = AeccClient(config.battery_host, config.battery_port)
    backoff = 2.0
    last_soc: float | None = None
    while True:
        try:
            raw = await client.get("EnergyParameter")
            if not (raw.get("SSumInfoList") or raw.get("Storage_list")):
                raise ValueError(f"onverwacht antwoord: {str(raw)[:200]}")
            vals = parse_energy_parameter(raw, config.grid_invert)
            # Bekende firmware-glitch: SOC kort 0 terwijl de batterij gewoon werkt.
            if vals["soc"] == 0 and last_soc and abs(vals["battery_w"] or 0) > 200:
                vals["soc"] = last_soc
            last_soc = vals["soc"] if vals["soc"] is not None else last_soc
            src.update(**vals)
            backoff = 2.0
            await asyncio.sleep(config.battery_poll_s)
        except asyncio.CancelledError:
            await client.close()
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            if src.last_error != msg:
                log.warning("Batterij: %s (opnieuw over %.0fs)", msg, backoff)
            src.error(msg)
            await client.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
