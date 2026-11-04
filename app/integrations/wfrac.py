"""Mitsubishi Heavy Industries airco's met de WF-RAC wifi-module (app "Smart M-Air").

Lokaal via de HTTP(S)-API van de module op poort 51443, met de bibliotheek pywfrac
(dezelfde als de Home Assistant-integratie blues-sechseck/Mitsubishi-WF-RAC-Integration).

Instellen in .env, één regel voor alle binnenunits:
    AIRCO_UNITS=Woonkamer=192.168.1.40;Slaapkamer=192.168.1.41:51443

Verbruik: de module geeft geen vermogen in watt. Wel per binnenunit of de compressor
draait en een energieteller in stappen van 0,25 kWh die bij elke inschakeling op 0 begint.
Uit de tijd tussen twee stappen schatten we het gemiddelde vermogen (zie PowerEstimator).
Alleen uitlezen schrijft niets naar de unit; schrijven gebeurt uitsluitend als iemand in
het tabblad Airco op een knop drukt (en kan uit met AIRCO_CONTROL=0).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ..config import config
from ..db import get_db
from ..state import Source
from . import register

log = logging.getLogger(__name__)
DEFAULT_PORT = 51443

MODES = {"auto": 0, "cool": 1, "heat": 2, "fan": 3, "dry": 4}
MODE_NAMES = {v: k for k, v in MODES.items()}
# Ventilatorstanden. "quiet" is de laagste stap; de ECO-stand van de afstandsbediening
# bestaat niet in het wifi-protocol (staat ECO aan, dan meldt de unit de laagste stap).
FANS = {"auto": 0, "quiet": 1, "low": 2, "medium": 3, "high": 4}
FAN_NAMES = {v: k for k, v in FANS.items()}
# Luchtstroom: "3d" is 3D-auto (Entrust), de rest is de verticale lamel (1 = hoogst, 4 = laagst).
AIRS = {"swing": 0, "up": 1, "mid": 2, "down": 4}
AIR_NAMES = {0: "swing", 1: "up", 2: "mid", 3: "mid", 4: "down"}


def _has_3d(a: Any) -> bool:
    caps = getattr(a, "Capabilities", None)
    return bool(getattr(caps, "entrust", True))

FAN_ONLY_W = 25.0  # binnenunit aan, compressor stil
STEP_KWH = 0.25
MIN_W, MAX_W = 80.0, 3000.0


# ---------------- instellingen ----------------


@dataclass
class UnitConfig:
    id: str
    name: str
    host: str
    port: int = DEFAULT_PORT


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "airco"


def parse_units(spec: str) -> list[UnitConfig]:
    """"Naam=host[:poort];Naam=host" -> lijst. Lege of kapotte stukken worden overgeslagen."""
    out: list[UnitConfig] = []
    seen: set[str] = set()
    for part in (spec or "").split(";"):
        name, sep, addr = part.partition("=")
        name, addr = name.strip(), addr.strip()
        if not sep or not name or not addr:
            continue
        host, _, port = addr.partition(":")
        uid = slug(name)
        while uid in seen:
            uid += "-2"
        seen.add(uid)
        out.append(UnitConfig(uid, name, host.strip(), int(port) if port.strip().isdigit() else DEFAULT_PORT))
    return out


# ---------------- verbruik schatten ----------------


@dataclass
class PowerEstimator:
    """Schat het vermogen van één binnenunit uit de 0,25 kWh-teller en de compressorstatus.

    * uit                          -> 0 W
    * aan, compressor stil         -> FAN_ONLY_W
    * compressor draait            -> gemiddelde over de laatste teller-stap,
                                      of `default_w` zolang er nog geen stap is gezien
    De eerste stap na het inschakelen telt vanaf het moment van inschakelen, want de
    teller begint dan op 0. Draaide de unit al toen de app startte, dan is het begin van
    de lopende stap onbekend en meten we pas vanaf de eerstvolgende stap.
    """

    default_w: float = 500.0
    avg_w: float | None = None
    last_kwh: float | None = None
    step_from: float | None = None  # begin van de lopende stap; None = onbekend
    was_on: bool | None = None  # None = nog niet gezien (app net gestart)

    def update(self, now: float, on: bool, compressor: bool, kwh: float | None) -> float:
        if on and self.was_on is False:
            self.step_from = now  # net ingeschakeld: de teller begint opnieuw bij 0
            self.last_kwh = None
        self.was_on = on
        if kwh is not None:
            if self.last_kwh is not None and kwh < self.last_kwh:
                self.step_from = None  # teller gereset zonder dat wij het zagen: begin onbekend
            elif self.last_kwh is not None and kwh > self.last_kwh:
                if self.step_from is not None:
                    hours = (now - self.step_from) / 3600
                    if hours > 0:
                        w = (kwh - self.last_kwh) * 1000 / hours
                        if MIN_W <= w <= MAX_W:
                            self.avg_w = w if self.avg_w is None else 0.5 * self.avg_w + 0.5 * w
                self.step_from = now  # vanaf hier loopt een hele, meetbare stap
            self.last_kwh = kwh
        if not on:
            return 0.0
        if not compressor:
            return FAN_ONLY_W
        return self.avg_w if self.avg_w is not None else self.default_w


# ---------------- één binnenunit ----------------


@dataclass
class Unit:
    cfg: UnitConfig
    repo: Any = None
    airco_id: str | None = None
    aircon: Any = None  # pywfrac.Aircon
    registered: bool = False
    updated: float | None = None
    error: str | None = None
    est: PowerEstimator = field(default_factory=PowerEstimator)
    power_w: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def unit_view(u: Unit, now: float | None = None) -> dict[str, Any]:
    """Wat de app over een airco laat zien. Ook gebruikt door de demo."""
    now = now or time.time()
    a = u.aircon
    online = u.updated is not None and now - u.updated <= max(3 * config.airco_poll_s, 180)
    view: dict[str, Any] = {"id": u.cfg.id, "name": u.cfg.name, "online": online, "error": u.error}
    if a is None:
        return view
    mode = MODE_NAMES.get(int(a.OperationMode), "auto")
    lo, hi = _range(a, mode)
    err = str(a.ErrorCode or "").strip()
    view.update(
        on=bool(a.Operation),
        mode=mode,
        target=float(a.PresetTemp),
        fan=FAN_NAMES.get(int(a.AirFlow)),
        air="3d" if a.Entrust else AIR_NAMES.get(int(a.WindDirectionUD)),
        has_3d=_has_3d(a),
        room_temp=_t(a.IndoorTemp),
        outdoor_temp=_t(a.OutdoorTemp),
        compressor=bool(getattr(a, "CompressorRunning", False)),
        power_w=round(u.power_w, 1) if online else None,
        energy_run_kwh=a.Electric,
        min=lo,
        max=hi,
        fault=err if err and err.strip("0") else None,
    )
    return view


def _t(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f <= -50 or f >= 80 else round(f, 1)


def _range(a: Any, mode: str) -> tuple[float, float]:
    try:
        from pywfrac import OperationMode

        lo, hi = a.Capabilities.setpoint_range(OperationMode(MODES[mode]))
        return float(lo), float(hi)
    except Exception:  # noqa: BLE001
        return (16.0 if mode == "cool" else 18.0), 30.0


def command_params(a: Any, changes: dict[str, Any]) -> dict[Any, Any]:
    """Vertaalt {"on": bool, "mode": str, "target": float, "fan": str, "air": str} naar pywfrac-commando's."""
    from pywfrac import AirconCommands

    params: dict[Any, Any] = {}
    mode = changes.get("mode")
    if mode is not None:
        if mode not in MODES:
            raise ValueError(f"onbekende stand: {mode}")
        params[AirconCommands.OperationMode] = MODES[mode]
        params[AirconCommands.Operation] = True  # een stand kiezen zet de airco aan
    fan = changes.get("fan")
    if fan is not None:
        if fan not in FANS:
            raise ValueError(f"onbekende ventilatorstand: {fan}")
        params[AirconCommands.AirFlow] = FANS[fan]
    air = changes.get("air")
    if air is not None:
        if air == "3d":
            if not _has_3d(a):
                raise ValueError("deze airco heeft geen 3D-auto")
            params[AirconCommands.Entrust] = True
        elif air in AIRS:
            params[AirconCommands.Entrust] = False
            params[AirconCommands.WindDirectionUD] = AIRS[air]
        else:
            raise ValueError(f"onbekende luchtstroom: {air}")
    if "on" in changes and changes["on"] is not None:
        params[AirconCommands.Operation] = bool(changes["on"])
    target = changes.get("target")
    if target is not None:
        lo, hi = _range(a, mode or MODE_NAMES.get(int(a.OperationMode), "auto"))
        t = round(float(target) * 2) / 2  # de unit kent alleen halve graden
        if not lo <= t <= hi:
            raise ValueError(f"temperatuur moet tussen {lo:g} en {hi:g} °C liggen")
        params[AirconCommands.PresetTemp] = t
    if not params:
        raise ValueError("niets te veranderen")
    return params


