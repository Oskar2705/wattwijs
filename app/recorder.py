"""Integreert de live vermogens tot energie per minuut en schrijft die naar de database."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .db import CLIMATE_COLS, ENERGY_COLS, HEAT_ENERGY_COLS, POWER_COLS, get_db
from .state import state

log = logging.getLogger(__name__)
TICK_S = 5.0
MAX_DT_S = 30.0  # gaten langer dan dit tellen niet mee


class MinuteAccumulator:
    def __init__(self, minute_ts: int) -> None:
        self.minute_ts = minute_ts
        self.energy = {c: 0.0 for c in [*ENERGY_COLS, *HEAT_ENERGY_COLS]}
        self._wsum: dict[str, float] = {c: 0.0 for c in [*POWER_COLS, *CLIMATE_COLS]}
        self._tsum: dict[str, float] = {c: 0.0 for c in [*POWER_COLS, *CLIMATE_COLS]}
        self.soc: float | None = None

    def add(self, snap: dict[str, Any], dt: float) -> None:
        h = dt / 3600.0
        grid = snap.get("grid_w")
        if grid is not None:
            if grid >= 0:
                self.energy["import_wh"] += grid * h
            else:
                self.energy["export_wh"] += -grid * h
        bat = snap.get("battery_w")
        if bat is not None:
            if bat >= 0:
                self.energy["charge_wh"] += bat * h
            else:
                self.energy["discharge_wh"] += -bat * h
        for key, col in (("solar_w", "solar_wh"), ("heatpump_w", "heatpump_wh"), ("aircon_w", "aircon_wh")):
            v = snap.get(key)
            if v is not None:
                self.energy[col] += max(v, 0.0) * h
        # Huisverbruik zonder afkappen: kortstondig negatieve waarden komen door meter en
        # omvormer die niet tegelijk gelezen worden, en vallen over de minuut tegen elkaar weg.
        house = snap.get("house_net_w", snap.get("house_w"))
        if house is not None:
            self.energy["house_wh"] += house * h
        heat = snap.get("heat_w")
        if heat is not None:
            self.energy["heat_wh"] += max(heat, 0.0) * h
        for c in [*POWER_COLS, *CLIMATE_COLS]:
            if c == "soc":
                continue
            v = snap.get(c)
            if v is not None:
                self._wsum[c] += v * dt
                self._tsum[c] += dt
        if snap.get("soc") is not None:
            self.soc = snap["soc"]

    def power(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {
            c: (self._wsum[c] / self._tsum[c] if self._tsum[c] > 0 else None)
            for c in [*POWER_COLS, *CLIMATE_COLS] if c != "soc"
        }
        out["soc"] = self.soc
        return out

    def has_data(self) -> bool:
        return any(t > 0 for t in self._tsum.values()) or self.soc is not None


class Recorder:
    def __init__(self) -> None:
        self.acc: MinuteAccumulator | None = None
        self.last_tick: float | None = None

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        snap = state.snapshot(now)
        minute = int(now // 60) * 60
        if self.acc is None:
            self.acc = MinuteAccumulator(minute)
        elif minute != self.acc.minute_ts:
            self.flush()
            self.acc = MinuteAccumulator(minute)
        if self.last_tick is not None:
            dt = now - self.last_tick
            if 0 < dt <= MAX_DT_S:
                self.acc.add(snap, dt)
        self.last_tick = now

    def flush(self) -> None:
        if self.acc and self.acc.has_data():
            get_db().write_minute(self.acc.minute_ts, self.acc.energy, self.acc.power())

    def pending_energy(self) -> dict[str, float]:
        return dict(self.acc.energy) if self.acc else {c: 0.0 for c in ENERGY_COLS}


recorder = Recorder()


async def run() -> None:
    while True:
        try:
            recorder.tick()
        except Exception:  # noqa: BLE001
            log.exception("Fout bij vastleggen")
        await asyncio.sleep(TICK_S)
