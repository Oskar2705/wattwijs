"""SQLite-opslag: per minuut de energie (Wh) en gemiddelde vermogens (W)."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .config import config

ENERGY_COLS = ["import_wh", "export_wh", "solar_wh", "charge_wh", "discharge_wh", "heatpump_wh", "house_wh", "aircon_wh"]
POWER_COLS = ["grid_w", "solar_w", "battery_w", "house_w", "heatpump_w", "soc", "aircon_w"]
# Verwarming: geleverde warmte (Wh, opgeteld) en temperaturen (gemiddeld per minuut).
# Apart gehouden zodat bestaande rijen, de geschiedenis en de CSV-export niet veranderen.
HEAT_ENERGY_COLS = ["heat_wh"]
CLIMATE_COLS = ["supply_temp", "outdoor_temp", "room_temp"]
EXTRA_COLS = [*HEAT_ENERGY_COLS, *CLIMATE_COLS]
MINUTE_COLS = ["ts", "day", "hour", *ENERGY_COLS, *POWER_COLS, *EXTRA_COLS]
BASE_TEMP_C = 18.0  # graaddagen: per dag max(0, 18 - gemiddelde buitentemperatuur)

def consumption_wh(t: dict[str, Any]) -> float:
    """Verbruik van het huis over een periode, uit de energiebalans.

    Per minuut wordt het huisverbruik alleen vastgelegd als alle bronnen tegelijk bekend
    zijn. Valt er even één weg (bijvoorbeeld een batterij die nog niet bereikbaar is), dan
    ontbreken die minuten in house_wh, terwijl afname, teruglevering en opwek wel gemeten
    zijn. De balans afname - teruglevering + opwek + ontladen - laden is dan vollediger.
    We nemen de grootste van de twee: zonder gaten zijn ze (op batterijverliezen na) gelijk.
    """
    g = lambda k: float(t.get(k) or 0.0)  # noqa: E731
    balance = g("import_wh") - g("export_wh") + g("solar_wh") + g("discharge_wh") - g("charge_wh")
    return max(g("house_wh"), balance, 0.0)


def self_sufficiency(t: dict[str, Any], min_wh: float = 50.0) -> float | None:
    """Deel van het verbruik dat niet van het net kwam, in procent."""
    house = consumption_wh(t)
    if house <= min_wh:
        return None
    return max(0.0, min(1.0, 1 - float(t.get("import_wh") or 0.0) / house)) * 100


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS minute (
    ts INTEGER PRIMARY KEY,         -- begin van de minuut, unix-tijd (UTC)
    day TEXT NOT NULL,              -- lokale datum JJJJ-MM-DD
    hour INTEGER NOT NULL,          -- lokaal uur 0-23
    {", ".join(c + " REAL" for c in ENERGY_COLS)},
    {", ".join(c + " REAL" for c in POWER_COLS)},
    {", ".join(c + " REAL" for c in EXTRA_COLS)}
);
CREATE INDEX IF NOT EXISTS minute_day ON minute(day);

CREATE TABLE IF NOT EXISTS panel_day (
    day TEXT NOT NULL,
    serial TEXT NOT NULL,
    name TEXT,
    grp TEXT,
    sort TEXT,
    wh REAL,
    final INTEGER DEFAULT 0,        -- 1 = dag afgesloten (na de avondcontrole)
    PRIMARY KEY (day, serial)
);

CREATE TABLE IF NOT EXISTS panel_alert (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT NOT NULL,
    name TEXT,
    opened_day TEXT NOT NULL,
    closed_day TEXT,
    deviation REAL,
    mail_status TEXT
);

CREATE TABLE IF NOT EXISTS price (
    ts INTEGER PRIMARY KEY,         -- begin van het kwartier, unix-tijd (UTC)
    day TEXT NOT NULL,              -- lokale datum
    market REAL,                    -- kale marktprijs, EUR/kWh
    all_in REAL                     -- afnameprijs incl. energiebelasting, opslag en btw, EUR/kWh
);
CREATE INDEX IF NOT EXISTS price_day ON price(day);

CREATE TABLE IF NOT EXISTS push_sub (
    endpoint TEXT PRIMARY KEY,
    sub TEXT NOT NULL,              -- JSON van de PushSubscription
    prefs TEXT NOT NULL,            -- JSON met per soort melding aan/uit
    label TEXT,
    created REAL,
    last_ok REAL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS gas_period (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start TEXT NOT NULL,            -- eerste dag (lokaal, JJJJ-MM-DD)
    end TEXT NOT NULL,              -- laatste dag, inclusief
    m3 REAL NOT NULL,
    created REAL
);
"""