# ---------------- de koppeling ----------------


# De module weigert te lange ID's met "501 Not supported this command", ook voor
# getDeviceInfo. De Smart M-Air-app en Home Assistant gebruiken een gewone UUID (36 tekens)
# als operatorId en een deviceId van rond de 32 tekens; daar blijven we binnen.
MAX_OPERATOR_ID = 36
MAX_DEVICE_ID = 32


def _ids() -> tuple[str, str]:
    """Vaste operator- en apparaat-ID voor deze installatie (bewaard in de database)."""
    db = get_db()
    op = db.get_kv("wfrac_operator_id") or ""
    dev = db.get_kv("wfrac_device_id") or ""
    if not op or len(op) > MAX_OPERATOR_ID:
        op = str(uuid.uuid4())
        db.set_kv("wfrac_operator_id", op)
    if not dev or len(dev) > MAX_DEVICE_ID:
        dev = f"wattwijs-{uuid.uuid4().hex[:12]}"
        db.set_kv("wfrac_device_id", dev)
    return op, dev


def _tz_name() -> str:
    return getattr(config.tz, "key", None) or "Europe/Amsterdam"


def publish(src: Source, units: list[Unit]) -> None:
    now = time.time()
    views = [unit_view(u, now) for u in units]
    online = [v for v in views if v.get("online")]
    if online:
        src.update(
            units=views,
            power_w=sum(v.get("power_w") or 0.0 for v in online),
            units_on=sum(1 for v in online if v.get("on")),
        )
    else:
        src.values["units"] = views  # laat ook offline units en hun fout zien
        errs = [f"{v['name']}: {v['error']}" for v in views if v.get("error")]
        src.error("; ".join(errs) or "geen verbinding")


