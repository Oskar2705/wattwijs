"""Pushbericht als het huis een tijd stroom teruglevert: tijd om iets aan te zetten.

Zinvol vanaf het einde van salderen (CONTRACT_START): teruggeleverde stroom levert dan
veel minder op dan stroom die je zelf gebruikt. Met een thuisbatterij pas als die vol is.
In het stookseizoen komt er een tip bij om de verwarming een graad hoger te zetten
(het huis houdt die warmte vast voor de dure avond), en later een herinnering om hem
weer terug te zetten.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .config import config

BATTERY_FULL_PCT = 95.0
HEATING_BELOW_C = 14.0


@dataclass
class ExportWatcher:
    """Houdt bij hoe lang er al wordt teruggeleverd. Puur rekenwerk, zonder klok of I/O."""

    min_w: float = 500.0
    minutes: float = 10.0
    cooldown_h: float = 3.0
    since: float | None = None
    last_sent: float | None = None

    def update(self, now: float, grid_w: float | None, battery_configured: bool, soc: float | None) -> float | None:
        """Geeft het aantal minuten terug als er nu een bericht uit moet, anders None."""
        exporting = grid_w is not None and grid_w <= -self.min_w
        # Kleine dip (wolk, waterkoker) breekt de reeks niet meteen af: pas onder de helft.
        still = grid_w is not None and grid_w <= -self.min_w / 2
        if exporting:
            if self.since is None:
                self.since = now
        elif not still:
            self.since = None
        if self.since is None:
            return None
        if battery_configured and (soc is None or soc < BATTERY_FULL_PCT):
            return None  # de batterij vangt het overschot nog op (of we weten het niet)
        if self.last_sent is not None and now - self.last_sent < self.cooldown_h * 3600:
            return None
        mins = (now - self.since) / 60
        if mins < self.minutes:
            return None
        self.last_sent = now
        return mins


def heating_season(today: date, outdoor_c: float | None) -> bool:
    if outdoor_c is not None:
        return outdoor_c < HEATING_BELOW_C
    return today.month in (10, 11, 12, 1, 2, 3, 4)


def _cent(v: float) -> str:
    c = v * 100
    s = f"{c:.1f}" if abs(c) < 10 else f"{c:.0f}"
    return s.replace(".", ",") + " cent"


def _kw(w: float) -> str:
    return f"{abs(w) / 1000:.1f}".replace(".", ",") + " kW"


def build_message(grid_w: float, minutes: float, export_price: float | None, battery_full: bool,
                  heating_tip: bool, heat_back_time: str) -> tuple[str, str]:
    lead = "De batterij is vol en je levert" if battery_full else "Je levert"
    mins = int(round(minutes))
    if export_price is not None and export_price < 0:
        title = "⚡ Terugleveren kost nu geld"
        body = (f"{lead} al {mins} minuten zo'n {_kw(grid_w)} terug, en dat kost nu {_cent(-export_price)} per kWh. "
                "Zet nu de vaatwasser, wasmachine of droger aan.")
    else:
        title = "☀️ Je levert stroom terug"
        price = f" voor {_cent(export_price)} per kWh" if export_price is not None else ""
        body = (f"{lead} al {mins} minuten zo'n {_kw(grid_w)} terug{price}. "
                "Goed moment voor de vaatwasser, wasmachine of droger.")
    if heating_tip:
        body += (f" Of zet de verwarming een graad hoger: het huis houdt die warmte vast voor vanavond. "
                 f"Om {heat_back_time} krijg je een seintje om hem terug te zetten.")
    return title, body


def heat_back_message() -> tuple[str, str]:
    return ("🌡️ Verwarming weer terugzetten",
            "Vanmiddag was er zon over en stond de verwarming hoger. Zet hem nu weer op de gewone "
            "temperatuur, dan stookt de warmtepomp minder met dure avondstroom.")


def active(today: date) -> bool:
    start = config.push_export_from or config.contract_start
    try:
        return today >= date.fromisoformat(start)
    except ValueError:
        return False


def now_ts() -> float:
    return time.time()


def local_now() -> datetime:
    return datetime.now(config.tz)


def snapshot_inputs(snap: dict[str, Any]) -> tuple[float | None, bool, float | None, float | None]:
    hp = snap.get("heatpump") or {}
    return snap.get("grid_w"), bool(snap.get("battery_configured")), snap.get("soc"), hp.get("outdoor_temperature")
