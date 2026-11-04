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


def test_morning_message():
    d = date(2030, 3, 10)
    _seed_day(d - timedelta(days=1))
    _seed_prices(d)
    get_db().x("INSERT INTO panel_alert(serial,name,opened_day,deviation,mail_status) VALUES('X','1.1.4','2030-03-01',-97,'x')")
    now = datetime.combine(d, datetime.min.time(), tzinfo=config.tz).replace(hour=7, minute=30)
    title, body = notifier.build_morning(now)  # zonder zonverwachting: zoals vroeger, op prijs
    assert title == "Vandaag het goedkoopst: 13:00–15:00"
    assert "Goedkoopste stroom: 13:00–15:00" in body and "Duurst rond 18:00" in body
    assert "Tussen 14:00 en 15:00 is de prijs negatief" in body
    assert "Zonnepaneel 1.1.4 doet het nog steeds niet goed" in body
    title, body = notifier.build_morning(now, {"sun_hours": 4.2, "solar_kwh": 6.3, "block": None})
    assert title == "Vandaag zo'n 4 uur zon en ongeveer 6,3 kWh zonnestroom"
    assert "Gisteren was het 8,4 kWh." in body and "weinig zonnestroom over" in body
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
    assert "Opgewekt: 12,6 kWh (verwacht was 11,0 kWh)." in body
    assert "Verbruikt:" in body and "% van eigen panelen" in body
    assert "Nieuw record" in body
    assert notifier.build_evening(d0 + timedelta(days=5))[1].count("Nieuw record") == 0
    sunday = date(2031, 5, 18)
    assert sunday.weekday() == 6
    title, body = notifier.build_weekly(sunday)
    assert title.startswith("📊") and "Zonnigste dag:" in body
    assert "Ongeveer evenveel zon als vorige week." in body
    assert notifier.build_weekly(date(2031, 5, 25)) is None  # maar 3 dagen gemeten
