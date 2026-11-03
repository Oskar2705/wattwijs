import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import pytest

from app import integrations
from app.config import config


def test_every_role_has_integrations_and_demo():
    for role in integrations.ROLES:
        keys = {i.key for i in integrations.available(role)}
        assert "demo" in keys, role
    assert {"smeter", "homewizard"} <= {i.key for i in integrations.available("grid")}
    assert {"solaredge_modbus", "solaredge_cloud"} <= {i.key for i in integrations.available("solar")}


def test_live_integrations_have_run_and_providers_have_factory():
    for i in integrations.available():
        if i.role in integrations.LIVE_ROLES:
            assert i.run is not None and i.factory is None, i.key
        else:
            assert i.factory is not None, i.key
            obj = i.factory()
            if i.role == "panels":
                assert hasattr(obj, "panels") and hasattr(obj, "panel_energy")
            if i.role == "prices":
                assert hasattr(obj, "fetch_day")


def test_legacy_settings_still_select_integrations(monkeypatch):
    monkeypatch.setattr(config, "demo", False)
    monkeypatch.setattr(config, "p1_host", "1.2.3.4")
    monkeypatch.setattr(config, "p1_type", "smeter")
    monkeypatch.setattr(config, "battery_host", "")
    monkeypatch.setattr(config, "se_modbus_host", "")
    monkeypatch.setattr(config, "se_username", "a")
    monkeypatch.setattr(config, "se_password", "b")
    monkeypatch.setattr(config, "se_site_id", "1")
    monkeypatch.setattr(config, "dw_username", "")
    for r in integrations.ROLES:
        monkeypatch.delenv(f"{r.upper()}_INTEGRATION", raising=False)
    assert integrations.selected("grid") == "smeter"
    assert integrations.selected("battery") is None
    assert integrations.selected("solar") == "solaredge_cloud"
    assert integrations.selected("panels") == "solaredge"
    assert integrations.selected("heatpump") is None
    monkeypatch.setenv("GRID_INTEGRATION", "homewizard")
    monkeypatch.setenv("SOLAR_INTEGRATION", "none")
    assert integrations.selected("grid") == "homewizard"
    assert integrations.selected("solar") is None


def test_unknown_integration_gives_helpful_error():
    with pytest.raises(ValueError, match="available: .*smeter"):
        integrations.get("grid", "bestaatniet")
