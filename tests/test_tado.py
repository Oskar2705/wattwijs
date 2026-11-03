import asyncio
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.integrations import tado

ROOM = {
    "id": 1,
    "name": "Woonkamer",
    "sensorDataPoints": {"insideTemperature": {"value": 20.4}, "humidity": {"percentage": 52.0}},
    "setting": {"power": "ON", "temperature": {"value": 21.0}},
    "manualControlTermination": {"type": "NEXT_TIME_BLOCK", "remainingTimeInSeconds": 3600,
                                 "projectedExpiry": "2026-11-03T16:00:00Z"},
    "boostMode": None,
    "heatingPower": {"percentage": 40},
    "connection": {"state": "CONNECTED"},
    "openWindow": None,
    "nextScheduleChange": {"start": "2026-11-03T16:00:00Z", "setting": {"power": "ON", "temperature": {"value": 19.0}}},
    "nextTimeBlock": {"start": "2026-11-03T16:00:00Z"},
}


def test_parse_room():
    r = tado.parse_room(ROOM)
    assert r["name"] == "Woonkamer" and r["room_temp"] == 20.4 and r["humidity"] == 52.0
    assert r["on"] is True and r["target"] == 21.0 and r["heating_pct"] == 40
    assert r["mode"] == "manual" and r["manual_type"] == "NEXT_TIME_BLOCK" and r["manual_until"] is not None
    assert r["next_change_target"] == 19.0 and r["online"] is True
    off = tado.parse_room({**ROOM, "setting": {"power": "OFF"}, "manualControlTermination": None})
    assert off["on"] is False and off["target"] is None and off["mode"] == "schedule"


def test_manual_payload_and_ratelimit():
    p = tado.manual_payload(21.3, "next_block")
    assert p["setting"]["temperature"]["value"] == 21.5 and p["termination"]["type"] == "NEXT_TIME_BLOCK"
    assert tado.manual_payload(None, "manual") == {"setting": {"power": "OFF"}, "termination": {"type": "MANUAL"}}
    with pytest.raises(ValueError):
        tado.manual_payload(30, "manual")
    assert tado.parse_ratelimit('"perday";r=87') == (87, None)
    assert tado.parse_ratelimit('"perday";r=0;t=123') == (0, 123.0)
    assert tado.parse_ratelimit(None) == (None, None)


def test_apply_change():
    r = tado.parse_room({**ROOM, "manualControlTermination": None})
    assert tado.apply_change(r, {"target": 22.2}, "next_block")["target"] == 22.0
    assert tado.apply_change(r, {"mode": "off"}, "manual")["on"] is False
    assert tado.apply_change({**r, "mode": "manual"}, {"mode": "schedule"}, "manual")["mode"] == "schedule"


class FakeTado:
    """Mini-nabootsing van login.tado.com, my.tado.com en hops.tado.com."""

    def __init__(self):
        self.polls = 0
        self.refresh_counter = 0
        self.valid_access = set()
        self.posts = []
        self.limit_left = 5
        app = web.Application()
        app.router.add_post("/oauth2/device_authorize", self.authorize)
        app.router.add_post("/oauth2/token", self.token)
        app.router.add_get("/api/v2/me", self.me)
        app.router.add_get("/api/v2/homes/{hid}", self.home)
        app.router.add_get("/homes/{hid}/rooms", self.rooms)
        app.router.add_get("/homes/{hid}/roomsAndDevices", self.rooms_and_devices)
        app.router.add_post("/homes/{hid}/rooms/{rid}/manualControl", self.manual)
        app.router.add_post("/homes/{hid}/rooms/{rid}/resumeSchedule", self.resume)
        self.app = app

    def _tok(self):
        self.refresh_counter += 1
        acc = f"acc{self.refresh_counter}"
        self.valid_access.add(acc)
        return web.json_response({"access_token": acc, "refresh_token": f"ref{self.refresh_counter}", "expires_in": 599})

    async def authorize(self, req):
        return web.json_response({"device_code": "DEV", "user_code": "ABCD", "verification_uri": "https://login/x",
                                  "expires_in": 30, "interval": 0})

    async def token(self, req):
        if req.query.get("grant_type", "").endswith("device_code"):
            self.polls += 1
            if self.polls < 2:
                return web.json_response({"error": "authorization_pending"}, status=400)
            return self._tok()
        form = await req.post()
        if form.get("refresh_token") != f"ref{self.refresh_counter}":
            return web.json_response({"error": "invalid_grant"}, status=400)
        return self._tok()

    def _auth(self, req):
        tok = req.headers.get("Authorization", "").removeprefix("Bearer ")
        if tok not in self.valid_access:
            raise web.HTTPUnauthorized()

    def _limited(self, data, status=200):
        self.limit_left -= 1
        if self.limit_left < 0:
            return web.json_response({}, status=429, headers={"ratelimit": '"perday";r=0;t=600'})
        return web.json_response(data, status=status, headers={"ratelimit": f'"perday";r={self.limit_left}'})

    async def me(self, req):
        self._auth(req)
        return self._limited({"homes": [{"id": 42}]})

    async def home(self, req):
        self._auth(req)
        return self._limited({"id": 42, "generation": "LINE_X"})

    async def rooms(self, req):
        self._auth(req)
        return self._limited([ROOM])

    async def rooms_and_devices(self, req):
        self._auth(req)
        return self._limited({"rooms": [{"roomId": 1, "roomName": "Woonkamer", "devices": []}], "otherDevices": []})

    async def manual(self, req):
        self._auth(req)
        self.posts.append(("manual", req.match_info["rid"], await req.json()))
        return self._limited({})

    async def resume(self, req):
        self._auth(req)
        self.posts.append(("resume", req.match_info["rid"], None))
        return self._limited({})


