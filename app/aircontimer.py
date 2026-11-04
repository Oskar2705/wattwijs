"""Uit-timers voor de airco's: "zet deze airco over 2 uur uit".

De timers lopen op de server, dus ze werken ook als de telefoon dicht is. Ze worden in de
database bewaard en overleven zo een herstart van de app. Werkt voor elke airco-integratie
die `Source.control` aanbiedt (ook de demo).

Een timer verdwijnt vanzelf als
* hij de airco heeft uitgezet;
* de airco al op een andere manier uit is gegaan (afstandsbediening, Smart M-Air, deze app);
* het uitzetten na RETRY_FOR seconden nog steeds niet lukt (airco onbereikbaar).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from .db import get_db
from .state import state

log = logging.getLogger(__name__)

KV_KEY = "aircon_timers"
MIN_S = 60  # korter dan een minuut heeft geen zin
MAX_S = 24 * 3600  # verder vooruit dan een dag: waarschijnlijk een vergissing
GRACE_S = 150  # zo lang na het instellen telt "de airco staat uit" nog niet (de status loopt achter)
RETRY_FOR = 30 * 60
CHECK_EVERY = 15


def resolve_off_at(body: dict[str, Any], now: float) -> float:
    """{"minutes": 90} of {"at": <unix-tijd>} -> tijdstip waarop de airco uit moet. ValueError bij onzin."""
    if body.get("minutes") is not None:
        try:
            minutes = float(body["minutes"])
        except (TypeError, ValueError):
            raise ValueError("ongeldig aantal minuten") from None
        off_at = now + minutes * 60
    elif body.get("at") is not None:
        try:
            off_at = float(body["at"])
        except (TypeError, ValueError):
            raise ValueError("ongeldig tijdstip") from None
    else:
        raise ValueError("geef een tijd op")
    if off_at - now < MIN_S - 1:
        raise ValueError("kies een tijd minstens een minuut vanaf nu")
    if off_at - now > MAX_S:
        raise ValueError("kies een tijd binnen 24 uur")
    return round(off_at)


class Timers:
    """Per airco-ID: {"off_at": ts, "set_at": ts}. Bewaard in de kv-tabel."""

    def __init__(self, load: Callable[[], str | None] | None = None, save: Callable[[str], None] | None = None):
        self._load = load
        self._save = save
        self.items: dict[str, dict[str, float]] = {}
        self.loaded = False

    def _ensure(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        raw = self._load() if self._load else None
        try:
            data = json.loads(raw) if raw else {}
            self.items = {k: {"off_at": float(v["off_at"]), "set_at": float(v.get("set_at", 0))}
                          for k, v in data.items()}
        except (ValueError, TypeError, KeyError, AttributeError):
            self.items = {}

    def _persist(self) -> None:
        if self._save:
            self._save(json.dumps(self.items))

    def set(self, unit_id: str, off_at: float, now: float | None = None) -> None:
        self._ensure()
        self.items[unit_id] = {"off_at": off_at, "set_at": now or time.time()}
        self._persist()

    def clear(self, unit_id: str) -> bool:
        self._ensure()
        if self.items.pop(unit_id, None) is None:
            return False
        self._persist()
        return True

    def clear_all(self) -> int:
        self._ensure()
        n = len(self.items)
        if n:
            self.items = {}
            self._persist()
        return n

    def off_at(self, unit_id: str) -> float | None:
        self._ensure()
        t = self.items.get(unit_id)
        return t["off_at"] if t else None

    def all(self) -> dict[str, dict[str, float]]:
        self._ensure()
        return dict(self.items)


def _db_timers() -> Timers:
    return Timers(lambda: get_db().get_kv(KV_KEY), lambda v: get_db().set_kv(KV_KEY, v))


timers = _db_timers()


def with_timers(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Voegt "off_at" toe aan de airco-weergave voor de app."""
    return [{**u, "off_at": timers.off_at(u.get("id", ""))} for u in units]


def decide(t: dict[str, float], unit: dict[str, Any] | None, now: float) -> str:
    """Wat te doen met één timer: "wait", "drop" (vervalt) of "fire" (nu uitzetten)."""
    if unit is None:
        return "drop"  # deze airco bestaat niet meer
    seen_off = bool(unit.get("online")) and unit.get("on") is False
    if seen_off and now - t["set_at"] > GRACE_S:
        return "drop"  # al uit, op een andere manier
    if now < t["off_at"]:
        return "wait"
    if seen_off:
        return "drop"
    return "fire"


def _clear_same(uid: str, t: dict[str, float]) -> None:
    """Ruimt een timer op, tenzij iemand hem intussen (tijdens het uitzetten) opnieuw heeft ingesteld."""
    if timers.off_at(uid) == t["off_at"]:
        timers.clear(uid)


async def check(now: float | None = None) -> list[str]:
    """Eén ronde: zet airco's uit waarvan de timer is afgelopen. Geeft de uitgezette ID's terug."""
    now = now or time.time()
    src = state.aircon
    items = timers.all()
    if not items or src.control is None:
        return []
    units = src.values.get("units")
    if not units:
        return []  # nog geen status binnen (net gestart): niets weggooien
    by_id = {u.get("id"): u for u in units}
    done: list[str] = []
    for uid, t in items.items():
        unit = by_id.get(uid)
        action = decide(t, unit, now)
        if action == "drop":
            timers.clear(uid)
            continue
        if action == "wait":
            continue
        name = (unit or {}).get("name", uid)
        try:
            await asyncio.wait_for(src.control(uid, {"on": False}), timeout=90)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if now - t["off_at"] > RETRY_FOR:
                log.warning("Airco %s: timer kon de airco niet uitzetten (%s), timer vervalt", name, exc)
                _clear_same(uid, t)
            else:
                log.info("Airco %s: uitzetten via timer lukte nog niet (%s), straks opnieuw", name, exc)
            continue
        log.info("Airco %s: uitgezet door de timer", name)
        _clear_same(uid, t)
        done.append(uid)
    return done


async def run() -> None:
    while True:
        try:
            await check()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Airco-timers controleren mislukt")
        await asyncio.sleep(CHECK_EVERY)