class DB:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # Kolommen die later zijn bijgekomen (bv. aircon_wh) toevoegen aan een bestaande database.
            have = {r[1] for r in self.conn.execute("PRAGMA table_info(minute)")}
            for c in [*ENERGY_COLS, *POWER_COLS, *EXTRA_COLS]:
                if c not in have:
                    self.conn.execute(f"ALTER TABLE minute ADD COLUMN {c} REAL")
            self.conn.commit()

    # ---- algemeen ----
    def q(self, sql: str, args: Any = ()) -> list[dict[str, Any]]:
        with self.lock:
            params = args if isinstance(args, dict) else tuple(args)
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def x(self, sql: str, args: Iterable[Any] = ()) -> None:
        with self.lock:
            self.conn.execute(sql, tuple(args))
            self.conn.commit()

    def purge_demo_leftovers(self) -> None:
        """Oudere versies schreven demodata in dezelfde database. Zit die erin, dan alles wissen."""
        demo_panels = self.q("SELECT COUNT(*) AS n FROM panel_day WHERE serial LIKE 'DEMO%'")[0]["n"]
        if demo_panels or self.get_kv("price_source") == "demo":
            with self.lock:
                for t in ("minute", "panel_day", "panel_alert", "price", "kv"):
                    self.conn.execute(f"DELETE FROM {t}")
                self.conn.commit()
            import logging

            logging.getLogger(__name__).warning("Demogegevens gevonden in de database: alles gewist, er wordt opnieuw begonnen")

    def get_kv(self, k: str) -> str | None:
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return r[0]["v"] if r else None

    def set_kv(self, k: str, v: str) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ---- minuten ----
    def write_minute(self, ts: int, energy: dict[str, float], power: dict[str, float | None]) -> None:
        local = datetime.fromtimestamp(ts, config.tz)
        cols = MINUTE_COLS
        vals = [ts, local.date().isoformat(), local.hour]
        vals += [round(energy.get(c, 0.0), 3) for c in [*ENERGY_COLS, *HEAT_ENERGY_COLS][: len(ENERGY_COLS)]]
        vals += [None if power.get(c) is None else round(power[c], 1) for c in POWER_COLS]
        vals += [round(energy.get(c, 0.0), 3) for c in HEAT_ENERGY_COLS]
        vals += [None if power.get(c) is None else round(power[c], 1) for c in CLIMATE_COLS]
        sql = (
            f"INSERT INTO minute({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
            f"ON CONFLICT(ts) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols[1:])
        )
        self.x(sql, vals)

    def write_minutes(self, rows: list[tuple]) -> None:
        """Rijen in de volgorde van MINUTE_COLS; kortere rijen (zonder verwarmingskolommen) worden aangevuld."""
        cols = MINUTE_COLS
        rows = [tuple(r) + (None,) * (len(cols) - len(r)) for r in rows]
        with self.lock:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO minute({','.join(cols)}) VALUES({','.join('?' * len(cols))})", rows
            )
            self.conn.commit()

    def _sums(self, alias: str = "") -> str:
        a = f"{alias}." if alias else ""
        return ", ".join(f"ROUND(SUM({a}{c}), 1) AS {c}" for c in ENERGY_COLS)

    # Kosten per minuut: minuutenergie x prijs van het kwartier waarin de minuut valt.
    # exp = terugleverprijs = marktprijs x vermenigvuldiger + vaste vergoeding.
    def _costs(self) -> str:
        exp = "(p.market * :mult + :fee)"
        return (
            "ROUND(SUM(m.import_wh * p.all_in) / 1000, 4) AS cost_import, "
            f"ROUND(SUM(m.export_wh * {exp}) / 1000, 4) AS revenue_export, "
            f"ROUND(SUM(m.discharge_wh * p.all_in - MIN(m.charge_wh, m.import_wh) * p.all_in "
            f"- MAX(m.charge_wh - m.import_wh, 0) * {exp}) / 1000, 4) AS battery_value, "
            "COUNT(p.ts) AS priced_minutes, COUNT(*) AS minutes"
        )

    def _agg(self, bucket: str, where: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        args = {"mult": config.export_multiplier, "fee": config.export_fee_eur, **args}
        return self.q(
            f"SELECT {bucket} AS bucket, {self._sums('m')}, {self._costs()} "
            f"FROM minute m LEFT JOIN price p ON p.ts = m.ts - (m.ts % 900) "
            f"WHERE {where} GROUP BY 1 ORDER BY 1",
            args,
        )

    def totals_for_day(self, day: str) -> dict[str, float]:
        r = self._agg("m.day", "m.day = :d", {"d": day})
        if not r:
            return {c: 0.0 for c in ENERGY_COLS}
        out = {k: (v or 0.0) for k, v in r[0].items() if k != "bucket"}
        return out

    def by_hour(self, day: str) -> list[dict[str, Any]]:
        return self._agg("m.hour", "m.day = :d", {"d": day})

    def by_day(self, month: str) -> list[dict[str, Any]]:
        return self._agg("m.day", "m.day LIKE :d", {"d": month + "-%"})

    def by_month(self, year: str) -> list[dict[str, Any]]:
        return self._agg("substr(m.day,1,7)", "m.day LIKE :d", {"d": year + "-%"})

    def curve(self, day: str, step: int = 300) -> list[dict[str, Any]]:
        avgs = ", ".join(f"ROUND(AVG({c}),1) AS {c}" for c in POWER_COLS)
        return self.q(
            f"SELECT (ts/{step})*{step} AS ts, {avgs} FROM minute WHERE day=? GROUP BY ts/{step} ORDER BY 1", (day,)
        )

    def first_day(self) -> str | None:
        r = self.q("SELECT MIN(day) AS d FROM minute")
        return r[0]["d"] if r else None

    def hourly_export(self, start: str, end: str) -> list[dict[str, Any]]:
        args = {"mult": config.export_multiplier, "fee": config.export_fee_eur, "s": start, "e": end}
        return self.q(
            f"SELECT m.day AS day, m.hour AS hour, {self._sums('m')}, {self._costs()} "
            "FROM minute m LEFT JOIN price p ON p.ts = m.ts - (m.ts % 900) "
            "WHERE m.day BETWEEN :s AND :e GROUP BY m.day, m.hour ORDER BY m.day, m.hour",
            args,
        )

    # ---- prijzen ----
    def save_prices(self, rows: list[tuple[int, str, float, float]]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO price(ts,day,market,all_in) VALUES(?,?,?,?) "
                "ON CONFLICT(ts) DO UPDATE SET day=excluded.day, market=excluded.market, all_in=excluded.all_in",
                rows,
            )
            self.conn.commit()

    def prices_between(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        return self.q(
            "SELECT ts, day, market, all_in, ROUND(market * ? + ?, 5) AS export FROM price "
            "WHERE ts >= ? AND ts < ? ORDER BY ts",
            (config.export_multiplier, config.export_fee_eur, start_ts, end_ts),
        )

    def price_day_count(self, day: str) -> int:
        return self.q("SELECT COUNT(*) AS n FROM price WHERE day=?", (day,))[0]["n"]

    # ---- panelen ----
    def save_panel_day(self, day: str, panels: dict[str, dict[str, Any]], energy: dict[str, float], final: bool) -> None:
        rows = []
        for serial, info in panels.items():
            rows.append((day, serial, info["name"], info["group"], str(info["order"]), energy.get(serial, 0.0), int(final)))
        with self.lock:
            self.conn.executemany(
                "INSERT INTO panel_day(day,serial,name,grp,sort,wh,final) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(day,serial) DO UPDATE SET name=excluded.name, grp=excluded.grp, sort=excluded.sort, "
                "wh=excluded.wh, final=MAX(final, excluded.final)",
                rows,
            )
            self.conn.commit()

    def panel_day(self, day: str) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM panel_day WHERE day=? ORDER BY grp, sort, name", (day,))

    def panel_days(self) -> list[str]:
        return [r["day"] for r in self.q("SELECT DISTINCT day FROM panel_day ORDER BY day")]

    def final_panel_days_before(self, day: str, limit: int) -> list[str]:
        return [
            r["day"]
            for r in self.q(
                "SELECT DISTINCT day FROM panel_day WHERE final=1 AND day<=? ORDER BY day DESC LIMIT ?", (day, limit)
            )
        ]

    def open_alerts(self) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM panel_alert WHERE closed_day IS NULL ORDER BY opened_day")

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM panel_alert ORDER BY id DESC LIMIT ?", (limit,))

    # ---- gas (handmatig ingevuld) en verwarming per dag ----
    def add_gas(self, start: str, end: str, m3: float) -> int:
        import time as _time

        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO gas_period(start, end, m3, created) VALUES(?,?,?,?)", (start, end, m3, _time.time())
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def gas_periods(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.q("SELECT id, start, end, m3 FROM gas_period ORDER BY start DESC, id DESC LIMIT ?", (limit,))

    def delete_gas(self, gid: int) -> None:
        self.x("DELETE FROM gas_period WHERE id=?", (gid,))

    def heating_days(self, start: str, end: str) -> list[dict[str, Any]]:
        """Per dag: stroom en warmte van de warmtepomp, gewogen aanvoertemperatuur, buiten- en
        kamertemperatuur, en de stroomkosten van de warmtepomp tegen de kwartierprijzen."""
        return self.q(
            "SELECT m.day AS day, SUM(m.heatpump_wh) AS hp_wh, SUM(m.heat_wh) AS heat_wh, "
            "SUM(CASE WHEN m.heat_wh > 0 THEN m.supply_temp * m.heat_wh END) "
            "  / NULLIF(SUM(CASE WHEN m.heat_wh > 0 AND m.supply_temp IS NOT NULL THEN m.heat_wh END), 0) AS supply_temp, "
            "AVG(m.outdoor_temp) AS outdoor_temp, COUNT(m.outdoor_temp) AS outdoor_minutes, "
            "AVG(m.room_temp) AS room_temp, "
            "SUM(m.heatpump_wh * p.all_in) / 1000 AS hp_cost, COUNT(p.ts) AS priced_minutes, COUNT(*) AS minutes "
            "FROM minute m LEFT JOIN price p ON p.ts = m.ts - (m.ts % 900) "
            "WHERE m.day BETWEEN ? AND ? GROUP BY m.day ORDER BY m.day",
            (start, end),
        )


def month_days(month: str) -> list[str]:
    y, m = map(int, month.split("-"))
    d = date(y, m, 1)
    out = []
    while d.month == m:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


db: DB | None = None


def get_db() -> DB:
    """Demo en echt gebruiken elk een eigen databasebestand, zodat ze nooit mengen."""
    global db
    if db is None:
        name = "energie-demo.sqlite" if config.demo else "energie.sqlite"
        db = DB(os.path.join(config.data_dir, name))
        if not config.demo:
            db.purge_demo_leftovers()
    return db
