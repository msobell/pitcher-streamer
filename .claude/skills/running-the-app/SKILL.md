---
name: running-the-app
description: Launch, verify, and smoke-test the pitcher-streamer dashboard. Use when asked to run/start/serve the app, confirm a change works end-to-end, or debug why the server won't start or the page never loads.
---

# Running pitcher-streamer

## Preconditions (all local, all gitignored)

| File | Purpose | If missing |
|------|---------|------------|
| `config.yaml` | league id, `my_team_name`, `season` | copy `config.yaml.example`; `main.py` raises SystemExit at import |
| `park_factors.json` | Savant park run indexes, season-stamped | `python refresh_park_factors.py` (needs `BASEBALL_SAVANT_YEAR` from `.envrc`) |
| `.envrc` | `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `BASEBALL_SAVANT_YEAR` | direnv must be loaded or exports set manually |
| `oauth2.json` | Yahoo token cache | first request triggers an interactive browser OAuth flow — cannot complete headlessly |

Startup also fails (by design) if `park_factors.json` `season` ≠ `config.yaml` `season`.

## Launch

```bash
.venv/bin/pitcher-streamer serve            # default 127.0.0.1:8001, port+file guards
# or: .venv/bin/uvicorn main:app --port 8001
```

- First page load returns a **loading shell** that polls `/?week=N` every 3s while the data build runs (15–30s: Yahoo + MLB Stats + Savant + FanGraphs + Open-Meteo). This is normal, not a hang.
- If the page polls forever, check the server log for `Build failed: week_offset=...` — cold-start build exceptions are logged there (commonly Yahoo OAuth needing re-auth).
- `?week=0` = this week, `?week=1` = next week. Background thread rebuilds both every 2h.
- `main.py` loads `config.yaml` at **import time** — any script that imports `main` needs the file present and the CWD at repo root.

## Verify without credentials

The test suite stubs every external API (`tests/test_app.py` builds the full pipeline with mocks):

```bash
.venv/bin/python -m pytest -q
```

For pure-logic changes, `scoring.py` and `rotation.py` are import-safe with no config or network.