def test_client_login_rooms_commands_and_limit(monkeypatch, tmp_path):
    async def main():
        fake = FakeTado()
        server = TestServer(fake.app)
        await server.start_server()
        base = str(server.make_url("/"))
        monkeypatch.setattr(tado, "LOGIN", base + "oauth2/")
        monkeypatch.setattr(tado, "MY_API", base + "api/v2/")
        monkeypatch.setattr(tado, "HOPS", base)
        store = tado.TokenStore(str(tmp_path / "tok.json"))
        try:
            async with aiohttp.ClientSession() as s:
                c = tado.TadoClient(s, store)
                assert not c.linked
                flow = await c.start_login()
                assert "ABCD" in flow["url"]
                flow["interval"] = 0.01
                monkeypatch.setattr(tado.asyncio, "sleep", _fast_sleep)
                await c.finish_login(flow)
                assert c.linked and store.load()["refresh_token"] == "ref1"

                rooms = await c.rooms()
                assert rooms[0]["name"] == "Woonkamer" and store.load()["home_id"] == 42
                assert c.remaining == 2  # me, home, rooms

                # verlopen toegangstoken -> automatisch vernieuwen, nieuwe refresh-token bewaard
                fake.valid_access.clear()
                await c.set_manual(1, 21.0, "next_block")
                assert store.load()["refresh_token"] == "ref2"
                assert fake.posts[-1][2]["setting"]["temperature"]["value"] == 21.0

                await c.resume(1)
                with pytest.raises(tado.TadoRateLimited):
                    await c.rooms()
        finally:
            await server.close()

    real_sleep = asyncio.sleep

    async def _fast_sleep(_s):
        await real_sleep(0)

    asyncio.run(main())


def test_comfort_warm_up():
    r = tado.parse_room({**ROOM, "manualControlTermination": None, "setting": {"power": "ON", "temperature": {"value": 19.0}}})
    ch = tado.comfort_changes(r, None, None)
    assert ch["target"] == tado.config.tado_comfort_temp and ch["hours"] == tado.config.tado_comfort_hours
    # stond hij al hoger, dan niet omlaag
    hot = {**r, "target": 23.0}
    assert tado.comfort_changes(hot)["target"] == 23.0
    # expliciete temperatuur wint
    assert tado.comfort_changes(hot, 20.0, 1)["target"] == 20.0
    p = tado.manual_payload(21, "next_block", hours=3)
    assert p["termination"] == {"type": "TIMER", "durationInSeconds": 10800}
    after = tado.apply_change(r, {"target": 21, "hours": 3}, "next_block")
    assert after["manual_type"] == "TIMER" and after["manual_until"] > 0


def test_room_names_from_roomsanddevices_and_overrides(monkeypatch, tmp_path):
    async def main():
        fake = FakeTado()
        fake.limit_left = 50
        nameless = {k: v for k, v in ROOM.items() if k != "name"}
        async def rooms(req):
            fake._auth(req)
            return fake._limited([nameless])
        server = TestServer(_app_with_rooms(fake, rooms))
        await server.start_server()
        base = str(server.make_url("/"))
        monkeypatch.setattr(tado, "LOGIN", base + "oauth2/")
        monkeypatch.setattr(tado, "MY_API", base + "api/v2/")
        monkeypatch.setattr(tado, "HOPS", base)
        store = tado.TokenStore(str(tmp_path / "tok.json"))
        store.save({"refresh_token": "ref0", "home_id": 42})
        fake.refresh_counter = 0
        try:
            async with aiohttp.ClientSession() as s:
                c = tado.TadoClient(s, store)
                r = (await c.rooms())[0]
                assert r["name"] == "Woonkamer" and store.load()["room_names"] == {"1": "Woonkamer"}
                monkeypatch.setattr(tado.config, "tado_room_names", "1=Huiskamer")
                assert (await c.rooms())[0]["name"] == "Huiskamer"
        finally:
            await server.close()

    asyncio.run(main())


def _app_with_rooms(fake, rooms_handler):
    app = web.Application()
    app.router.add_post("/oauth2/token", fake.token)
    app.router.add_get("/homes/{hid}/rooms", rooms_handler)
    app.router.add_get("/homes/{hid}/roomsAndDevices", fake.rooms_and_devices)
    return app
