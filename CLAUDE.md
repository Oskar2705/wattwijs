# Wattwijs – notes for coding agents

Self-hosted home energy dashboard: FastAPI back end in `app/`, plain HTML/CSS/JS front end in `app/static/` (no build step, no framework), SQLite in `DATA_DIR`.

## Layout
- `app/integrations/` – one file per device brand, registered with `@register(role, key, ...)`. Roles: grid, battery, solar, heatpump, aircon, heating (live `run(src)` loops) and panels, prices (providers). Template: `_template.py`. Selection: `<ROLE>_INTEGRATION`, otherwise inferred from legacy env vars in `integrations.selected()`.
- `app/state.py` – live snapshot; house = grid + solar + battery AC output.
- `app/panels.py` – per-panel check (compare with neighbours in the same row of `PANEL_LAYOUT`).
- `app/prices.py`, `app/notifier.py`, `app/push.py`, `app/exportwatch.py` – prices/costs, push messages, Web Push, export push.
- tado allows 100 API requests a day without Auto-Assist: keep polling sparse and don't refetch after commands.
- `tests/` – pytest, recorded device responses. Run `pytest -q`.

## Rules
- Integrations are **read-only**: never change device settings. Only exceptions: the `aircon` and `heating` roles, and only on an explicit user action in the Klimaat tab (`src.control`). `AIRCO_CONTROL=0` makes the air conditioners read-only.
- Never crash a loop: catch, `src.error(msg)`, back off, retry.
- The UI text is **Dutch**, short plain sentences, for a non-technical user. Code comments and docs may be English.
- No personal data in the repo: no real IPs, serials, site IDs, names or addresses. Screenshots only from `DEMO=1`.
- Static assets are cache-busted automatically (`ASSET_VERSION`); no manual version bumps needed.

## Run locally
```bash
pip install -r requirements.txt pytest
DEMO=1 DATA_DIR=./data uvicorn app.main:app --reload
```
Deploy: `git pull && docker compose up -d --build` (keeps `.env` and `data/`).
