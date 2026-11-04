import asyncio

import pytest

from app import aircontimer as T
from app.state import state


def test_resolve_minutes_and_at():
    now = 1_000_000.0
    assert T.resolve_off_at({"minutes": 90}, now) == now + 90 * 60
    assert T.resolve_off_at({"at": now + 3600}, now) == now + 3600
    for bad in ({}, {"minutes": "abc"}, {"minutes": 0}, {"at": now - 5}, {"minutes": 25 * 60}, {"at": "x"}):
        with pytest.raises(ValueError):
            T.resolve_off_at(bad, now)


def test_timers_persist_and_clear():
    store = {}
    t = T.Timers(lambda: store.get("v"), lambda v: store.__setitem__("v", v))
    t.set("boven", 2000.0, now=1000.0)
    t.set("jonna", 3000.0, now=1000.0)
    again = T.Timers(lambda: store.get("v"), lambda v: store.__setitem__("v", v))
    assert again.off_at("boven") == 2000.0
    assert again.clear("boven") and not again.clear("boven")
    assert again.clear_all() == 1
    assert T.Timers(lambda: store.get("v")).all() == {}
    assert T.Timers(lambda: "{kapot").all() == {}


def test_decide():
    t = {"off_at": 5000.0, "set_at": 1000.0}
    on = {"id": "a", "online": True, "on": True}
    off = {"id": "a", "online": True, "on": False}
    offline = {"id": "a", "online": False, "on": True}
    assert T.decide(t, on, 4000) == "wait"
    assert T.decide(t, on, 5000) == "fire"
    assert T.decide(t, offline, 5000) == "fire"  # proberen; mislukt het, dan straks opnieuw
    assert T.decide(t, off, 4000) == "drop"  # met de hand uitgezet
    assert T.decide(t, off, 1050) == "wait"  # net ingesteld, status loopt misschien achter
    assert T.decide(t, None, 4000) == "drop"


@pytest.fixture
def fake_aircon(monkeypatch):
    store = {}
    monkeypatch.setattr(T, "timers", T.Timers(lambda: store.get("v"), lambda v: store.__setitem__("v", v)))
    calls = []
    units = [{"id": "a", "name": "A", "online": True, "on": True},
             {"id": "b", "name": "B", "online": True, "on": True}]
    fail = {"a": False}

    async def control(uid, changes):
        calls.append((uid, changes))
        if fail.get(uid):
            raise ConnectionError("weg")
        for u in units:
            if u["id"] == uid:
                u["on"] = False
        return {}

    monkeypatch.setattr(state.aircon, "control", control)
    monkeypatch.setattr(state.aircon, "values", {"units": units})
    return calls, units, fail


def test_check_fires_due_timers_only(fake_aircon):
    calls, units, _ = fake_aircon
    T.timers.set("a", 1000.0, now=0)
    T.timers.set("b", 9000.0, now=0)
    assert asyncio.run(T.check(now=1001)) == ["a"]
    assert calls == [("a", {"on": False})]
    assert T.timers.off_at("a") is None and T.timers.off_at("b") == 9000.0


def test_check_retries_then_gives_up(fake_aircon):
    calls, _, fail = fake_aircon
    fail["a"] = True
    T.timers.set("a", 1000.0, now=0)
    asyncio.run(T.check(now=1100))
    assert T.timers.off_at("a") == 1000.0  # blijft staan voor een nieuwe poging
    asyncio.run(T.check(now=1000 + T.RETRY_FOR + 1))
    assert T.timers.off_at("a") is None
    assert len(calls) == 2


def test_check_keeps_timers_until_status_known(fake_aircon, monkeypatch):
    monkeypatch.setattr(state.aircon, "values", {})
    T.timers.set("a", 1000.0, now=0)
    asyncio.run(T.check(now=5000))
    assert T.timers.off_at("a") == 1000.0


def test_timer_minutes_setting():
    from app.config import DEFAULT_TIMER_MINUTES, parse_minutes

    assert parse_minutes("15, 45,90;600,abc,0,45") == [15, 45, 90, 600]
    assert parse_minutes("") == DEFAULT_TIMER_MINUTES
    assert parse_minutes("2000") == DEFAULT_TIMER_MINUTES  # meer dan 24 uur telt niet
    assert len(parse_minutes(",".join(str(i) for i in range(1, 20)))) == 8
