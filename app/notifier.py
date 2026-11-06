"""Planner en teksten voor de pushmeldingen (ochtendtip, avondrecords, weekoverzicht)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from . import exportwatch, push, solarforecast, weather
from .config import config
from .db import consumption_wh, get_db, self_sufficiency
from .prices import QUARTER, cheapest_block, price_info, windows
from .state import state

log = logging.getLogger(__name__)

DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


# ---------------- opmaak ----------------


def cent(v: float) -> str:
    c = v * 100
    s = f"{c:.1f}" if abs(c) < 10 else f"{c:.0f}"
    return s.replace(".", ",") + " cent"


def kwh(wh: float) -> str:
    return f"{wh / 1000:.1f}".replace(".", ",") + " kWh"


def eur(v: float) -> str:
    s = f"{abs(v):.2f}".replace(".", ",")
    return f"-€ {s}" if v < 0 else f"€ {s}"


def hm(ts: int) -> str:
    return datetime.fromtimestamp(ts, config.tz).strftime("%H:%M")


def _hhmm(text: str) -> tuple[int, int]:
    h, m = text.strip().split(":")
    return int(h), int(m)


def _day_bounds(d: date) -> tuple[int, int]:
    start = int(datetime.combine(d, dtime.min, tzinfo=config.tz).timestamp())
    end = int(datetime.combine(d + timedelta(days=1), dtime.min, tzinfo=config.tz).timestamp())
    return start, end


PRICE_COVERAGE_MIN = 0.95  # bedragen alleen tonen als (vrijwel) elke minuut een prijs heeft


def _totals(day: str) -> dict[str, Any]:
    db = get_db()
    t = db.totals_for_day(day)
    t["house_wh"] = consumption_wh(t)
    t["self_sufficiency"] = self_sufficiency(t, min_wh=100)
    t.update(db.solar_split(day))
    hd = db.heating_days(day, day)
    t["boiler_h"] = float(hd[0]["boiler_h"] or 0.0) if hd and hd[0].get("boiler_minutes") else None
    return t


# ---------------- ochtend ----------------


def best_block_today(now: datetime) -> dict[str, Any] | None:
    """Goedkoopste aaneengesloten blok binnen het venster (bv. 07:00-23:00), vanaf nu."""
    start_s, end_s = config.push_block_window.split("-")
    d = now.date()
    w_start = datetime.combine(d, dtime(*_hhmm(start_s)), tzinfo=config.tz)
    w_end = datetime.combine(d, dtime(*_hhmm(end_s)), tzinfo=config.tz)
    frm = max(int(w_start.timestamp()), int(now.timestamp()) - int(now.timestamp()) % QUARTER)
    rows = get_db().prices_between(frm, int(w_end.timestamp()))
    q = max(1, int(round(config.push_block_hours * 4)))
    return cheapest_block(rows, q)


def net_metering_over(d: date) -> bool:
    """Is salderen voorbij (CONTRACT_START)? Dan is zelf verbruiken meer waard dan terugleveren."""
    try:
        return bool(config.contract_start) and d >= date.fromisoformat(config.contract_start)
    except ValueError:
        return False


def kw(w: float) -> str:
    return f"{w / 1000:.1f}".replace(".", ",") + " kW"


def _num(v: float) -> str:
    return f"{v:.1f}".replace(".", ",")


def sun_hours_text(h: float) -> str:
    if h < 0.75:
        return "nauwelijks zon"
    if h < 1.5:
        return "zo'n 1 uur zon"
    return f"zo'n {round(h):.0f} uur zon"


def _overlap(x: dict[str, Any], y: dict[str, Any]) -> bool:
    return x["start"] < y["end"] and y["start"] < x["end"]


def build_morning(now: datetime, outlook: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """Ochtendbericht: zonuren en wanneer, het blok met stroom over, en vanaf CONTRACT_START de prijzen.

    `outlook` komt van solarforecast.day_outlook:
    {"sun_hours", "sun_window", "solar_kwh", "block"} of None.
    Tijdens salderen geen prijstips: die leveren dan niets op."""
    db = get_db()
    today = now.date()
    dynamic = net_metering_over(today)
    lines: list[str] = []
    prio: list[int] = []  # belang per regel, lager = belangrijker (zie fit)
    title = "Goedemorgen"
    o = outlook or {}
    sun_h, solar_kwh, sun, win = o.get("sun_hours"), o.get("solar_kwh"), o.get("block"), o.get("sun_window")

    # zon
    if sun_h is not None or solar_kwh is not None:
        grey = sun_h is not None and sun_h < 0.75
        head = sun_hours_text(sun_h) if sun_h is not None else "zonnestroom"
        title = ("☁️ " if grey else "☀️ ") + "Vandaag " + head
        if solar_kwh is not None:
            title += f" (ongeveer {_num(solar_kwh)} kWh)"
        if win and not grey:
            lines.append(f"De zon schijnt vooral tussen {hm(win['start'])} en {hm(win['end'])}."); prio.append(4)
        if sun:
            extra = " of de warmtepomp een graadje hoger" if exportwatch.heating_season(today, weather.today_temp()) else ""
            what = "de was, de vaatwasser of de warmtepomp een graadje hoger" if extra else "de was of de vaatwasser"
            lines.append(f"Tussen {hm(sun['start'])} en {hm(sun['end'])} blijft er stroom over (tot {kw(sun['surplus_w'])}). "
                         f"Goed moment voor {what}."); prio.append(0)
            if dynamic and state.battery.configured:
                lines.append("De batterij kan dan ook bijladen met de zon."); prio.append(7)
        elif solar_kwh is not None:
            lines.append("Er blijft vandaag weinig zonnestroom over, dus de was kan wanneer het jou uitkomt."); prio.append(0)

    # prijs: pas vanaf het dynamische contract
    if dynamic:
        s, e = _day_bounds(today)
        rows = db.prices_between(s, e)
        block = best_block_today(now) if rows else None
        if block:
            if title == "Goedemorgen":
                title = f"Vandaag het goedkoopst: {hm(block['start'])}–{hm(block['end'])}"
            if sun and _overlap(sun, block):
                lines.append(f"Dat is ook ongeveer de goedkoopste tijd van de dag "
                             f"({hm(block['start'])}–{hm(block['end'])}, {cent(block['avg'])} per kWh)."); prio.append(1)
            else:
                lines.append(f"Stroom van het net is het goedkoopst tussen {hm(block['start'])} en {hm(block['end'])} "
                             f"({cent(block['avg'])} per kWh)."); prio.append(1)
            rest = [r for r in rows if r["ts"] >= int(now.timestamp()) - QUARTER]
            hi = max(rest, key=lambda r: r["all_in"]) if rest else None
            if hi:
                lines.append(f"Het duurst is het rond {hm(hi['ts'])} ({cent(hi['all_in'])})."); prio.append(6)
        if rows:
            neg = windows([r for r in rows if r["ts"] >= int(now.timestamp()) - QUARTER], lambda r: r["export"] < 0)
            for w in neg:
                lines.append(f"Tussen {hm(w['start'])} en {hm(w['end'])} is de prijs negatief: stroom gebruiken levert dan zelfs iets op."); prio.append(3)

    for a in db.open_alerts():
        name = a["name"] or a["serial"]
        lines.append(f"⚠ Zonnepaneel {name} doet het nog steeds niet goed."); prio.append(2)

    if not lines and title == "Goedemorgen":
        return None
    return title, fit(lines, prio=prio)


# ---------------- avond ----------------


def _record(day: str, t: dict[str, Any]) -> str | None:
    """Dagrecord, (bijna) zelfvoorzienende dag of mijlpaal, als één zin."""
    db = get_db()
    hist = db.q(
        "SELECT day, SUM(solar_wh) AS s FROM minute WHERE day < ? GROUP BY day HAVING COUNT(*) > 600", (day,)
    )
    solar = t.get("solar_wh") or 0.0
    if len(hist) >= 14 and solar > 500 and solar > max(h["s"] or 0 for h in hist):
        return f"☀ Nieuw record: de beste zonnedag sinds de meting begon."
    total = db.q("SELECT SUM(solar_wh) AS s FROM minute")[0]["s"] or 0.0
    before = total - solar
    step = 500_000  # elke 500 kWh
    if total >= step and int(before // step) < int(total // step):
        return f"🎉 Mijlpaal: de panelen hebben samen al {int(total // step) * step / 1000:.0f} kWh opgewekt."
    return None


FRACTIONS = [(10, "een tiende"), (20, "een vijfde"), (25, "een kwart"), (33, "een derde"), (40, "twee vijfde"),
             (50, "de helft"), (60, "drie vijfde"), (67, "twee derde"), (75, "driekwart"), (80, "vier vijfde"),
             (90, "negen tiende")]


def share_text(pct: float) -> str:
    """34 -> 'een derde'; valt het tussen twee breuken in, dan het percentage."""
    for p, word in FRACTIONS:
        if abs(pct - p) <= 2.5:
            return word
    return f"{pct:.0f}%"


# iOS toont van een webmelding maar 256 tekens tekst; daarna wordt hij afgekapt, midden in een woord.
BODY_MAX = 240


def fit(parts: list[str], limit: int = BODY_MAX, sep: str = "\n", prio: list[int] | None = None) -> str:
    """Zet de delen onder elkaar zolang het past, in de volgorde van de lijst.

    `prio` geeft per deel het belang (lager = belangrijker; standaard de volgorde zelf). Wat niet meer
    past valt weg, maar een kleiner, minder belangrijk deel kan daarna nog wel mee."""
    order = sorted(range(len(parts)), key=lambda i: (prio[i] if prio else i, i))
    keep: set[int] = set()
    for i in order:
        if not parts[i]:
            continue
        cand = sep.join(parts[j] for j in sorted(keep | {i}))
        if len(cand) <= limit:
            keep.add(i)
    return sep.join(parts[j] for j in sorted(keep))


def build_evening(day: date) -> tuple[str, str] | None:
    """Avondoverzicht, kort genoeg voor een melding op de iPhone: opwek tegenover de verwachting,
    verbruik, waar de zonnestroom heen ging, vanaf CONTRACT_START de kosten, en een record of mijlpaal."""
    d = day.isoformat()
    t = _totals(d)
    if (t.get("minutes") or 0) < 600:
        return None
    g = lambda k: float(t.get(k) or 0.0)  # noqa: E731
    solar, house, exp, imp = g("solar_wh"), g("house_wh"), g("export_wh"), g("import_wh")
    discharge, hp, ac = g("discharge_wh"), g("heatpump_wh"), g("aircon_wh")
    direct, to_batt, grid_batt = g("solar_direct_wh"), g("solar_to_batt_wh"), g("grid_to_batt_wh")
    ss = t["self_sufficiency"]

    # opwek
    if solar < 50:
        sun_line = "Vrijwel geen zonnestroom vandaag."
    else:
        sun_line = f"{kwh(solar)} opgewekt"
        expected = get_db().get_kv(f"solar_expected_{d}")
        if expected:
            diff = solar / 1000 - float(expected)
            sun_line += (" (zoals verwacht)" if abs(diff) < 0.3 else f" (verwacht: {_num(float(expected))} kWh)")
        sun_line += "."

    # verbruik
    use = f"Verbruik {kwh(house)}"
    parts = []
    if hp > 50:
        parts.append(f"warmtepomp {kwh(hp)}")
    if ac > 50:
        parts.append(f"airco's ≈ {kwh(ac)}")
    if parts:
        use += ", waarvan " + " en ".join(parts)
    use += "."

    # waar ging de zonnestroom heen, en wat kwam van het net
    flow = ""
    if solar >= 50:
        bits = [f"{kwh(direct)} meteen gebruikt"]
        if to_batt > 50:
            bits.append(f"{kwh(to_batt)} in de batterij")
        if exp > 50:
            bits.append(f"{kwh(exp)} terug")
        flow = "Zon: " + ", ".join(bits) + "."
    batt = f"Batterij gaf {kwh(discharge)} terug aan het huis." if discharge > 50 else ""
    grid = f"Van het net: {kwh(max(imp - grid_batt, 0.0))}"
    if grid_batt > 50:
        grid += f" (+{kwh(grid_batt)} voor de batterij)"
    grid += "."

    # geld: pas vanaf het dynamische contract
    money = ""
    priced, minutes = t.get("priced_minutes") or 0, t.get("minutes") or 0
    if net_metering_over(day) and minutes and priced >= PRICE_COVERAGE_MIN * minutes:
        cost, rev = g("cost_import"), g("revenue_export")
        net = cost - rev
        money = f"Stroom vandaag: {eur(net)}" if net >= 0 else f"Verdiend aan stroom: {eur(-net)}"
        money += f" (terugleveren {eur(rev)})." if rev >= 0.01 else "."

    boiler = ""
    bh = t.get("boiler_h")
    if bh is not None and bh >= 0.1:
        mins = round(bh * 60)
        boiler = f"Cv-ketel: {mins} minuten bijgesprongen." if mins < 60 else f"Cv-ketel: {_num(bh)} uur bijgesprongen."

    rec = _record(d, t) or ""

    if ss is not None and ss >= 95:
        title = "🏡 Vandaag bijna helemaal op eigen stroom"
    elif ss is not None and ss >= 5:
        title = f"Vandaag {share_text(ss)} eigen stroom" + (" ☀️" if ss >= 30 else "")
    else:
        title = f"Vandaag {kwh(house)} verbruikt"
    # getoond in deze volgorde; wat niet past op een iPhone, valt weg naar belang (prio)
    lines = [sun_line, use, flow, batt, grid, boiler, money, rec]
    return title, fit(lines, prio=[0, 1, 2, 6, 3, 5, 4, 7])


# ---------------- week ----------------


def build_weekly(day: date) -> tuple[str, str] | None:
    db = get_db()
    start = day - timedelta(days=6)

    def week(s: date, e: date) -> dict[str, Any]:
        r = db.q(
            "SELECT SUM(solar_wh) solar_wh, SUM(house_wh) house_wh, SUM(import_wh) import_wh, "
            "SUM(export_wh) export_wh, SUM(charge_wh) charge_wh, SUM(discharge_wh) discharge_wh, COUNT(*) n "
            "FROM minute WHERE day BETWEEN ? AND ?",
            (s.isoformat(), e.isoformat()),
        )[0]
        r = {k: (v or 0.0) for k, v in r.items()}
        return {"s": r["solar_wh"], "h": consumption_wh(r), "i": r["import_wh"], "n": r["n"]}

    w = week(start, day)
    if w["n"] < 5 * 1440:  # minstens 5 dagen gemeten
        return None
    prev = week(start - timedelta(days=7), start - timedelta(days=1))
    lines = [f"Opgewekt {kwh(w['s'])}, verbruikt {kwh(w['h'])}."]
    if w["h"] > 0:
        lines.append(f"{max(0.0, 1 - w['i'] / w['h']) * 100:.0f}% kwam van eigen zon of batterij.")
    if prev["n"] >= 6 * 1440 and prev["s"] > 0:  # vorige week bijna compleet
        diff = (w["s"] - prev["s"]) / prev["s"] * 100
        if abs(diff) < 3:
            lines.append("Ongeveer evenveel zon als vorige week.")
        else:
            lines.append(f"Dat is {abs(diff):.0f}% {'meer' if diff > 0 else 'minder'} zon dan vorige week.")
    days = db.q(
        "SELECT day, SUM(solar_wh) s FROM minute WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY s DESC LIMIT 1",
        (start.isoformat(), day.isoformat()),
    )
    if days and days[0]["s"]:
        best = date.fromisoformat(days[0]["day"])
        lines.append(f"Zonnigste dag: {DAGEN[best.weekday()]} ({kwh(days[0]['s'])}).")
    return "📊 Jouw energieweek", " ".join(lines)


# ---------------- planner ----------------


ONCE_TRIES = 5


async def _once(key: str, fn) -> None:
    """Voer fn één keer per sleutel uit (bv. per dag), ook na een herstart.

    Pas als fn gelukt is, telt hij als gedaan. Mislukt hij, dan volgt de minuut erna een nieuwe poging,
    hooguit ONCE_TRIES keer, zodat een blijvende fout niet elke minuut opnieuw wordt geprobeerd."""
    db = get_db()
    if db.get_kv(key):
        return
    tries = int(db.get_kv(f"{key}_tries") or 0) + 1
    db.set_kv(f"{key}_tries", str(tries))
    try:
        await fn()
    except Exception:
        if tries >= ONCE_TRIES:
            db.set_kv(key, "failed")
            log.error("%s: na %d pogingen opgegeven", key, tries)
        raise
    db.set_kv(key, "1")


def export_watcher() -> exportwatch.ExportWatcher:
    return exportwatch.ExportWatcher(
        min_w=config.push_export_w, minutes=config.push_export_minutes, cooldown_h=config.push_export_cooldown_h
    )


async def check_export(watcher: exportwatch.ExportWatcher, now: datetime) -> None:
    """Elke minuut: wordt er al een tijd teruggeleverd, dan een bericht (vanaf CONTRACT_START)."""
    if not exportwatch.active(now.date()):
        return
    snap = state.snapshot()
    grid, batt_cfg, soc, outdoor = exportwatch.snapshot_inputs(snap)
    if outdoor is None:  # niet elke warmtepomp geeft de buitentemperatuur door
        outdoor = weather.today_temp()
    mins = watcher.update(now.timestamp(), grid, batt_cfg, soc)
    if mins is None or grid is None:
        return
    pi = price_info() or {}
    export_price = (pi.get("now") or {}).get("export")
    tip = exportwatch.heating_season(now.date(), outdoor) and (now.hour, now.minute) < (
        _hhmm(config.push_heat_back_time)[0] - 1, _hhmm(config.push_heat_back_time)[1])
    title, body = exportwatch.build_message(grid, mins, export_price, batt_cfg, tip, config.push_heat_back_time)
    n = await push.broadcast("export", title, body, url="/#nu", tag="export")
    log.info("Terugleverbericht verstuurd naar %d telefoon(s)", n)
    if tip:
        get_db().set_kv("heat_tip_day", now.date().isoformat())


async def run() -> None:
    watcher = export_watcher()
    while True:
        try:
            now = datetime.now(config.tz)
            today = now.date()
            hhmm = (now.hour, now.minute)

            await check_export(watcher, now)
            if hhmm >= _hhmm(config.push_heat_back_time) and get_db().get_kv("heat_tip_day") == today.isoformat():
                async def heat_back() -> None:
                    await push.broadcast("export", *exportwatch.heat_back_message(), url="/#nu", tag="heat-back")
                await _once(f"push_heat_back_{today}", heat_back)

            if hhmm >= _hhmm(config.push_morning_time) and now.hour < 12:
                async def morning() -> None:
                    msg = build_morning(now, await solarforecast.day_outlook(now))
                    if msg:
                        await push.broadcast("morning", *msg, url="/#nu", tag=f"morning-{today}")
                await _once(f"push_morning_{today}", morning)

            if hhmm >= _hhmm(config.push_evening_time):
                async def evening() -> None:
                    msg = build_evening(today)
                    if msg:
                        await push.broadcast("records", *msg, url="/#geschiedenis", tag=f"evening-{today}")
                await _once(f"push_evening_{today}", evening)

            if today.weekday() == 6 and hhmm >= _hhmm(config.push_weekly_time):
                async def weekly() -> None:
                    msg = build_weekly(today)
                    if msg:
                        await push.broadcast("weekly", *msg, url="/#geschiedenis", tag=f"weekly-{today}")
                await _once(f"push_weekly_{today}", weekly)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Pushplanner")
        await asyncio.sleep(60)