async def _poll(u: Unit) -> None:
    from pywfrac import WfRacAccountTableFullError, WfRacError

    async with u.lock:
        try:
            if u.airco_id is None:
                u.airco_id = await u.repo.get_airco_id()
            if not u.registered and config.airco_control:
                try:
                    await u.repo.async_register(u.airco_id, _tz_name())
                    u.registered = True
                except WfRacAccountTableFullError:
                    u.error = ("de airco heeft geen plek meer voor een extra gebruiker; "
                               "verwijder een oude koppeling in de Smart M-Air-app")
                except WfRacError as exc:
                    log.info("Airco %s: aanmelden lukte nog niet (%s), volgende keer opnieuw", u.cfg.name, exc)
            status = await u.repo.async_get_status(u.airco_id)
            a = status.aircon
            u.aircon = a
            u.power_w = u.est.update(time.time(), bool(a.Operation), bool(getattr(a, "CompressorRunning", False)),
                                     a.Electric)
            u.updated = time.time()
            if u.registered or not config.airco_control:
                u.error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            from pywfrac import WfRacConnectionError, WfRacMalformedResponseError

            no_conn = isinstance(exc, WfRacConnectionError) and not isinstance(exc, WfRacMalformedResponseError)
            u.error = "geen verbinding; staat de airco onder stroom en klopt het IP-adres?" if no_conn else detail
            if u.updated is None or time.time() - u.updated > 300:
                log.warning("Airco %s (%s:%s): %s", u.cfg.name, u.cfg.host, u.cfg.port, detail)


@register("aircon", "wfrac", "Mitsubishi Heavy (WF-RAC / Smart M-Air)",
          env=("AIRCO_UNITS", "AIRCO_CONTROL", "AIRCO_POLL_SECONDS", "AIRCO_DEFAULT_W",
               "AIRCO_TIMER_MINUTES"),
          description="Lokaal via poort 51443. Uitlezen en (optioneel) aan/uit, stand en temperatuur.")
async def run(src: Source) -> None:
    from pywfrac import Repository, WfRacError

    cfgs = parse_units(config.airco_units)
    if not cfgs:
        src.error("AIRCO_UNITS is leeg of onleesbaar (verwacht: Naam=ip;Naam=ip)")
        return
    operator_id, device_id = _ids()
    connector = aiohttp.TCPConnector(limit_per_host=1)  # de module kan maar één verbinding tegelijk aan
    async with aiohttp.ClientSession(connector=connector) as session:
        units = [
            Unit(c, Repository(session, c.host, c.port, operator_id, device_id, time_zone=_tz_name()),
                 est=PowerEstimator(default_w=config.airco_default_w))
            for c in cfgs
        ]
        by_id = {u.cfg.id: u for u in units}

        async def control(unit_id: str, changes: dict[str, Any]) -> dict[str, Any]:
            if not config.airco_control:
                raise PermissionError("bedienen staat uit (AIRCO_CONTROL=0)")
            u = by_id.get(unit_id)
            if u is None:
                raise KeyError(unit_id)
            async with u.lock:
                if u.aircon is None or u.airco_id is None:
                    raise ConnectionError("de airco is nog niet bereikbaar geweest")
                params = command_params(u.aircon, changes)
                try:
                    a = await u.repo.async_send_command(u.airco_id, u.aircon, params)
                except WfRacError as exc:
                    raise ConnectionError(str(exc)) from exc
                u.aircon = a
                u.registered = True
                u.power_w = u.est.update(time.time(), bool(a.Operation), bool(getattr(a, "CompressorRunning", False)),
                                         a.Electric)
                u.updated = time.time()
                u.error = None
            publish(src, units)
            return unit_view(u)

        src.control = control
        while True:
            for u in units:
                await _poll(u)
            publish(src, units)
            await asyncio.sleep(config.airco_poll_s)
