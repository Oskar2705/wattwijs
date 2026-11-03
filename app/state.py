"""Live toestand van alle bronnen, gedeeld tussen integraties, logger en API."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# Hoe lang een waarde geldig blijft zonder nieuwe meting (seconden).
MAX_AGE = {
    "grid": 60,
    "battery": 60,
    "solar": 20 * 60,  # cloud-integraties verversen maar eens per paar minuten
    "heatpump": 10 * 60,
    "aircon": 5 * 60,
    "heating": 75 * 60,  # tado wordt zuinig uitgelezen vanwege de daglimiet
}


@dataclass
class Source:
    name: str
    configured: bool = False
    values: dict[str, Any] = field(default_factory=dict)
    updated: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    mode: str = ""  # bv. "modbus" / "cloud" voor SolarEdge
    # Alleen voor bronnen die bediend kunnen worden (airco): async control(unit_id, changes) -> unit
    control: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # Overige acties, bv. {"login": ..., "logout": ...} voor tado
    actions: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = field(default_factory=dict)

    def update(self, **values: Any) -> None:
        self.values.update(values)
        self.updated = time.time()
        self.last_error = None

    def error(self, msg: str) -> None:
        self.last_error = msg
        self.last_error_at = time.time()

    def fresh(self, now: float | None = None) -> bool:
        if self.updated is None:
            return False
        return (now or time.time()) - self.updated <= MAX_AGE.get(self.name, 60)

    def get(self, key: str, now: float | None = None) -> Any:
        return self.values.get(key) if self.fresh(now) else None

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "online": self.fresh(),
            "updated": self.updated,
            "error": self.last_error,
            "mode": self.mode,
        }


class LiveState:
    def __init__(self) -> None:
        self.grid = Source("grid")
        self.battery = Source("battery")
        self.solar = Source("solar")
        self.heatpump = Source("heatpump")
        self.aircon = Source("aircon")
        self.heating = Source("heating")

    def sources(self) -> dict[str, Source]:
        return {"grid": self.grid, "battery": self.battery, "solar": self.solar, "heatpump": self.heatpump,
                "aircon": self.aircon, "heating": self.heating}

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Afgeleide momentwaarden in W. None = onbekend.

        Tekenafspraken:
          grid_w     + = afname van het net, - = teruglevering
          battery_w  + = batterij laadt,     - = batterij ontlaadt
          solar_w    totale opwek (SolarEdge + eventuele panelen op de batterij)
          house_w    totaal verbruik van het huis (incl. warmtepomp en airco's)
          aircon_w   geschat verbruik van alle airco's samen
        """
        now = now or time.time()
        # Netvermogen: bij voorkeur rechtstreeks van de meter, anders via de batterij.
        grid = self.grid.get("grid_w", now) if self.grid.configured else None
        if grid is None:
            grid = self.battery.get("grid_w", now)
        battery_w = self.battery.get("battery_w", now)
        batt_ac = self.battery.get("battery_ac_w", now)  # + = levert aan huis
        if not self.battery.configured:
            batt_ac = 0.0  # geen batterij in gebruik: telt niet mee in het huisverbruik
        pv_batt = self.battery.get("pv_w", now) or 0.0
        soc = self.battery.get("soc", now)
        se = self.solar.get("power_w", now)
        hp = self.heatpump.get("power_w", now)
        ac = self.aircon.get("power_w", now)

        se_known = se is not None or not self.solar.configured
        solar = None
        if se_known and (se is not None or self.battery.fresh(now)):
            solar = (se or 0.0) + pv_batt

        house = None
        if grid is not None and batt_ac is not None and se_known:
            house = max(grid + (se or 0.0) + batt_ac, 0.0)

        other = None
        if house is not None and hp is not None:
            other = max(house - hp - (ac or 0.0), 0.0)

        return {
            "ts": now,
            "grid_w": _r(grid),
            "solar_w": _r(solar),
            "solar_inverter_w": _r(se),
            "solar_battery_w": _r(pv_batt) if self.battery.fresh(now) else None,
            "battery_w": _r(battery_w),
            "battery_configured": self.battery.configured,
            "soc": _r(soc),
            "house_w": _r(house),
            "heatpump_w": _r(hp),
            "other_w": _r(other),
            "aircon_w": _r(ac),
            "aircon_configured": self.aircon.configured,
            "aircon_on": self.aircon.get("units_on", now),
            "heating_configured": self.heating.configured,
            "heatpump": {
                k: self.heatpump.get(k, now)
                for k in ("heat_output_w", "supply_temperature", "outdoor_temperature", "is_on", "fault_code")
            },
        }


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 1)


state = LiveState()
