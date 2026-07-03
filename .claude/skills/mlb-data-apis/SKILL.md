---
name: mlb-data-apis
description: Hard-won reference for the external data sources this app depends on — MLB Stats API, Baseball Savant, FanGraphs, Open-Meteo, Yahoo Fantasy. Use before adding/altering any connector code, debugging empty API results, or interpreting typeCodes, sitCodes, hydrate params, or team-name/abbreviation mismatches.
---

# External data APIs — verified quirks

All request code lives in `connectors.py` (plus `refresh_park_factors.py`). Everything below was verified against live responses; re-verify with `curl` before assuming a payload shape changed.

## MLB Stats API (`statsapi.mlb.com/api/v1`) — no auth, no rate limit observed

- **Schedule**: `/schedule?sportId=1&hydrate=venue(location),weather,probablePitcher(note)` — probables, venue coords, and game-time weather come from the hydrate. `gameType=R` filters to regular season.
- **Transactions** (`/transactions?startDate=&endDate=`) — the trap: IL moves are **not** their own typeCode. Verified codes:
  - `SC` "Status Change" = all list moves (IL, restricted, paternity, bereavement). Direction is only in `description`: "placed … on the X-day injured list" / "transferred … to the 60-day" vs "activated … from …".
  - `OPT` optioned, `CU` recalled, `DES` designated for assignment, `OUT` outrighted, `REL` released, `TR` trade, `SFA`/`DFA` = signed/**declared** free agency (DFA ≠ designated-for-assignment!).
  - Process chronologically: a placement followed by an activation inside the window means *available*.
- **Batting splits**: `/teams/{id}/stats?stats=statSplits&group=hitting&sitCodes=vl|vr` — OPS/K% vs handedness. wOBA is NOT available here (only from FanGraphs).
- **Game logs**: `/people/{id}/stats?stats=gameLog&group=pitching` — filter rows on `stat.gamesStarted` to isolate starts. `inningsPitched` is a string like `"6.1"` (thirds, not tenths) — parse with `_parse_ip`.
- **Player search**: `/people/search?names=X` returns loose matches; `people[0]` may be a same-named batter. No pitcher filter is applied today (known limitation, issue pitcher-streamer-fk7).

## Baseball Savant

- **Per-game pitch data**: `GET /gf?game_pk=N` — undocumented, multi-MB JSON covering the whole game. Pitcher pitches live under `home_pitchers`/`away_pitchers` keyed by **string** mlbam id. Needs a browser User-Agent. Cache the raw payload per game_pk (two pitchers in one game share it).
- **Park factors**: no JSON endpoint — `refresh_park_factors.py` regex-extracts `var data = [...]` from the leaderboard HTML. Keys are club **nicknames** (`name_display_club`): "D-backs", "Red Sox"… As of 2026 only **29 teams** — no Athletics row (temporary Sacramento venue). `main._park_factor_for_team` bridges "D-backs"→"Diamondbacks" via `_PARK_KEY_ALIASES` and warns on any neutral-100 fallback.

## FanGraphs (team wOBA — only clean source)

- `GET https://www.fangraphs.com/api/leaders/major-league/data` with `team=0,ts&stats=bat&qual=0&sortstat=wOBA&season=season1=YYYY`. Cloudflare blocks default UAs: send a browser `User-Agent` **and** `Referer: https://www.fangraphs.com/leaders/major-league`. Omitting `sortstat` returns an empty response.
- Abbreviation mismatches vs MLB (map in `_FG_TO_MLB_ABB`): ARI→AZ, CHW→CWS, KCR→KC, SDP→SD, SFG→SF, TBR→TB, WSN→WSH.

## Open-Meteo (weather, free tier)

- **Rate-limited**: parallel calls draw 429s — fetch sequentially (~200ms each; the app does this deliberately in Phase 4).
- `timezone=auto` returns hourly times in **venue-local** time; convert the UTC game time using `utc_offset_seconds` from the response before matching an hourly slot.
- `forecast_days=7` today, so next-week games >7 days out silently get no weather (Open-Meteo supports 16 — issue pitcher-streamer-fk7).

## Yahoo Fantasy (via yahoo_oauth + yahoo_fantasy_api)

- `lg.free_agents()` is hardcoded to status `FA` and returns nothing in waiver-wire leagues — use `lg._fetch_players("A", ...)` (status A = FA + waivers), as `YahooConnector.get_free_agents` does.
- `yahoo_oauth` corrupts `oauth2.json` two known ways (stray trailing `}`, zero-byte truncation); `_heal_oauth_json` repairs or deletes it. It also drops consumer creds — `_inject_consumer_creds` restores them.
- First-ever auth is an interactive browser flow; there is no headless path.
