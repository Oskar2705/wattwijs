import asyncio
import base64
import json
import os
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import http_ece
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app import notifier, push
from app.config import config
from app.db import get_db

b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731


class FakePushService:
    """Ontvangt pushberichten zoals Apple/Google dat doen en ontsleutelt ze met de sleutel van de 'telefoon'."""

    def __init__(self, status=201):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.received = []
        self.status = status
        svc = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                svc.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(svc.status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def subscription(self, path="device1"):
        pub = self.priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return {"endpoint": f"http://127.0.0.1:{self.httpd.server_port}/{path}",
                "keys": {"p256dh": b64(pub), "auth": b64(self.auth)}}

    def decrypt(self, i=-1):
        headers, body = self.received[i]
        return headers, json.loads(http_ece.decrypt(body, private_key=self.priv, auth_secret=self.auth, version="aes128gcm"))


def test_end_to_end_encrypted_delivery():
    for s in push.subscriptions():
        push.unsubscribe(s["endpoint"])
    svc = FakePushService()
    sub = svc.subscription()
    prefs = push.subscribe(sub, {"tech": True}, "test")
    assert prefs["morning"] and prefs["tech"]
    assert asyncio.run(push.broadcast("morning", "Titel", "Tekst", url="/#nu")) >= 1
    headers, payload = svc.decrypt()
    assert payload == {"title": "Titel", "body": "Tekst", "url": "/#nu", "tag": "morning"}
    assert headers["authorization"].startswith("vapid t=") and f"k={push.public_key()}" in headers["authorization"]
    assert int(headers["ttl"]) == 12 * 3600 and headers["content-encoding"] == "aes128gcm"
    push.unsubscribe(sub["endpoint"])


def test_prefs_filter_and_expired_subscription_removed():
    for s in push.subscriptions():
        push.unsubscribe(s["endpoint"])
    svc = FakePushService(status=410)
    sub = svc.subscription("gone")
    push.subscribe(sub, {"weekly": False}, None)
    assert asyncio.run(push.broadcast("weekly", "x", "y")) == 0 and not svc.received  # staat uit
    assert asyncio.run(push.broadcast("morning", "x", "y")) == 0                       # 410 = verlopen
    assert push.get_prefs(sub["endpoint"]) is None                                       # opgeruimd


def test_invalid_subscription_rejected():
    with pytest.raises(ValueError):
        push.subscribe({"endpoint": "x"}, None, None)


def _seed_day(d: date, solar_scale=1.0, import_wh=6.0):
    """Eén dag minuten: overdag zon, 's avonds afname."""
    rows = []
    start = int(datetime.combine(d, datetime.min.time(), tzinfo=config.tz).timestamp())
    for m in range(1440):
        ts = start + m * 60
        local = datetime.fromtimestamp(ts, config.tz)
        sun = 20.0 * solar_scale if 9 <= local.hour < 16 else 0.0
        imp = 0.0 if sun else import_wh
        rows.append((ts, d.isoformat(), local.hour, imp, max(sun - 5, 0), sun, 0, 0, 2.0, 5.0 if sun else imp, 0,
                     None, None, None, None, None, None, None))
    get_db().write_minutes(rows)


def _seed_prices(d: date):
    start = int(datetime.combine(d, datetime.min.time(), tzinfo=config.tz).timestamp())
    rows = []
    for q in range(96):
        h = q / 4
        market = 0.02 if 12 <= h < 14 else (-0.03 if 14 <= h < 15 else (0.30 if 18 <= h < 19 else 0.10))
        rows.append((start + q * 900, d.isoformat(), market, round(market + 0.13, 4)))
    get_db().save_prices(rows)


def test_morning_message(monkeypatch):
    d = date(2030, 3, 10)
    _seed_day(d - timedelta(days=1))
    _seed_prices(d)
    get_db().x("INSERT INTO panel_alert(serial,name,opened_day,deviation,mail_status) VALUES('X','1.1.4','2030-03-01',-97,'x')")
    now = datetime.combine(d, datetime.min.time(), tzinfo=config.tz).replace(hour=7, minute=30)
    # salderen: geen prijstips
    monkeypatch.setattr(config, "contract_start", "2031-01-01")
    title, body = notifier.build_morning(now)
    assert title == "Goedemorgen" and "cent" not in body
    assert "Zonnepaneel 1.1.4 doet het nog steeds niet goed" in body
    # dynamisch contract: wel prijzen
    monkeypatch.setattr(config, "contract_start", "2030-01-01")
    title, body = notifier.build_morning(now)  # zonder zonverwachting: op prijs
    assert title == "Vandaag het goedkoopst: 13:00–15:00"
    assert "goedkoopst tussen 13:00 en 15:00" in body and "duurst is het rond 18:00" in body
    assert "Tussen 14:00 en 15:00 is de prijs negatief" in body
    title, body = notifier.build_morning(now, {"sun_hours": 4.2, "solar_kwh": 6.3, "block": None})
    assert title == "☀️ Vandaag zo'n 4 uur zon (ongeveer 6,3 kWh)"
    assert "weinig zonnestroom over" in body
    get_db().x("DELETE FROM panel_alert WHERE serial='X'")


def test_evening_record_and_weekly():
    d0 = date(2031, 5, 1)
    for i in range(20):
        _seed_day(d0 + timedelta(days=i), solar_scale=1.0)
    rec = d0 + timedelta(days=20)
    _seed_day(rec, solar_scale=1.5)
    get_db().set_kv(f"solar_expected_{rec.isoformat()}", "11.0")
    title, body = notifier.build_evening(rec)
    assert title.startswith("Vandaag") and "eigen stroom" in title
    assert "De panelen wekten 12,6 kWh op, meer dan de 11,0 kWh die vanochtend verwacht werd." in body
    assert "Het huis gebruikte" in body and "meteen het huis in" in body and "kwam van het net" in body
    assert "Stroom vandaag" not in body  # geen contract ingesteld: geen bedragen
    assert "Nieuw record" in body
    assert notifier.build_evening(d0 + timedelta(days=5))[1].count("Nieuw record") == 0
    sunday = date(2031, 5, 18)
    assert sunday.weekday() == 6
    title, body = notifier.build_weekly(sunday)
    assert title.startswith("📊") and "Zonnigste dag:" in body
    assert "Ongeveer evenveel zon als vorige week." in body
    assert notifier.build_weekly(date(2031, 5, 25)) is None  # maar 3 dagen gemeten


def test_evening_battery_split_and_price_coverage(monkeypatch):
    """Batterij laadt 's nachts van het net en overdag met zon: per minuut uit elkaar houden.
    Bedragen alleen als (vrijwel) de hele dag een prijs heeft."""
    d = date(2031, 6, 3)
    start = int(datetime.combine(d, datetime.min.time(), tzinfo=config.tz).timestamp())
    rows = []
    for m in range(1440):
        ts = start + m * 60
        hr = datetime.fromtimestamp(ts, config.tz).hour
        if 2 <= hr < 4:      # nacht: 1,2 kW van het net de batterij in, huis 0,3 kW
            imp, exp, sun, chg, dis, house = 25.0, 0.0, 0.0, 20.0, 0.0, 5.0
        elif 10 <= hr < 14:  # middag: 3 kW zon, 1,2 kW de batterij in, 0,6 kW naar het net
            imp, exp, sun, chg, dis, house = 0.0, 10.0, 50.0, 20.0, 0.0, 20.0
        elif 18 <= hr < 22:  # avond: batterij levert
            imp, exp, sun, chg, dis, house = 0.0, 0.0, 0.0, 0.0, 15.0, 15.0
        else:
            imp, exp, sun, chg, dis, house = 5.0, 0.0, 0.0, 0.0, 0.0, 5.0
        rows.append((ts, d.isoformat(), hr, imp, exp, sun, chg, dis, 0.0, house, 0.0,
                     None, None, None, None, None, None, None))
    get_db().write_minutes(rows)
    s = get_db().solar_split(d.isoformat())
    assert s["solar_to_batt_wh"] == pytest.approx(4 * 60 * 20)      # 4,8 kWh zon de batterij in
    assert s["grid_to_batt_wh"] == pytest.approx(2 * 60 * 20)       # 2,4 kWh net de batterij in
    assert s["solar_direct_wh"] == pytest.approx(4 * 60 * 20)       # 4,8 kWh zon direct
    monkeypatch.setattr(config, "contract_start", "2031-01-01")
    _, body = notifier.build_evening(d)
    assert "4,8 kWh meteen het huis in, 4,8 kWh de batterij in" in body
    assert "De batterij laadde 2,4 kWh van het net bij." in body.replace("Daarnaast laadde", "De batterij laadde") \
        or "Daarnaast laadde de batterij 2,4 kWh van het net bij." in body
    assert "€" not in body  # geen prijzen voor deze dag
    # maar een paar kwartieren met prijs: nog steeds geen bedragen (zou te laag uitvallen)
    get_db().save_prices([(start + q * 900, d.isoformat(), 0.1, 0.25) for q in range(10)])
    _, body = notifier.build_evening(d)
    assert "€" not in body
    get_db().save_prices([(start + q * 900, d.isoformat(), 0.1, 0.25) for q in range(96)])
    _, body = notifier.build_evening(d)
    assert "Stroom vandaag: €" in body
