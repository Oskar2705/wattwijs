"""tado° X (Thread/Matter-lijn) via de cloud-API van tado: kamers uitlezen en de temperatuur instellen.

Inloggen gaat via de "device flow" van tado: de app vraagt een code aan, jij opent de link en
logt in bij tado, en de app krijgt een token. Dat token ververst zichzelf en staat in
DATA_DIR/tado_token.json (niet in git, niet in .env).

Let op de limiet van tado: zonder Auto-Assist-abonnement 100 aanvragen per dag, met
Auto-Assist 20.000. Standaard leest de app daarom elke 20 minuten (TADO_POLL_MINUTES) en
past hij na een opdracht de kaart zelf aan in plaats van alles opnieuw op te halen.

Endpoints zoals de app van tado zelf ze gebruikt (zie ook de bibliotheek PyTado):
  POST login.tado.com/oauth2/device_authorize, /oauth2/token
  GET  my.tado.com/api/v2/me, /homes/{id}
  GET  hops.tado.com/homes/{id}/rooms
  POST hops.tado.com/homes/{id}/rooms/{room}/manualControl, /resumeSchedule
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

from ..config import config
from ..state import Source
from . import register

log = logging.getLogger(__name__)

CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"  # publieke client-ID van de tado-apps
LOGIN = "https://login.tado.com/oauth2/"
MY_API = "https://my.tado.com/api/v2/"
HOPS = "https://hops.tado.com/"
HEADERS = {"Referer": "https://app.tado.com/"}
TERMINATIONS = {"next_block": "NEXT_TIME_BLOCK", "manual": "MANUAL"}
MIN_C, MAX_C = 5.0, 25.0


class TadoError(Exception):
    pass


class TadoAuthError(TadoError):
    """Opnieuw inloggen nodig."""


class TadoRateLimited(TadoError):
    def __init__(self, msg: str, retry_after: float | None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


# ---------------- token-opslag ----------------


class TokenStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(config.data_dir, "tado_token.json")

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass


# ---------------- vertalen ----------------


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _ts(v: Any) -> float | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_room(raw: dict[str, Any]) -> dict[str, Any]:
    """Eén kamer uit GET hops.tado.com/homes/{id}/rooms naar wat de app toont."""
    setting = raw.get("setting") or {}
    power_on = str(setting.get("power", "")).upper() == "ON"
    manual = raw.get("manualControlTermination")
    boost = raw.get("boostMode")
    nxt = raw.get("nextScheduleChange") or {}
    if boost:
        mode = "boost"
    elif manual:
        mode = "manual"
    else:
        mode = "schedule"
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or f"Kamer {raw.get('id')}",
        "room_temp": _get(raw, "sensorDataPoints", "insideTemperature", "value"),
        "humidity": _get(raw, "sensorDataPoints", "humidity", "percentage"),
        "on": power_on,
        "target": _get(setting, "temperature", "value") if power_on else None,
        "heating_pct": _get(raw, "heatingPower", "percentage"),
        "mode": mode,
        "manual_until": _ts(_get(manual, "projectedExpiry")) if manual else None,
        "manual_type": _get(manual, "type") if manual else None,
        "next_change_at": _ts(nxt.get("start")),
        "next_change_target": _get(nxt, "setting", "temperature", "value")
        if str(_get(nxt, "setting", "power") or "").upper() == "ON" else None,
        "window_open": bool(_get(raw, "openWindow", "activated")),
        "online": _get(raw, "connection", "state") in (None, "CONNECTED"),
    }


def manual_payload(target: float | None, termination: str) -> dict[str, Any]:
    term = {"type": TERMINATIONS.get(termination, termination)}
    if target is None:
        return {"setting": {"power": "OFF"}, "termination": term}
    t = round(float(target) * 2) / 2
    if not MIN_C <= t <= MAX_C:
        raise ValueError(f"temperatuur moet tussen {MIN_C:g} en {MAX_C:g} °C liggen")
    return {
        "setting": {"power": "ON", "isBoost": False, "temperature": {"value": t, "valueRaw": t, "precision": 0.1}},
        "termination": term,
    }


def parse_ratelimit(header: str | None) -> tuple[int | None, float | None]:
    """'"perday";r=87' of '"perday";r=0;t=3600' -> (resterend, seconden tot aanvulling)."""
    if not header:
        return None, None
    r = re.search(r"\br=(\d+)", header)
    t = re.search(r"\bt=(\d+)", header)
    return (int(r.group(1)) if r else None), (float(t.group(1)) if t else None)


# ---------------- de client ----------------


class TadoClient:
    def __init__(self, session: aiohttp.ClientSession, store: TokenStore) -> None:
        self.session = session
        self.store = store
        self.data = store.load()
        self.access: str | None = None
        self.access_exp = 0.0
        self.remaining: int | None = None
        self.refill_in: float | None = None
        self.lock = asyncio.Lock()

    @property
    def linked(self) -> bool:
        return bool(self.data.get("refresh_token"))

    def reload(self) -> None:
        self.data = self.store.load()
        self.access = None

    def _keep(self, tok: dict[str, Any]) -> None:
        self.access = tok["access_token"]
        self.access_exp = time.time() + float(tok.get("expires_in", 600)) - 30
        if tok.get("refresh_token"):
            self.data["refresh_token"] = tok["refresh_token"]  # tado roteert de refresh-token
        self.store.save(self.data)

    # --- inloggen ---
    async def start_login(self) -> dict[str, Any]:
        async with self.session.post(LOGIN + "device_authorize", data={"client_id": CLIENT_ID, "scope": "offline_access"},
                                     headers=HEADERS) as r:
            if r.status != 200:
                raise TadoError(f"tado gaf HTTP {r.status} bij het starten van de koppeling")
            d = await r.json(content_type=None)
        url = d.get("verification_uri_complete") or (
            f"{d['verification_uri']}?user_code={d['user_code']}&client_id={CLIENT_ID}")
        return {"url": url, "user_code": d["user_code"], "device_code": d["device_code"],
                "interval": float(d.get("interval", 5)), "expires_at": time.time() + float(d.get("expires_in", 300))}

    async def finish_login(self, flow: dict[str, Any]) -> None:
        """Wacht tot de gebruiker bij tado heeft ingelogd (of de code verloopt)."""
        params = {"client_id": CLIENT_ID, "device_code": flow["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
        while time.time() < flow["expires_at"]:
            await asyncio.sleep(max(flow["interval"], 2))
            async with self.session.post(LOGIN + "token", params=params, headers=HEADERS) as r:
                body = await r.json(content_type=None)
            if r.status == 200:
                self.data = {}
                self._keep(body)
                return
            err = body.get("error") if isinstance(body, dict) else None
            if err == "slow_down":
                flow["interval"] += 5
            elif err != "authorization_pending":
                raise TadoError(f"koppelen mislukt: {err or r.status}")
        raise TadoError("de koppelcode is verlopen; probeer het opnieuw")

    def logout(self) -> None:
        self.store.clear()
        self.data = {}
        self.access = None

    # --- verzoeken ---
    async def _token(self) -> str:
        if self.access and time.time() < self.access_exp:
            return self.access
        rt = self.data.get("refresh_token")
        if not rt:
            raise TadoAuthError("niet gekoppeld")
        async with self.session.post(LOGIN + "token", headers=HEADERS,
                                     data={"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt}) as r:
            body = await r.json(content_type=None)
            if r.status in (400, 401):
                # Een andere instantie kan de token net hebben vernieuwd: kijk eerst in het bestand.
                fresh = self.store.load().get("refresh_token")
                if fresh and fresh != rt:
                    self.data["refresh_token"] = fresh
                    return await self._token()
                raise TadoAuthError("de koppeling met tado is verlopen; koppel opnieuw")
            if r.status != 200:
                raise TadoError(f"token vernieuwen: HTTP {r.status}")
        self._keep(body)
        return self.access or ""

    async def request(self, method: str, url: str, payload: Any = None) -> Any:
        async with self.lock:
            for attempt in range(2):
                headers = {**HEADERS, "Authorization": f"Bearer {await self._token()}"}
                async with self.session.request(method, url, json=payload, headers=headers) as r:
                    rem, refill = parse_ratelimit(r.headers.get("ratelimit"))
                    if rem is not None:
                        self.remaining, self.refill_in = rem, refill
                    if r.status == 401 and attempt == 0:
                        self.access = None
                        continue
                    if r.status == 429:
                        retry = r.headers.get("Retry-After")
                        raise TadoRateLimited("daglimiet van tado bereikt",
                                              float(retry) if retry and retry.isdigit() else refill)
                    if r.status >= 400:
                        text = (await r.text())[:200]
                        raise TadoError(f"tado gaf HTTP {r.status}: {text}")
                    if r.status == 204:
                        return None
                    text = await r.text()
                    return json.loads(text) if text.strip() else None
        return None

    async def home_id(self) -> int:
        if self.data.get("home_id"):
            return int(self.data["home_id"])
        me = await self.request("GET", MY_API + "me")
        homes = (me or {}).get("homes") or []
        if not homes:
            raise TadoError("geen huis gevonden in dit tado-account")
        hid = int(homes[0]["id"])
        home = await self.request("GET", f"{MY_API}homes/{hid}")
        if (home or {}).get("generation") != "LINE_X":
            raise TadoError("dit is geen tado X-installatie; alleen de X-lijn wordt nu ondersteund")
        self.data["home_id"] = hid
        self.store.save(self.data)
        return hid

    async def rooms(self) -> list[dict[str, Any]]:
        hid = await self.home_id()
        raw = await self.request("GET", f"{HOPS}homes/{hid}/rooms")
        return [parse_room(r) for r in (raw or []) if isinstance(r, dict)]

    async def set_manual(self, room_id: int, target: float | None, termination: str) -> None:
        hid = await self.home_id()
        await self.request("POST", f"{HOPS}homes/{hid}/rooms/{int(room_id)}/manualControl",
                           manual_payload(target, termination))

    async def resume(self, room_id: int) -> None:
        hid = await self.home_id()
        await self.request("POST", f"{HOPS}homes/{hid}/rooms/{int(room_id)}/resumeSchedule")


# ---------------- de koppeling ----------------


def apply_change(room: dict[str, Any], changes: dict[str, Any], termination: str) -> dict[str, Any]:
    """Past de kaart alvast aan na een geslaagde opdracht (scheelt een aanvraag bij tado)."""
    r = dict(room)
    mode = changes.get("mode")
    if mode == "schedule":
        r.update(mode="schedule", manual_until=None, manual_type=None, pending=True)
    elif mode == "off":
        r.update(on=False, target=None, mode="manual", manual_type=TERMINATIONS.get(termination), pending=True)
    elif changes.get("target") is not None:
        r.update(on=True, target=round(float(changes["target"]) * 2) / 2, mode="manual",
                 manual_type=TERMINATIONS.get(termination), pending=True)
    return r


def publish(src: Source, rooms: list[dict[str, Any]], client: TadoClient | None, login: dict[str, Any] | None,
            error: str | None = None) -> None:
    status = {
        "linked": bool(client and client.linked),
        "login": {"url": login["url"], "user_code": login["user_code"]} if login else None,
        "remaining": client.remaining if client else None,
        "termination": config.tado_manual_until,
    }
    if rooms and not error:
        src.update(rooms=rooms, tado=status)
    else:
        src.values.update(rooms=rooms, tado=status)
        if error:
            src.error(error)


@register("heating", "tado", "tado° X (cloud)", env=("TADO_ENABLED", "TADO_POLL_MINUTES", "TADO_MANUAL_UNTIL"),
          description="Kamertemperaturen uitlezen en instellen. Koppelen gaat via het tabblad Klimaat.")
async def run(src: Source) -> None:
    store = TokenStore()
    wake = asyncio.Event()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        client = TadoClient(session, store)
        rooms: list[dict[str, Any]] = []
        login: dict[str, Any] | None = None
        login_task: asyncio.Task | None = None

        async def do_login() -> dict[str, Any]:
            nonlocal login, login_task
            flow = await client.start_login()
            login = flow

            async def wait() -> None:
                nonlocal login
                try:
                    await client.finish_login(flow)
                    log.info("tado gekoppeld")
                    src.last_error = None
                except Exception as exc:  # noqa: BLE001
                    src.error(str(exc))
                finally:
                    login = None
                    publish(src, rooms, client, None)
                    wake.set()

            if login_task and not login_task.done():
                login_task.cancel()
            login_task = asyncio.create_task(wait())
            publish(src, rooms, client, login)
            return {"url": flow["url"], "user_code": flow["user_code"]}

        async def do_logout() -> dict[str, Any]:
            nonlocal rooms
            client.logout()
            rooms = []
            publish(src, rooms, client, None)
            return {"linked": False}

        async def control(room_id: str, changes: dict[str, Any]) -> dict[str, Any]:
            nonlocal rooms
            idx = next((i for i, r in enumerate(rooms) if str(r["id"]) == str(room_id)), None)
            if idx is None:
                raise KeyError(room_id)
            term = config.tado_manual_until
            try:
                if changes.get("mode") == "schedule":
                    await client.resume(int(room_id))
                elif changes.get("mode") == "off":
                    await client.set_manual(int(room_id), None, term)
                elif changes.get("target") is not None:
                    await client.set_manual(int(room_id), float(changes["target"]), term)
                else:
                    raise ValueError("niets te veranderen")
            except TadoRateLimited as exc:
                raise ConnectionError("de daglimiet van tado is bereikt; probeer het later of gebruik de tado-app") from exc
            except TadoAuthError as exc:
                raise ConnectionError(str(exc)) from exc
            rooms[idx] = apply_change(rooms[idx], changes, term)
            publish(src, rooms, client, login)
            return rooms[idx]

        src.control = control
        src.actions = {"login": do_login, "logout": do_logout}
        publish(src, rooms, client, None)

        while True:
            delay = config.tado_poll_minutes * 60
            if not client.linked:
                client.reload()  # misschien elders gekoppeld
            if client.linked:
                try:
                    rooms = await client.rooms()
                    publish(src, rooms, client, login)
                    if client.remaining is not None and client.remaining < 10:
                        delay = max(delay, client.refill_in or 3600)  # bewaar de rest voor bediening
                except TadoRateLimited as exc:
                    publish(src, rooms, client, login, "daglimiet van tado bereikt")
                    delay = max(delay, exc.retry_after or 3600)
                except TadoAuthError as exc:
                    client.logout()
                    rooms = []
                    publish(src, rooms, client, login, str(exc))
                except Exception as exc:  # noqa: BLE001
                    msg = f"{type(exc).__name__}: {exc}"
                    log.warning("tado: %s", msg)
                    publish(src, rooms, client, login, msg)
                    delay = min(delay, 300)
            else:
                publish(src, rooms, client, login)
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
