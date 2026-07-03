---
name: season-rollover
description: Annual maintenance checklist for a new MLB season (or when startup fails the park-factors season guard). Use at the start of a season, when config.yaml season changes, or when scores look systematically off.
---

# Season rollover checklist

The app hard-fails at startup if `park_factors.json` season ≠ `config.yaml` season — that guard usually triggers this checklist.

1. **`config.yaml`** — set `league.season` to the new year; update `league.id` (Yahoo assigns a new league id every season) and `my_team_name` if renamed. A wrong `my_team_name` now raises at fetch time listing available teams.
2. **`.envrc`** — set `BASEBALL_SAVANT_YEAR` to the new year, `direnv allow`.
3. **Park factors** — `python refresh_park_factors.py`. Verify the output: expect ~29–30 teams; keys are Savant nicknames ("D-backs"). If a team is missing (Athletics were absent in 2026), either hand-add an entry or accept the logged neutral-100 fallback.
4. **`scoring.py` calibration** (all hardcoded):
   - `_FIP_CONSTANT` (3.10 in 2026) — set so league FIP ≈ league ERA; look up the season's constant or derive from early-season league totals.
   - Population means/σ: `_MEAN_FIP`, `_MEAN_K_PCT`, `_MEAN_OPS` drift year to year (league K% especially). The offense z-score centers on `_MEAN_K_PCT`; see issue pitcher-streamer-42a about the Log5 formula's neutral point before touching it.
5. **Yahoo OAuth** — token cache usually survives; if the league id changed, the connector re-resolves the league key automatically. Delete `oauth2.json` only if auth loops.
6. **Sanity pass** — `pytest -q` (fixtures pin season 2026 — bump fixture dates/season if the year is asserted anywhere), then launch and confirm: scores center near 50, no `No park factor entry matches` warnings in logs beyond known gaps, transactions filter still matches live typeCodes (`curl /api/v1/transactions` and spot-check — see the mlb-data-apis skill).
