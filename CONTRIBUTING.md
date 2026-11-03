# Contributing

Thanks for wanting to help. The most useful contribution is support for **your own devices**: another battery, P1 meter, inverter, heat pump or energy supplier. Translations of the Dutch interface are welcome too.

## Adding a device

Every brand is one file in `app/integrations/`. The app finds it automatically.

1. Copy `app/integrations/_template.py` to `app/integrations/<brand>.py`. Files that start with `_` are not loaded.
2. Fill in the `@register(role, key, title, env=..., description=...)` line:
   - `role` is one of `grid`, `battery`, `solar`, `heatpump`, `aircon`, `panels`, `prices`;
   - `key` is short, lowercase and unique within the role. Users select it with `<ROLE>_INTEGRATION=<key>`;
   - `env` lists the environment variables your integration reads. They show up in `/api/integrations`.
3. Live roles (`grid`, `battery`, `solar`, `heatpump`, `aircon`) register an `async def run(src)` coroutine. It reads the device in a loop and calls `src.update(...)` with the fields in the table below.
4. Provider roles (`panels`, `prices`) register a class or factory instead; see the template for the methods.
5. Add your settings to `.env.example` and a row to the table in both READMEs.
6. Add a test.

### Fields per role

All power values are in watts. `None` means unknown.

| Role | Field | Meaning |
|---|---|---|
| `grid` | `grid_w` | + import from the grid, − export to the grid |
| | `import_kwh`, `export_kwh` | meter totals (optional) |
| `battery` | `soc` | state of charge, % |
| | `battery_w` | + charging, − discharging (DC side) |
| | `battery_ac_w` | + delivering to the house, − taking from the house |
| | `pv_w` | panels connected directly to the battery (optional) |
| | `grid_w` | when the battery reads the smart meter itself (optional) |
| `solar` | `power_w` | current AC production of the inverter |
| `heatpump` | `power_w` | electrical power used |
| | `heat_output_w`, `supply_temperature`, `outdoor_temperature`, `is_on`, `fault_code` | optional |
| `aircon` | `power_w` | all units together (may be estimated) |
| | `units_on` | number of units switched on |
| | `units` | list of unit dicts, see `unit_view()` in `app/integrations/wfrac.py` |

### Rules

- **Read only.** An integration must never change a setting on a device. The one exception is the `aircon` role: it may set `src.control` to an async `control(unit_id, changes)` that is only called when a user presses a button, and it must respect `AIRCO_CONTROL=0`.
- **Never crash the loop.** Catch exceptions, call `src.error(message)`, wait, and try again. The footer of the app shows the last error, and the watchdog sends an alert when a source stays silent.
- **No secrets in code or logs.** Read them from environment variables.
- **Keep parsing separate from I/O.** A pure `parse(raw) -> dict` function can be tested without the device.
- **Prefer local access over the cloud** when the device offers both.

### Testing

Record a real response from your device, remove anything personal (serial numbers, IP addresses, locations), and use it in a test:

```python
from app.integrations.mybrand import parse

RAW = {"power_kw": 1.25, "outdoor_c": 7.5}  # recorded from a real device

def test_parse():
    assert parse(RAW)["power_w"] == 1250
```

Run the tests with:

```bash
pip install -r requirements.txt pytest
pytest
```

To check that the app starts with your integration (outside demo mode):

```bash
DEMO=0 HEATPUMP_INTEGRATION=mybrand MYBRAND_HOST=192.168.1.20 DATA_DIR=./data uvicorn app.main:app
curl localhost:8000/api/integrations
```

## Other changes

- Open an issue first for larger changes, so we can agree on the approach.
- Keep the front end framework-free: plain HTML, CSS and JavaScript in `app/static/`.
- The interface text is Dutch for now. If you add text, write it in plain, short sentences.
- Make sure `pytest` passes before opening a pull request. GitHub Actions runs the tests and a Docker build on every pull request.

## Privacy

Never commit a `.env` file, a database from `data/`, or screenshots with real addresses, serial numbers or names. The screenshots in `docs/` are made in demo mode (`DEMO=1`).
