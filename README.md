# pitcher-streamer

A local web app for pitcher streaming decisions in Yahoo Fantasy Baseball. Shows your rostered SPs and available waiver wire SPs, annotated with start counts (1 or 2 start week), K%-BB%, matchup scores (see below), and a link to their Baseball Savant page for convenience.

Built using Claude Code and [beads](https://github.com/gastownhall/beads).

## What it does

**My Roster** — your rostered pitchers who have at least one start this week, each start scored and annotated.

**Waiver Wire** — available SPs with a start this week, sorted by start count then total score. Works with both free-agent and waiver-wire league formats.

Both panels support **this week / next week** switching via a dropdown. All columns are sortable. The Starts count is clickable to see each individual game and the basis for the projection.

Each start shows:
- Date, opponent, and home/away indicator
- Matchup score (50 = neutral; higher = better for the pitcher) — click to expand the full factor breakdown
- Temperature and rain flag (⛈ if precipitation probability > 50%)
- ⚠ familiarity flag if the pitcher faced this team in the last 9 days
- Start confidence: ★ confirmed · ~ projected · ? unconfirmed

## Matchup score

Scores are centered at 50 (league-average matchup) and clamped to [10, 90]. Green ≥ 60 · Yellow 45–59 · Red < 45.

### Weighted components

The four factor weights come from the multiple regression analysis in Rudy Gamble's [Stream-o-Nator](https://razzball.com/launch-of-stream-o-nator-the-fantasy-baseball-pitcher-streaming-tool/) article on Razzball, which regressed park factor, pitcher FIP, opponent offense, and recent FIP against historical ERA/WHIP:

| Weight | Factor | Original metric | This implementation |
|--------|--------|-----------------|---------------------|
| 39% | Park environment | Park run index | Park run factor index (Baseball Savant) |
| 31% | Pitcher season skill | Season FIP index | Season FIP |
| 17% | Opposing offense | Opponent runs/game (park-adjusted) | Log5 expected K% vs pitcher's hand |
| 14% | Pitcher recent skill | FIP index over last 20 starts | FIP over last 20 starts |

The opposing offense metric is the main divergence: the original uses opponent runs/game adjusted for their home park, while this implementation uses Log5 expected K% from MLB Stats API batting splits by handedness — a more direct measure of the pitcher-lineup matchup.

### Z-score normalization

Each input is normalized before weights are applied:

```
component_delta = weight × ((mean - value) / σ) × scale
```

This ensures the weights reflect true relative contribution to score variance — a 39% park weight means park factor actually drives 39% of the score spread, regardless of each input's raw numeric range. Without normalization, a factor with a large raw range (like FIP) would dominate over one with a small range (like park index 85–115) even if its weight is lower.

Population parameters used:

| Factor | Mean | σ | Direction |
|--------|------|---|-----------|
| Park index | 100 | 8 | lower = pitcher-friendly |
| Season FIP | 4.10 | 0.60 | lower = better |
| Recent FIP | 4.10 | 0.80 | lower = better |
| Log5 K% | 22% | 4% | higher = better |

Scale factor = 15, so a 1σ difference in the top factor (park) moves the score by 0.39 × 15 ≈ 5.9 points. Typical realistic range: 35–70.

### FIP

Fielding Independent Pitching — calculated from game log data (K, BB, HR, IP):

```
FIP = (13 × HR + 3 × BB − 2 × K) / IP + 3.10
```

Season FIP uses the full season game log. Recent FIP uses the last 20 starts. If insufficient data exists (e.g. a pitcher returning from injury), that component contributes 0 (league average assumed).

### Log5 expected strikeout rate

For the opposing offense component, the Log5 method predicts the expected K% for this specific pitcher-vs-lineup matchup, accounting for the interaction between pitcher and lineup rather than treating them independently:

```
E[K%] = (B × P) / (0.84 × B × P + 0.16)
```

**B** = opposing team K% vs this pitcher's handedness (L or R), sourced from MLB Stats API batting splits. **P** = pitcher's season K% derived from strikeouts per 9 innings.

When pitcher K% is unavailable, falls back to opponent OPS vs handedness.

### Situational adjustments

Applied as flat modifiers after the weighted score (not part of the regression):

```
- 8  if faced this team 6–9 days ago   (lineup has recent tape)
- 12 if faced this team ≤ 5 days ago
+ 5  if temperature < 55°F             (cold air = less carry on batted balls)
- 5  if temperature > 82°F             (ball carries further, more HRs)
- 15 if rain probability > 50%         (start cut short mid-game)
```

Weather is sourced from Open-Meteo hourly forecasts for the venue coordinates. Temperature uses the first-pitch hour. Rain probability is the **maximum across hours 1–2 after first pitch** (skipping first pitch itself, which would cause a postponement rather than a shortened start, and skipping hour 3+ when the starter is typically out of the game anyway).

## Game Score (past starts)

For starts that have already happened, clicking the score badge shows the pitcher's game line and a **Game Score** — a process-oriented single-game quality metric that ignores hits and runs in favour of Statcast contact quality.

```
Base 40
+ 2 × outs recorded
+ 2 × strikeouts
− 3 × walks
− 5 × barrels allowed        (launch angle 26–50°, EV ≥ 98 mph)
− 1 × hard-hit balls allowed  (EV ≥ 95 mph)
+ 5 if whiff% > 25%
+ 5 if chase% > 30%
```

Clamped to [0, 100]. A pitcher who allows 8 runs on bad-luck contact but records 17 outs, 8 strikeouts, 1 barrel, and elite whiff/chase rates scores ~87 — correctly identifying a good start hidden behind unlucky results.

Statcast stats sourced from Baseball Savant `/gf` endpoint per game. Also shown: CSW% (called strike + whiff rate — gold standard for single-game pitch quality), F-Strike%, Zone-Contact%, hard-hit%, avg EV, barrels, swords, and avg fastball velo.

### Start confidence

Each start is tagged based on how the assignment was determined:

- **★ CONFIRMED** — MLB's official probable pitcher for that game
- **~ PROJECTED** — Rotation math: 5-day cadence with off-day shifting, anchored to recent game log starts, filtered against recent IL/DFA transactions. Pitchers confirmed for an earlier game this week can be projected for a second start.
- **? UNCONFIRMED** — No confirmed or projected starter determined

## Setup

### 1. Prerequisites

- Python 3.11+
- [direnv](https://direnv.net/) (optional but recommended)
- Yahoo Fantasy account with API credentials ([get them here](https://developer.yahoo.com/apps/))

### 2. Clone and install

```bash
git clone <this repo>
cd pitcher-streamer
python -m venv .venv
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your league details:

```yaml
league:
  id: 1234          # Your Yahoo league ID
  name: "My League"
  my_team_name: "My Team Name"
  season: 2026
```

### 4. Set credentials

Copy `.envrc` to fill in your Yahoo API credentials, or export them manually:

```bash
export YAHOO_CLIENT_ID=your_client_id
export YAHOO_CLIENT_SECRET=your_client_secret
export YAHOO_OAUTH_PATH="$(pwd)/oauth2.json"
export BASEBALL_SAVANT_YEAR="2026"
```

If using direnv: `direnv allow`.

### 5. Fetch park factors (once per season)

```bash
python refresh_park_factors.py
```

This writes `park_factors.json`. Re-run at the start of each season. The app will refuse to start if this file is missing or out of date.

### 6. OAuth (first run only)

```bash
cp oauth2.json.example oauth2.json
```

Fill in `consumer_key` and `consumer_secret` from your Yahoo app credentials, then run the app. It will open a browser for Yahoo OAuth authorization. After authorizing, Yahoo redirects to `https://localhost` — that URL won't load, which is expected. Copy the `code=` value from the URL and paste it when prompted. The token is saved to `oauth2.json` and refreshed automatically.

### 7. Run

```bash
uvicorn main:app --reload --port 8001
```

Open [http://localhost:8001](http://localhost:8001).

Data is fetched fresh on first load, then refreshed in the background every 2 hours. API responses are cached in-process (30 min for schedule/weather, up to 6 hours for splits and game logs) to avoid redundant calls during refresh.

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, route logic, parallel data pipeline, background refresh |
| `connectors.py` | Yahoo Fantasy and MLB Stats API connectors, in-process TTL cache |
| `scoring.py` | `compute_matchup_score()` — Stream-o-Nator weights + Log5 K% + z-score normalization |
| `rotation.py` | Team rotation builder and projection engine |
| `refresh_park_factors.py` | One-shot script to fetch Baseball Savant park factors |
| `config.yaml` | Your league config |
| `config.yaml.example` | Template to copy from |
| `park_factors.json` | Cached park factors (written by refresh script) |
| `oauth2.json` | Yahoo OAuth token (written on first run) |
| `oauth2.json.example` | Template showing the expected token file structure |

## Tests

```bash
pytest tests/ -v
```

All external HTTP calls are mocked; no credentials needed to run tests.
