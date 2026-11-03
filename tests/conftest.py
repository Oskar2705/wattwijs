"""Vaste omgeving voor alle tests, ongeacht de computer waarop ze draaien."""

import os
import tempfile

# De tests rekenen met Nederlandse tijd; GitHub-servers staan op UTC.
os.environ["TZ"] = "Europe/Amsterdam"
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
