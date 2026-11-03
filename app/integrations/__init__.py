"""Integration registry: one module per brand/protocol.

Every integration fills one *role*:

    grid      net power at the meter (P1 dongle)           -> state.grid
    battery   home battery                                 -> state.battery
    solar     live PV production (inverter)                -> state.solar
    heatpump  heat pump power and temperatures             -> state.heatpump
    panels    per-panel (optimizer/micro-inverter) energy   -> daily panel check
    prices    dynamic electricity prices                   -> costs, tips

Live roles (grid, battery, solar, heatpump) register an ``async run(source)``
coroutine that keeps writing values into the given ``Source`` (see
CONTRIBUTING.md for the exact fields per role). ``panels`` and ``prices``
register a factory that returns an object with the methods the core calls.

Which integration is used per role is set with ``<ROLE>_INTEGRATION``
(e.g. ``GRID_INTEGRATION=homewizard``). When that is empty, the choice is
inferred from the older per-device settings (``P1_HOST``, ``BATTERY_HOST``,
...) so existing configurations keep working.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

LIVE_ROLES = ("grid", "battery", "solar", "heatpump", "aircon", "heating")
ROLES = (*LIVE_ROLES, "panels", "prices")


@dataclass
class Integration:
    role: str
    key: str
    title: str
    run: Callable[..., Awaitable[None]] | None = None  # live roles: async run(source)
    factory: Callable[[], Any] | None = None  # panels / prices: returns a provider object
    env: tuple[str, ...] = ()  # documented settings, shown in the README table
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


REGISTRY: dict[tuple[str, str], Integration] = {}


def register(role: str, key: str, title: str, *, env: tuple[str, ...] = (), description: str = ""):
    """Decorator. Use on ``async def run(source)`` (live roles) or on a provider factory/class."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")

    def deco(obj):
        integ = Integration(role, key, title, env=env, description=description)
        if role in LIVE_ROLES:
            integ.run = obj
        else:
            integ.factory = obj
        REGISTRY[(role, key)] = integ
        return obj

    return deco


_loaded = False


def load_all() -> None:
    """Import every module in this package so their @register calls run."""
    global _loaded
    if _loaded:
        return
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{__name__}.{mod.name}")
    _loaded = True


def get(role: str, key: str) -> Integration:
    load_all()
    try:
        return REGISTRY[(role, key)]
    except KeyError:
        options = ", ".join(sorted(k for r, k in REGISTRY if r == role)) or "none"
        raise ValueError(f"unknown {role} integration {key!r} (available: {options})") from None


def available(role: str | None = None) -> list[Integration]:
    load_all()
    return [i for (r, _), i in sorted(REGISTRY.items()) if role is None or r == role]


def selected(role: str) -> str | None:
    """The integration key chosen for a role, or None when the role is not used."""
    from ..config import config

    explicit = os.getenv(f"{role.upper()}_INTEGRATION", "").strip().lower()
    if explicit in ("none", "off", "0"):
        return None
    if explicit:
        return explicit
    if config.demo:
        return "demo"
    # Backwards compatible: derive from the per-device settings.
    if role == "grid" and config.p1_host:
        return config.p1_type or "smeter"
    if role == "battery" and config.battery_host:
        return "aecc"
    if role == "solar":
        if config.se_modbus_host:
            return "solaredge_modbus"
        if config.se_username and config.se_password and config.se_site_id:
            return "solaredge_cloud"
    if role == "panels" and config.se_username and config.se_password and config.se_site_id:
        return "solaredge"
    if role == "heatpump" and config.dw_username and config.dw_password:
        return "dewarmte"
    if role == "aircon" and config.airco_units:
        return "wfrac"
    if role == "heating" and config.tado_enabled:
        return "tado"
    if role == "prices" and config.prices_enabled:
        return "frank"
    return None
