"""Controle per zonnepaneel: levert een paneel minder op dan het zou moeten?

Panelen op één dak verschillen van nature (rij, schaduw, stand van de zon), en
welke rij het best presteert verschilt per dag. Eerlijk vergelijken kan dus
alleen met panelen die ernaast liggen. Daarvoor dient PANEL_LAYOUT, de fysieke
indeling zoals in het SolarEdge-portaal:

    PANEL_LAYOUT=1.1.1,1.1.2,1.1.3,1.1.4,1.1.5;1.1.8,1.1.7,1.1.6;1.1.9,1.1.10,1.1.11

Per paneel en per dag:
  * aandeel = opbrengst / mediaan van de andere panelen in dezelfde rij
  * normaal = mediaan van dat aandeel over de afgelopen PANEL_BASELINE_DAYS
              meetbare dagen (vaste schaduw van een buurpaneel valt zo weg);
              de ondergrens is het 20e percentiel (de slechtere gewone dagen)
  * te laag = aandeel minstens PANEL_THRESHOLD_PERCENT onder die ondergrens;
              zo geeft een randpaneel dat per dag wisselt geen valse meldingen
  * een kapot paneel telt niet mee als maatstaf voor zijn buren
  * kapot   = minder dan PANEL_DEAD_SHARE van de rest, altijd en ook op donkere dagen

Zonder PANEL_LAYOUT geldt alleen de regel 'kapot': dat is de enige vergelijking
die over verschillende rijen heen betrouwbaar is.
Pas na PANEL_CONSECUTIVE_DAYS meetbare dagen op rij gaat er één mail uit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from . import mailer
from .config import config
from .db import get_db

log = logging.getLogger(__name__)

OK, LOW, SKIP = "ok", "low", "skip"
BASELINE_DAYS = int(os.getenv("PANEL_BASELINE_DAYS", "30"))
DEAD_SHARE = float(os.getenv("PANEL_DEAD_SHARE", "0.25"))
MIN_BASELINE_DAYS = 5


def display_name(name: str) -> str:
    return name[len("Optimizer "):] if name.startswith("Optimizer ") else name


def natural_key(name: str) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name or "")]


DEMO_LAYOUT = "1.1.1,1.1.2,1.1.3,1.1.4,1.1.5,1.1.6;1.2.1,1.2.2,1.2.3,1.2.4,1.2.5,1.2.6"


def parse_layout(text: str | None = None) -> list[list[str]]:
    if text is None:
        text = config.panel_layout or (DEMO_LAYOUT if config.demo else "")
    return [[p.strip() for p in row.split(",") if p.strip()] for row in (text or "").split(";") if row.strip()]


def row_of(layout: list[list[str]]) -> dict[str, int]:
    return {name: i for i, row in enumerate(layout) for name in row}


def peer_reference(rows: list[dict[str, Any]], layout: list[list[str]] | None = None) -> dict[str, tuple[float, str]]:
    """serial -> (mediaan van de vergelijkbare andere panelen, omschrijving).

    Met een indeling: de andere panelen in dezelfde rij (minstens 2). Anders: alle andere panelen.
    """
    layout = parse_layout() if layout is None else layout
    rows_by = row_of(layout)
    groups: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(rows_by.get(display_name(r.get("name") or "")), []).append(r)
    def compute(exclude: set[str]) -> dict[str, tuple[float, str]]:
        out = {}
        for r in rows:
            key = rows_by.get(display_name(r.get("name") or ""))
            peers = groups[key] if key is not None and len(groups[key]) >= 3 else rows
            label = f"rij {key + 1}" if key is not None and len(groups[key]) >= 3 else "alle panelen"
            others = [p["wh"] or 0.0 for p in peers if p["serial"] != r["serial"] and p["serial"] not in exclude]
            if len(others) < 2:  # te weinig buren over: dan toch iedereen meenemen
                others = [p["wh"] or 0.0 for p in peers if p["serial"] != r["serial"]]
            out[r["serial"]] = (statistics.median(others) if others else 0.0, label)
        return out

    first = compute(set())
    # Een kapot paneel mag de maatstaf voor zijn buren niet omlaag trekken.
    dead = {r["serial"] for r in rows if first[r["serial"]][0] > 0 and (r["wh"] or 0.0) < DEAD_SHARE * first[r["serial"]][0]}
    return compute(dead) if dead else first


def evaluate_day(
    rows: list[dict[str, Any]],
    threshold_pct: float,
    min_reference_wh: float,
    layout: list[list[str]] | None = None,
    baselines: dict[str, Any] | None = None,
    dead_share: float = DEAD_SHARE,
) -> dict[str, dict[str, Any]]:
    """rows: [{serial, name, wh}] -> {serial: {status, deviation, reference_wh, share, baseline, reason, compared_with}}"""
    layout = parse_layout() if layout is None else layout
    relative = bool(layout)
    baselines = baselines or {}
    result: dict[str, dict[str, Any]] = {}
    if len(rows) < 3:
        for r in rows:
            result[r["serial"]] = {"status": SKIP, "deviation": None, "reference_wh": None, "share": None,
                                   "baseline": None, "compared_with": "te weinig panelen", "reason": None}
        return result

    refs = peer_reference(rows, layout)
    for r in rows:
        ref, label = refs[r["serial"]]
        wh = r["wh"] or 0.0
        share = wh / ref if ref > 0 else None
        raw = baselines.get(r["serial"]) if relative and label != "alle panelen" else None
        base, low = (raw if isinstance(raw, tuple) else (raw, raw)) if raw is not None else (None, None)
        expected_wh = ref * (base if base is not None else 1.0)
        dev = (wh - expected_wh) / expected_wh * 100.0 if expected_wh > 0 else None
        # Oordeel t.o.v. de ondergrens van de normale spreiding (de slechtere gewone dagen).
        floor_wh = ref * low if low is not None else None
        below = (wh - floor_wh) / floor_wh * 100.0 if floor_wh else None

        reason = None
        if ref < 20:
            status = SKIP
        elif share is not None and share < dead_share:
            status, reason = LOW, "dead"  # levert (bijna) niets
        elif ref < min_reference_wh:
            status = SKIP
        elif below is not None and below <= -threshold_pct:
            status, reason = LOW, "low"
        else:
            status = OK
        show = base is not None or reason == "dead"
        result[r["serial"]] = {
            "status": status,
            "deviation": None if (dev is None or not show) else round(dev, 1),
            "reference_wh": round(expected_wh, 1) if base is not None else None,
            "share": None if share is None else round(share, 3),
            "baseline": None if base is None else round(base, 3),
            "compared_with": label,
            "reason": reason,
        }
    return result


def streak(statuses: list[str], needed: int) -> tuple[bool, bool]:
    """statuses: nieuwste eerst. Geeft (probleem, hersteld) terug."""
    meaningful = [s for s in statuses if s != SKIP]
    problem = len(meaningful) >= needed and all(s == LOW for s in meaningful[:needed])
    recovered = bool(meaningful) and meaningful[0] == OK
    return problem, recovered


def baseline_from(shares: list[float]) -> tuple[float, float] | None:
    """(typisch aandeel, ondergrens = 20e percentiel) of None bij te weinig dagen."""
    if len(shares) < MIN_BASELINE_DAYS:
        return None
    v = sorted(shares)
    return statistics.median(v), v[int(0.2 * (len(v) - 1))]


def baselines_for(day: str, layout: list[list[str]] | None = None) -> dict[str, tuple[float, float] | None]:
    """Normaal aandeel per paneel binnen zijn rij, uit de afgesloten dagen vóór `day`."""
    layout = parse_layout() if layout is None else layout
    if not layout:
        return {}
    db = get_db()
    days = [d for d in db.final_panel_days_before(day, BASELINE_DAYS + 1) if d < day][:BASELINE_DAYS]
    freeze = {}
    for a in db.open_alerts():
        f = date.fromisoformat(a["opened_day"]) - timedelta(days=config.panel_consecutive_days)
        freeze[a["serial"]] = f.isoformat()
    shares: dict[str, list[float]] = {}
    for d in days:
        rows = db.panel_day(d)
        if len(rows) < 3:
            continue
        refs = peer_reference(rows, layout)
        for r in rows:
            ref, _ = refs[r["serial"]]
            if ref < config.panel_min_reference_wh:
                continue
            if r["serial"] in freeze and d > freeze[r["serial"]]:
                continue
            shares.setdefault(r["serial"], []).append((r["wh"] or 0.0) / ref)
    return {s: baseline_from(v) for s, v in shares.items()}


def evaluate_stored_day(day: str) -> dict[str, dict[str, Any]]:
    rows = get_db().panel_day(day)
    layout = parse_layout()
    return evaluate_day(rows, config.panel_threshold_pct, config.panel_min_reference_wh, layout, baselines_for(day, layout))


def revalidate_open_alerts() -> int:
    """Sluit (zonder mail) open meldingen die volgens de huidige regels niet meer kloppen.

    Nodig na een wijziging van de regels of van PANEL_LAYOUT.
    """
    db = get_db()
    alerts = db.open_alerts()
    if not alerts:
        return 0
    days = db.final_panel_days_before("9999-12-31", config.panel_consecutive_days + 10)
    per_day = {d: evaluate_stored_day(d) for d in days}
    removed = 0
    for a in alerts:
        statuses = [per_day[d].get(a["serial"], {}).get("status", SKIP) for d in days]
        problem, _ = streak(statuses, config.panel_consecutive_days)
        if not problem:
            db.x("DELETE FROM panel_alert WHERE id=?", (a["id"],))
            removed += 1
    if removed:
        log.info("%d paneelmelding(en) ingetrokken na herbeoordeling", removed)
    return removed


async def run_check(day: str, send_mail: bool = True) -> list[str]:
    """Bekijkt de afgesloten dagen t/m `day` en opent/sluit meldingen. Geeft meldingsteksten terug."""
    db = get_db()
    days = db.final_panel_days_before(day, config.panel_consecutive_days + 10)
    if not days:
        return []
    per_day = {d: evaluate_stored_day(d) for d in days}
    rows_today = {r["serial"]: r for r in db.panel_day(days[0])}
    open_alerts = {a["serial"]: a for a in db.open_alerts()}
    messages: list[str] = []

    for serial, row in rows_today.items():
        statuses = [per_day[d].get(serial, {}).get("status", SKIP) for d in days]
        problem, recovered = streak(statuses, config.panel_consecutive_days)
        latest = per_day[days[0]].get(serial, {})
        if problem and serial not in open_alerts:
            history = []
            for d in days[: config.panel_consecutive_days + 3]:
                r = next((x for x in db.panel_day(d) if x["serial"] == serial), None)
                e = per_day[d].get(serial, {})
                if r:
                    dev = e.get("deviation")
                    history.append(
                        f"  {d}: {r['wh'] / 1000:.2f} kWh (normaal voor dit paneel {(e.get('reference_wh') or 0) / 1000:.2f} kWh"
                        + (f", {dev:+.0f}%" if dev is not None else "")
                        + ")"
                    )
            name = display_name(row["name"])
            if latest.get("reason") == "dead":
                what = "levert (bijna) niets meer op"
                intro = (f"Paneel {name} (optimizer {serial}, {row['grp']}) levert al "
                         f"{config.panel_consecutive_days} dagen op rij (bijna) niets op, terwijl de andere panelen wel opwekken.")
            else:
                what = "levert te weinig op"
                intro = (f"Paneel {name} (optimizer {serial}, {row['grp']}) wekt al "
                         f"{config.panel_consecutive_days} zonnige dagen op rij minstens "
                         f"{config.panel_threshold_pct:.0f}% minder op dan het normaal doet, vergeleken met de "
                         f"panelen naast hem in dezelfde rij.")
            subject = f"Zonnepaneel {name} {what}"
            body = (
                intro + "\n\n"
                "Opbrengst de afgelopen dagen:\n" + "\n".join(history) + "\n\n"
                "Mogelijke oorzaken: een defect paneel of optimizer, een losse kabel, vuil of schaduw "
                "(bijvoorbeeld een boom of schoorsteen). Als de afwijking blijft, is het verstandig de "
                "installateur te bellen."
                + mailer.footer()
            )
            status = await mailer.send(config.split(config.alert_to), subject, body) if send_mail else "niet verstuurd"
            if send_mail:
                from . import push

                short = (f"Paneel {name} levert al {config.panel_consecutive_days} dagen (bijna) niets op."
                         if latest.get("reason") == "dead" else
                         f"Paneel {name} wekt al {config.panel_consecutive_days} zonnige dagen duidelijk minder op dan normaal.")
                await push.broadcast("panels", f"⚠ {subject}", short + " Bel de installateur als het zo blijft.",
                                     url="/#panelen", tag=f"panel-{serial}")
            db.x(
                "INSERT INTO panel_alert(serial,name,opened_day,deviation,mail_status) VALUES(?,?,?,?,?)",
                (serial, display_name(row["name"]), days[0],
                 round((latest["share"] - 1) * 100, 1) if latest.get("reason") == "dead" and latest.get("share") is not None
                 else latest.get("deviation"), status),
            )
            messages.append(subject)
        elif recovered and serial in open_alerts:
            db.x("UPDATE panel_alert SET closed_day=? WHERE id=?", (days[0], open_alerts[serial]["id"]))
            subject = f"Zonnepaneel {display_name(row['name'])} werkt weer normaal"
            if send_mail:
                from . import push

                await push.broadcast("panels", f"✅ {subject}", "De melding is gesloten.", url="/#panelen",
                                     tag=f"panel-{serial}")
            if send_mail and config.panel_recovery_mail:
                await mailer.send(
                    config.split(config.alert_to),
                    subject,
                    f"Paneel {display_name(row['name'])} (optimizer {serial}) wekte op {days[0]} weer op "
                    "zoals het normaal doet. De melding is gesloten." + mailer.footer(),
                )
            messages.append(subject)
    return messages


# ---------------- planning ----------------


class PanelSource:
    """Interface: panels() -> {serial: {name, group, order}}, panel_energy(date) -> {serial: Wh}"""

    async def panels(self) -> dict[str, dict[str, Any]]: ...  # pragma: no cover

    async def panel_energy(self, day: date) -> dict[str, float]: ...  # pragma: no cover


async def fetch_and_store(source: Any, day: date, final: bool) -> None:
    panels = await source.panels()
    energy = await source.panel_energy(day)
    db = get_db()
    db.save_panel_day(day.isoformat(), panels, energy, final)
    if day == datetime.now(config.tz).date():
        db.set_kv("panel_fetched_at", datetime.now(config.tz).isoformat(timespec="seconds"))


# Opening the panel tab asks the loop for fresh data, so you don't wait for the next round.
_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None
MIN_ON_DEMAND = timedelta(minutes=3)


def request_refresh() -> None:
    """Thread-safe: may be called from a sync FastAPI endpoint."""
    if _loop is not None and _wake is not None:
        _loop.call_soon_threadsafe(_wake.set)


def _check_time() -> tuple[int, int]:
    h, m = config.panel_check_time.split(":")
    return int(h), int(m)


async def backfill(source: Any) -> int:
    """Haalt ontbrekende afgesloten dagen op. Geeft het aantal opgehaalde dagen terug."""
    db = get_db()
    today = datetime.now(config.tz).date()
    have_final = {r["day"] for r in db.q("SELECT DISTINCT day FROM panel_day WHERE final=1")}
    missing = [
        today - timedelta(days=i)
        for i in range(config.panel_backfill_days, 0, -1)
        if (today - timedelta(days=i)).isoformat() not in have_final
    ]
    for d in missing:
        await fetch_and_store(source, d, final=True)
        await asyncio.sleep(2)
    if missing:
        log.info("Paneelgeschiedenis opgehaald voor %d dagen", len(missing))
        await run_check((today - timedelta(days=1)).isoformat())
    return len(missing)


async def run(source: Any) -> None:
    global _wake, _loop
    _wake, _loop = asyncio.Event(), asyncio.get_running_loop()
    db = get_db()
    tz = config.tz
    every = timedelta(minutes=config.panel_refresh_minutes)
    try:
        revalidate_open_alerts()
    except Exception:  # noqa: BLE001
        log.exception("Herbeoordelen van meldingen mislukt")
    backfilled_on: str | None = None
    last_refresh: datetime | None = None
    last_attempt: datetime | None = None
    while True:
        now = datetime.now(tz)
        today_s = now.date().isoformat()
        try:
            if backfilled_on != today_s and (last_attempt is None or now - last_attempt > timedelta(minutes=55)):
                last_attempt = now
                await backfill(source)
                backfilled_on = today_s
            ch, cm = _check_time()
            check_due = (now.hour, now.minute) >= (ch, cm) and db.get_kv("panel_check_done") != today_s
            if check_due:
                await fetch_and_store(source, now.date(), final=True)
                msgs = await run_check(today_s)
                db.set_kv("panel_check_done", today_s)
                log.info("Paneelcontrole %s klaar: %s", today_s, msgs or "alles in orde")
                last_refresh = now
            else:
                asked = _wake.is_set()
                _wake.clear()
                since = None if last_refresh is None else now - last_refresh
                daylight = 6 <= now.hour <= 22
                if since is None or (daylight and since >= every) or (asked and since >= MIN_ON_DEMAND):
                    last_refresh = now
                    await fetch_and_store(source, now.date(), final=False)
            db.set_kv("panel_error", "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            log.warning("Paneeldata ophalen mislukt: %s", msg)
            db.set_kv("panel_error", msg)
        try:
            await asyncio.wait_for(_wake.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
