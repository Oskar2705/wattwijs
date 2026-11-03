"""E-mail versturen via SMTP."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import config

log = logging.getLogger(__name__)


def _send_sync(to: list[str], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = config.mail_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if config.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
    with server:
        if config.smtp_security == "starttls":
            server.starttls(context=ctx)
        if config.smtp_user:
            server.login(config.smtp_user, config.smtp_password)
        server.send_message(msg)


async def send(to: list[str], subject: str, body: str) -> str:
    """Verstuurt een mail; geeft een korte status terug voor de logboeken."""
    if not to:
        log.info("Geen ontvanger ingesteld, mail niet verstuurd: %s", subject)
        return "geen ontvanger ingesteld"
    if not config.mail_enabled:
        log.info("SMTP niet ingesteld, mail niet verstuurd: %s\n%s", subject, body)
        return "SMTP niet ingesteld"
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
        log.info("Mail verstuurd naar %s: %s", to, subject)
        return "verstuurd"
    except Exception as exc:  # noqa: BLE001
        log.error("Mail versturen mislukt: %s", exc)
        return f"mislukt: {exc}"


def footer() -> str:
    return f"\n\nBekijk het overzicht: {config.app_url}\n" if config.app_url else ""
