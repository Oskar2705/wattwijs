"""Energiemonitor - webserver en achtergrondtaken."""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import integrations, mailer, notifier, panels, prices, push, recorder
from .config import config
from .db import ENERGY_COLS, get_db, month_days
from .state import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("energiemonitor")
logging.getLogger("solaredge_web").setLevel(logging.CRITICAL)  # fouten loggen we zelf, zonder lange traceback
STATIC = Path(__file__).parent / "static"


# ---------------- achtergrond ----------------


async def watchdog() -> None:
    """Mailt de beheerder als een bron lang onbereikbaar is (en weer terug is)."""
    offline_since: dict[str, float] = {}
    notified: set[str] = set()
    names = ROLE_NAMES
    await asyncio.sleep(120)
    while True:
        now = time.time()
        for key, src in state.sources().items():
            if not src.configured:
                continue
            if src.fresh(now):
                offline_since.pop(key, None)
                if key in notified:
                    notified.discard(key)
                    await push.broadcast("tech", f"{names[key]} is weer bereikbaar", "Er komen weer gegevens binnen.")
                    await mailer.send(config.split(config.admin_to), f"Energiemonitor: {names[key]} is weer bereikbaar",
                                      f"{names[key]} levert weer gegevens." + mailer.footer())
                continue
            offline_since.setdefault(key, now)
            if key not in notified and now - offline_since[key] > config.offline_alert_minutes * 60:
                notified.add(key)
                await push.broadcast("tech", f"{names[key]} geeft geen gegevens",
                                     f"Al {config.offline_alert_minutes} minuten niets ontvangen. {src.last_error or ''}".strip())
                await mailer.send(
                    config.split(config.admin_to),
                    f"Energiemonitor: {names[key]} geeft geen gegevens",
                    f"Al {config.offline_alert_minutes} minuten geen gegevens van {names[key]}.\n"
                    f"Laatste foutmelding: {src.last_error or 'onbekend'}" + mailer.footer(),
                )
        await asyncio.sleep(60)


ROLE_NAMES = {"grid": "slimme meter", "battery": "thuisbatterij", "solar": "zonnepanelen", "heatpump": "warmtepomp"}
SELECTED: dict[str, str | None] = {}


def start_tasks() -> list[asyncio.Task]:
    """Start per rol de gekozen integratie (zie app/integrations)."""
    tasks = [recorder.run(), watchdog()]
    for role in integrations.ROLES:
        key = integrations.selected(role)
        SELECTED[role] = key
        if not key:
            continue
        integ = integrations.get(role, key)
        if role in integrations.LIVE_ROLES:
            src = state.sources()[role]
            src.configured = True
            src.mode = integ.title
            tasks.append(integ.run(src))
        elif role == "panels":
            tasks.append(panels.run(integ.factory()))
        elif role == "prices":
            tasks.append(prices.run(integ.factory()))
    if config.push_enabled:
        tasks.append(notifier.run())
    return [asyncio.create_task(t) for t in tasks]


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_db()
    tasks = start_tasks()
    log.info(
        "Start: demo=%s %s mail=%s push=%s",
        config.demo, " ".join(f"{r}={k or '-'}" for r, k in SELECTED.items()), config.mail_enabled, config.push_enabled,
    )
    yield
    for t in tasks:
        t.cancel()
    recorder.recorder.flush()


app = FastAPI(title="Energiemonitor", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.basic_auth_user and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.basic_auth_user) and secrets.compare_digest(
                    pw, config.basic_auth_pass
                )
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Energie"'})
    return await call_next(request)


# ---------------- API ----------------


def today() -> date:
    return datetime.now(config.tz).date()


def _parse_day(day: str | None) -> str:
    if not day:
        return today().isoformat()
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError as exc:
        raise HTTPException(400, "ongeldige datum") from exc


COST_COLS = ["cost_import", "revenue_export", "battery_value"]


