"""Instellingen, allemaal via omgevingsvariabelen (zie .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class Config:
    # Algemeen
    demo: bool = field(default_factory=lambda: _bool("DEMO"))
    tz_name: str = field(default_factory=lambda: _str("TZ", "Europe/Amsterdam"))
    data_dir: str = field(default_factory=lambda: _str("DATA_DIR", "/data"))
    app_url: str = field(default_factory=lambda: _str("APP_URL"))
    site_name: str = field(default_factory=lambda: _str("SITE_NAME", "Thuis"))
    basic_auth_user: str = field(default_factory=lambda: _str("BASIC_AUTH_USER"))
    basic_auth_pass: str = field(default_factory=lambda: _str("BASIC_AUTH_PASS"))

    # Thuisbatterij (Voltdeer / AECC) - levert ook de P1-meterwaarde
    battery_host: str = field(default_factory=lambda: _str("BATTERY_HOST"))
    battery_port: int = field(default_factory=lambda: _int("BATTERY_PORT", 8080))
    battery_poll_s: float = field(default_factory=lambda: _float("BATTERY_POLL_SECONDS", 5))
    grid_invert: bool = field(default_factory=lambda: _bool("GRID_INVERT"))

    # Slimme meter rechtstreeks (los van de batterij)
    p1_host: str = field(default_factory=lambda: _str("P1_HOST"))
    p1_type: str = field(default_factory=lambda: _str("P1_TYPE", "smeter"))  # smeter | homewizard
    p1_poll_s: float = field(default_factory=lambda: _float("P1_POLL_SECONDS", 5))

    # SolarEdge
    se_modbus_host: str = field(default_factory=lambda: _str("SOLAREDGE_MODBUS_HOST"))
    se_modbus_port: int = field(default_factory=lambda: _int("SOLAREDGE_MODBUS_PORT", 1502))
    se_modbus_poll_s: float = field(default_factory=lambda: _float("SOLAREDGE_MODBUS_POLL_SECONDS", 5))
    se_username: str = field(default_factory=lambda: _str("SOLAREDGE_USERNAME"))
    se_password: str = field(default_factory=lambda: _str("SOLAREDGE_PASSWORD"))
    se_site_id: str = field(default_factory=lambda: _str("SOLAREDGE_SITE_ID"))
    se_cloud_poll_s: float = field(default_factory=lambda: _float("SOLAREDGE_CLOUD_POLL_SECONDS", 120))

    # DeWarmte
    dw_username: str = field(default_factory=lambda: _str("DEWARMTE_USERNAME"))
    dw_password: str = field(default_factory=lambda: _str("DEWARMTE_PASSWORD"))
    dw_poll_s: float = field(default_factory=lambda: _float("DEWARMTE_POLL_SECONDS", 60))

    # Paneelcontrole
    panel_threshold_pct: float = field(default_factory=lambda: _float("PANEL_THRESHOLD_PERCENT", 20))
    panel_consecutive_days: int = field(default_factory=lambda: _int("PANEL_CONSECUTIVE_DAYS", 2))
    panel_min_reference_wh: float = field(default_factory=lambda: _float("PANEL_MIN_REFERENCE_WH", 100))
    # Fysieke ligging: rijen gescheiden door ';', panelen van links naar rechts gescheiden door ','
    panel_layout: str = field(default_factory=lambda: _str("PANEL_LAYOUT"))
    panel_check_time: str = field(default_factory=lambda: _str("PANEL_CHECK_TIME", "21:30"))
    # How often today's per-panel yield is fetched during daylight (the portal itself updates about every 15 min)
    panel_refresh_minutes: int = field(default_factory=lambda: max(5, _int("PANEL_REFRESH_MINUTES", 15)))
    panel_recovery_mail: bool = field(default_factory=lambda: _bool("PANEL_RECOVERY_MAIL", True))
    panel_backfill_days: int = field(default_factory=lambda: _int("PANEL_BACKFILL_DAYS", 14))

    # Stroomprijzen (Frank Energie, openbare API zonder login)
    prices_enabled: bool = field(default_factory=lambda: _bool("PRICES_ENABLED", True))
    contract_start: str = field(default_factory=lambda: _str("CONTRACT_START", "2027-01-01"))
    export_multiplier: float = field(default_factory=lambda: _float("EXPORT_MULTIPLIER", 1.0))
    export_fee_eur: float = field(default_factory=lambda: _float("EXPORT_FEE_EUR", 0.0))
    price_negative_mail: bool = field(default_factory=lambda: _bool("PRICE_NEGATIVE_MAIL", True))
    price_backfill_days: int = field(default_factory=lambda: _int("PRICE_BACKFILL_DAYS", 62))

    # Pushmeldingen (webapp op het beginscherm)
    push_enabled: bool = field(default_factory=lambda: _bool("PUSH_ENABLED", True))
    push_contact: str = field(default_factory=lambda: _str("PUSH_CONTACT"))
    push_morning_time: str = field(default_factory=lambda: _str("PUSH_MORNING_TIME", "07:30"))
    push_evening_time: str = field(default_factory=lambda: _str("PUSH_EVENING_TIME", "20:30"))
    push_weekly_time: str = field(default_factory=lambda: _str("PUSH_WEEKLY_TIME", "19:00"))  # zondag
    push_block_hours: float = field(default_factory=lambda: _float("PUSH_BLOCK_HOURS", 2))
    push_block_window: str = field(default_factory=lambda: _str("PUSH_BLOCK_WINDOW", "07:00-23:00"))

    # Mail
    smtp_host: str = field(default_factory=lambda: _str("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _str("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _str("SMTP_PASSWORD"))
    smtp_security: str = field(default_factory=lambda: _str("SMTP_SECURITY", "starttls"))  # starttls | ssl | none
    mail_from: str = field(default_factory=lambda: _str("MAIL_FROM"))
    alert_to: str = field(default_factory=lambda: _str("ALERT_TO"))
    admin_to: str = field(default_factory=lambda: _str("ADMIN_TO"))
    offline_alert_minutes: int = field(default_factory=lambda: _int("OFFLINE_ALERT_MINUTES", 120))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def mail_enabled(self) -> bool:
        return bool(self.smtp_host and self.mail_from)

    @staticmethod
    def split(addresses: str) -> list[str]:
        return [a.strip() for a in addresses.split(",") if a.strip()]


config = Config()
