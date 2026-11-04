"""Pushmeldingen naar de webapp op het beginscherm (Web Push met VAPID).

Er is geen externe dienst nodig: de server versleutelt het bericht en stuurt het
naar de pushdienst van het toestel (bij een iPhone die van Apple).

Soorten meldingen (per telefoon aan of uit te zetten):
  morning   ochtendtip: goedkoopste tijdvak, gisteren, en een eventueel defect paneel
  panels    zonnepaneel kapot of te laag (direct na de avondcontrole)
  negative  morgen een negatieve stroomprijs
  records   dagrecord opwek, (bijna) zelfvoorzienende dag, mijlpalen
  weekly    weekoverzicht op zondagavond
  tech      een apparaat geeft al een tijd geen gegevens (voor de beheerder)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any

from .config import config
from .db import get_db

log = logging.getLogger(__name__)

KINDS: list[dict[str, Any]] = [
    {"key": "morning", "label": "Ochtendbericht: zonverwachting en het beste moment om stroom te gebruiken", "default": True},
    {"key": "panels", "label": "Zonnepaneel kapot of levert te weinig op", "default": True},
    {"key": "negative", "label": "Morgen een negatieve stroomprijs", "default": True},
    {"key": "export", "label": "Je levert stroom terug: goed moment om iets aan te zetten (vanaf 2027)", "default": True},
    {"key": "records", "label": "Avondoverzicht: opwek, verbruik en eigen stroom van vandaag", "default": True},
    {"key": "weekly", "label": "Weekoverzicht op zondagavond", "default": True},
    {"key": "tech", "label": "Technisch: apparaat geeft geen gegevens", "default": False},
]
DEFAULT_PREFS = {k["key"]: k["default"] for k in KINDS}


# ---------------- sleutels ----------------


def _key_path() -> str:
    return os.path.join(config.data_dir, "vapid_private.pem")


def vapid() -> Any:
    from py_vapid import Vapid02

    path = _key_path()
    if not os.path.exists(path):
        v = Vapid02()
        v.generate_keys()
        os.makedirs(config.data_dir, exist_ok=True)
        v.save_key(path)
        os.chmod(path, 0o600)
        log.info("Nieuwe sleutels voor pushmeldingen aangemaakt")
    return Vapid02.from_file(path)


def public_key() -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _contact() -> str:
    c = config.push_contact or config.admin_to.split(",")[0].strip() or config.mail_from
    if not c:
        return "mailto:wattwijs@example.invalid"
    return c if c.startswith(("mailto:", "https:")) else f"mailto:{c}"


# ---------------- abonnementen ----------------


def subscribe(sub: dict[str, Any], prefs: dict[str, bool] | None, label: str | None) -> dict[str, bool]:
    if not isinstance(sub, dict) or not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
        raise ValueError("ongeldig abonnement")
    merged = {**DEFAULT_PREFS, **{k: bool(v) for k, v in (prefs or {}).items() if k in DEFAULT_PREFS}}
    get_db().x(
        "INSERT INTO push_sub(endpoint, sub, prefs, label, created) VALUES(?,?,?,?,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET sub=excluded.sub, label=COALESCE(excluded.label, push_sub.label)",
        (sub["endpoint"], json.dumps(sub), json.dumps(merged), label, time.time()),
    )
    return get_prefs(sub["endpoint"]) or merged


def get_prefs(endpoint: str) -> dict[str, bool] | None:
    r = get_db().q("SELECT prefs FROM push_sub WHERE endpoint=?", (endpoint,))
    return {**DEFAULT_PREFS, **json.loads(r[0]["prefs"])} if r else None


def set_prefs(endpoint: str, prefs: dict[str, bool]) -> dict[str, bool] | None:
    cur = get_prefs(endpoint)
    if cur is None:
        return None
    cur.update({k: bool(v) for k, v in prefs.items() if k in DEFAULT_PREFS})
    get_db().x("UPDATE push_sub SET prefs=? WHERE endpoint=?", (json.dumps(cur), endpoint))
    return cur


def unsubscribe(endpoint: str) -> None:
    get_db().x("DELETE FROM push_sub WHERE endpoint=?", (endpoint,))


def subscriptions(kind: str | None = None) -> list[dict[str, Any]]:
    out = []
    for r in get_db().q("SELECT * FROM push_sub"):
        prefs = {**DEFAULT_PREFS, **json.loads(r["prefs"])}
        if kind is None or prefs.get(kind):
            out.append({**r, "sub": json.loads(r["sub"]), "prefs": prefs})
    return out


# ---------------- versturen ----------------


def _send_sync(sub: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=vapid(),
            vapid_claims={"sub": _contact()},
            ttl=12 * 3600,
            timeout=15,
        )
        return True, "ok"
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        return False, f"{status or ''} {exc}".strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def send_to(endpoint: str, title: str, body: str, url: str = "/", tag: str | None = None) -> tuple[bool, str]:
    r = get_db().q("SELECT sub FROM push_sub WHERE endpoint=?", (endpoint,))
    if not r:
        return False, "onbekend abonnement"
    return await _deliver(json.loads(r[0]["sub"]), {"title": title, "body": body, "url": url, "tag": tag})


async def _deliver(sub: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
    ok, msg = await asyncio.to_thread(_send_sync, sub, payload)
    db = get_db()
    if ok:
        db.x("UPDATE push_sub SET last_ok=?, last_error=NULL WHERE endpoint=?", (time.time(), sub["endpoint"]))
    elif msg.startswith(("404", "410")):
        # Abonnement bestaat niet meer (app verwijderd of meldingen uitgezet).
        db.x("DELETE FROM push_sub WHERE endpoint=?", (sub["endpoint"],))
        log.info("Verlopen push-abonnement verwijderd")
    else:
        db.x("UPDATE push_sub SET last_error=? WHERE endpoint=?", (msg[:300], sub["endpoint"]))
        log.warning("Pushmelding versturen mislukt: %s", msg)
    return ok, msg


async def broadcast(kind: str, title: str, body: str, url: str = "/", tag: str | None = None) -> int:
    """Stuur naar alle telefoons die deze soort melding aan hebben staan. Geeft het aantal geslaagde terug."""
    if not config.push_enabled:
        return 0
    sent = 0
    for s in subscriptions(kind):
        ok, _ = await _deliver(s["sub"], {"title": title, "body": body, "url": url, "tag": tag or kind})
        sent += ok
    if sent:
        log.info("Pushmelding '%s' verstuurd naar %d telefoon(s)", title, sent)
    return sent