def _with_ratios(t: dict[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {c: round(t.get(c, 0.0) or 0.0, 1) for c in ENERGY_COLS}
    priced = t.get("priced_minutes") or 0
    if priced:
        for c in COST_COLS:
            out[c] = round(t.get(c) or 0.0, 4)
        out["net_cost"] = round(out["cost_import"] - out["revenue_export"], 4)
        out["price_coverage"] = round(priced / max(t.get("minutes") or 1, 1), 3)
    else:
        for c in [*COST_COLS, "net_cost", "price_coverage"]:
            out[c] = None
    house, imp, solar, exp = out["house_wh"], out["import_wh"], out["solar_wh"], out["export_wh"]
    out["self_sufficiency"] = round(max(0.0, 1 - imp / house) * 100, 0) if house > 50 else None
    out["self_consumption"] = round(max(0.0, 1 - exp / solar) * 100, 0) if solar > 50 else None
    return out


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/live")
def live() -> dict[str, Any]:
    db = get_db()
    snap = state.snapshot()
    t = db.totals_for_day(today().isoformat())
    for k, v in recorder.recorder.pending_energy().items():
        t[k] = (t.get(k) or 0.0) + v
    return {
        "now": snap,
        "price": prices.price_info() if SELECTED.get("prices") else None,
        "price_error": (db.get_kv("price_error") or None) if SELECTED.get("prices") else None,
        "contract_start": config.contract_start,
        "today": _with_ratios(t),
        "sources": {k: s.status() for k, s in state.sources().items()},
        "open_alerts": db.open_alerts(),
        "site_name": config.site_name,
        "demo": config.demo,
    }


@app.get("/api/curve")
def curve(day: str | None = None) -> dict[str, Any]:
    d = _parse_day(day)
    return {"day": d, "points": get_db().curve(d)}


@app.get("/api/history")
def history(mode: str = "hour", key: str | None = None) -> dict[str, Any]:
    db = get_db()
    t = today()
    if mode == "hour":
        key = _parse_day(key)
        rows = {r["bucket"]: r for r in db.by_hour(key)}
        buckets = list(range(24))
    elif mode == "day":
        key = key or t.strftime("%Y-%m")
        rows = {r["bucket"]: r for r in db.by_day(key)}
        buckets = month_days(key)
    elif mode == "month":
        key = key or str(t.year)
        rows = {r["bucket"]: r for r in db.by_month(key)}
        buckets = [f"{key}-{m:02d}" for m in range(1, 13)]
    else:
        raise HTTPException(400, "mode moet hour, day of month zijn")
    out = []
    for b in buckets:
        r = rows.get(b)
        out.append({"bucket": b, **(_with_ratios(r) if r else {c: None for c in [*ENERGY_COLS, *COST_COLS, "net_cost"]})})
    sum_cols = [*ENERGY_COLS, *COST_COLS, "priced_minutes", "minutes"]
    total = {c: sum((r.get(c) or 0.0) for r in rows.values()) for c in sum_cols}
    return {
        "mode": mode, "key": key, "rows": out, "total": _with_ratios(total), "first_day": db.first_day(),
        "contract_start": config.contract_start,
    }


@app.get("/api/prices")
def price_list(day: str | None = None) -> dict[str, Any]:
    """Kwartierprijzen voor een dag en de dag erna."""
    d = date.fromisoformat(_parse_day(day))
    start = int(datetime.combine(d, datetime.min.time(), tzinfo=config.tz).timestamp())
    end = int(datetime.combine(d + timedelta(days=2), datetime.min.time(), tzinfo=config.tz).timestamp())
    db = get_db()
    return {
        "day": d.isoformat(),
        "start": start,
        "end": end,
        "prices": db.prices_between(start, end),
        "error": db.get_kv("price_error") or None,
        "enabled": bool(SELECTED.get("prices")),
    }


@app.get("/api/panels")
def panel_view(day: str | None = None) -> dict[str, Any]:
    db = get_db()
    days = db.panel_days()
    d = day or (days[-1] if days else today().isoformat())
    rows = db.panel_day(d)
    ev = panels.evaluate_stored_day(d) if rows else {}
    alerts = {a["serial"] for a in db.open_alerts()}
    for r in rows:
        r.update(ev.get(r["serial"], {}))
        r["name"] = panels.display_name(r["name"] or r["serial"])
        r["alert"] = r["serial"] in alerts
    rows.sort(key=lambda r: (r["grp"] or "", panels.natural_key(r["name"])))
    fetched_at = db.get_kv("panel_fetched_at") or None
    if d == today().isoformat() and SELECTED.get("panels"):
        panels.request_refresh()  # the loop throttles this to once per few minutes
    return {
        "fetched_at": fetched_at if d == today().isoformat() else None,
        "day": d,
        "layout": panels.parse_layout(),
        "days": days,
        "panels": rows,
        "final": bool(rows and all(r["final"] for r in rows)),
        "threshold_pct": config.panel_threshold_pct,
        "consecutive_days": config.panel_consecutive_days,
        "check_time": config.panel_check_time,
        "open_alerts": [{**a, "name": panels.display_name(a["name"] or "")} for a in db.open_alerts()],
        "recent_alerts": [{**a, "name": panels.display_name(a["name"] or "")} for a in db.recent_alerts()],
        "configured": bool(SELECTED.get("panels")),
        "dead_share": panels.DEAD_SHARE,
        "error": db.get_kv("panel_error") or None,
    }


# ---------------- pushmeldingen ----------------


@app.get("/api/push/config")
def push_config() -> dict[str, Any]:
    return {"enabled": config.push_enabled, "public_key": push.public_key() if config.push_enabled else None,
            "kinds": push.KINDS}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request) -> dict[str, Any]:
    body = await request.json()
    try:
        prefs = push.subscribe(body.get("subscription"), body.get("prefs"), body.get("label"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"prefs": prefs}


@app.post("/api/push/status")
async def push_status(request: Request) -> dict[str, Any]:
    body = await request.json()
    return {"prefs": push.get_prefs(body.get("endpoint") or "")}


@app.post("/api/push/prefs")
async def push_prefs(request: Request) -> dict[str, Any]:
    body = await request.json()
    prefs = push.set_prefs(body.get("endpoint") or "", body.get("prefs") or {})
    if prefs is None:
        raise HTTPException(404, "onbekend abonnement")
    return {"prefs": prefs}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request) -> dict[str, str]:
    body = await request.json()
    push.unsubscribe(body.get("endpoint") or "")
    return {"status": "ok"}


@app.post("/api/push/test")
async def push_test(request: Request) -> dict[str, Any]:
    body = await request.json()
    endpoint = body.get("endpoint") or ""
    msg = notifier.build_morning(datetime.now(config.tz)) if body.get("kind") == "morning" else None
    title, text = msg or ("👋 Het werkt!", "Je krijgt voortaan meldingen van Energie Thuis op deze telefoon.")
    ok, detail = await push.send_to(endpoint, title, text, url="/#nu", tag="test")
    if not ok:
        raise HTTPException(502, f"versturen mislukt: {detail}")
    return {"status": "verstuurd"}


@app.get("/api/export.csv")
def export_csv(start: str, end: str) -> Response:
    s, e = _parse_day(start), _parse_day(end)
    rows = get_db().hourly_export(s, e)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    labels = ["datum", "uur", "afname_kwh", "teruglevering_kwh", "opwek_kwh", "batterij_geladen_kwh",
              "batterij_ontladen_kwh", "warmtepomp_kwh", "verbruik_huis_kwh",
              "kosten_afname_eur", "opbrengst_teruglevering_eur", "netto_eur"]
    w.writerow(labels)
    nl = lambda v, n: "" if v is None else f"{v:.{n}f}".replace(".", ",")  # noqa: E731
    for r in rows:
        priced = bool(r.get("priced_minutes"))
        cost = r["cost_import"] if priced else None
        rev = r["revenue_export"] if priced else None
        net = None if cost is None else cost - (rev or 0)
        w.writerow([r["day"], r["hour"], *[nl((r[c] or 0) / 1000, 3) for c in ENERGY_COLS], nl(cost, 4), nl(rev, 4), nl(net, 4)])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="energie_{s}_{e}.csv"'},
    )


@app.get("/api/status")
def status() -> JSONResponse:
    return JSONResponse({
        "sources": {k: s.status() for k, s in state.sources().items()},
        "integrations": SELECTED,
    })


@app.get("/api/integrations")
def integrations_list() -> list[dict[str, Any]]:
    """Alle beschikbare integraties, per rol (handig voor de README en voor bijdragers)."""
    return [{"role": i.role, "key": i.key, "title": i.title, "env": list(i.env), "description": i.description}
            for i in integrations.available()]


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


def _asset_version() -> str:
    """Verandert zodra app.js of style.css verandert, zodat browsers geen oude versie gebruiken."""
    import hashlib

    h = hashlib.sha1()
    for name in ("app.js", "style.css"):
        h.update((STATIC / name).read_bytes())
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") and not request.url.path.startswith("/static/fonts/"):
        response.headers["Cache-Control"] = "no-cache"  # altijd even controleren (goedkoop dankzij ETag)
    return response


@app.get("/")
def index() -> Response:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("/static/style.css", f"/static/style.css?v={ASSET_VERSION}")
    html = html.replace("/static/app.js", f"/static/app.js?v={ASSET_VERSION}")
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})
