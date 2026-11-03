"""Stuur een testmail: docker compose exec wattwijs python -m app.testmail"""

import asyncio

from . import mailer
from .config import config


async def main() -> None:
    to = config.split(config.alert_to) + config.split(config.admin_to)
    status = await mailer.send(
        sorted(set(to)),
        "Testmail van Wattwijs",
        "Als je dit leest, werken de meldingen per mail." + mailer.footer(),
    )
    print(f"Resultaat: {status}")


if __name__ == "__main__":
    asyncio.run(main())
