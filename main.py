"""
Pitcher Streamer — FastAPI app.

Loads config from config.yaml (gitignored). Park factors must be populated
first by running: python refresh_park_factors.py
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import re

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from connectors import MlbStatsConnector, YahooConnector
from rotation import build_team_rotation, project_probable_pitchers
from scoring import compute_matchup_score

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path("config.yaml")
_PARK_FACTORS_PATH = Path("park_factors.json")

if not _CONFIG_PATH.exists():
    raise SystemExit(
        "ERROR: config.yaml not found.\n"
        "Copy config.yaml.example to config.yaml and fill in your values."
    )

with open(_CONFIG_PATH) as f:
    _config = yaml.safe_load(f)

_LEAGUE_ID: int = _config["league"]["id"]
_MY_TEAM_NAME: str = _config["league"]["my_team_name"]
_SEASON: int = _config["league"]["season"]


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not _PARK_FACTORS_PATH.exists():
        raise SystemExit(
            "ERROR: park_factors.json not found.\n"
            "Run: python refresh_park_factors.py"
        )
    pf = json.loads(_PARK_FACTORS_PATH.read_text())
    if pf.get("season") != _SEASON:
        raise SystemExit(
            f"ERROR: park_factors.json season={pf.get('season')} but config.yaml season={_SEASON}.\n"
            "Run: python refresh_park_factors.py"
        )
    app.state.park_factors = pf["teams"]
    app.state.pitcher_cache = {}  # {0: this_week_data, 1: next_week_data}
    logger.info("Loaded park factors for %d teams (season %d)", len(pf["teams"]), _SEASON)

    # Start background refresh thread — ei3
    refresh_thread = threading.Thread(
        target=_background_refresh, args=(app,), daemon=True, name="pitcher-refresh"
    )
    refresh_thread.start()
    logger.info("Background refresh thread started (interval=%ds)", _REFRESH_INTERVAL_SECONDS)

    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _week_range(offset: int = 0) -> tuple[date, date]:
    """Return (monday, sunday) for this week (offset=0) or next week (offset=1)."""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _make_yahoo_connector() -> YahooConnector:
    return YahooConnector(
        client_id=os.environ["YAHOO_CLIENT_ID"],
        client_secret=os.environ["YAHOO_CLIENT_SECRET"],
        league_id=_LEAGUE_ID,
        my_team_name=_MY_TEAM_NAME,
        oauth_cache_path=Path(os.environ.get("YAHOO_OAUTH_PATH", "oauth2.json")),
    )



def _savant_url(name: str, mlbam_id: "int | None") -> "str | None":
    if not mlbam_id:
        return None
    slug = name.lower()
    slug = re.sub(r"['\.]", "", slug)
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    return f"https://baseballsavant.mlb.com/savant-player/{slug}-{mlbam_id}"


def _days_since(game_date_str: str, reference: date) -> int:
    try:
        gd = date.fromisoformat(game_date_str)
        return (reference - gd).days
    except (ValueError, TypeError):
        return 999


def _park_factor_for_team(park_factors: dict, home_team_name: str) -> float:
    for key, pf in park_factors.items():
        if key.lower() in home_team_name.lower() or home_team_name.lower() in key.lower():
            return float(pf.get("index_runs", 100))
    return 100.0


def _is_starter(pitcher: dict, mlb: MlbStatsConnector, week_start: date, week_end: date) -> bool:
    """
    Return True if this pitcher has a verified start within 5 days of today
    (past or future). Uses game log for recent starts and the week schedule
    for upcoming probable starts.

    Ignores Yahoo position tags and roster slots entirely.
    """
    today = date.today()
    mlbam_id = pitcher.get("mlbam_id")
    if not mlbam_id:
        return False

    recent = mlb.fetch_pitcher_recent_starts(mlbam_id, _SEASON)
    for rs in recent:
        d = _days_since(rs["date"], today)
        if 0 <= d <= 5:
            return True

    # Upcoming: check if they appear as a probable pitcher in the week schedule
    # (handled via probable_pitcher_ids passed from the route)
    # Fall back: if they have any start in the last 30 days, assume they're a starter
    # and trust the schedule-based team filter to gate them.
    for rs in recent:
        d = _days_since(rs["date"], today)
        if 0 <= d <= 30:
            return True

    return False


def _score_pitcher_starts(
    pitcher: dict,
    starts: list[dict],
    mlb: MlbStatsConnector,
    park_factors: dict,
    splits_cache: dict,
    weather_cache: dict,
    recent_starts_cache: dict,
    season_stats_cache: dict,
) -> list[dict]:
    """
    Score each of this pitcher's starts for the week.
    Returns annotated start dicts with score, breakdown, flags, and probable indicator.
    """
    from scoring import _calc_fip
    today = date.today()
    mlbam_id = pitcher.get("mlbam_id")

    # Recent starts for familiarity + FIP — cached per mlbam_id
    if mlbam_id and mlbam_id not in recent_starts_cache:
        recent_starts_cache[mlbam_id] = mlb.fetch_pitcher_recent_starts(mlbam_id, _SEASON)
    recent_starts = recent_starts_cache.get(mlbam_id, [])

    # Season FIP from season stats cache
    season_stats = season_stats_cache.get(mlbam_id, {})
    season_fip = _calc_fip(
        season_stats.get("k", 0), season_stats.get("bb", 0),
        season_stats.get("hr", 0), season_stats.get("ip", 0.0),
    )
    # Pitcher K% from season strikeouts per 9 → approximate K% (K/9 ÷ ~4.3 batters faced per inning)
    pitcher_k_pct: "float | None" = None
    k_per_9 = season_stats.get("k_per_9", 0.0)
    if k_per_9:
        pitcher_k_pct = round(k_per_9 / 4.3 * 100 / 9, 1)

    # Recent FIP from last 20 starts in game log
    last_20 = sorted(recent_starts, key=lambda s: s["date"], reverse=True)[:20]
    if last_20 and all("ip" in s for s in last_20):
        r_k = sum(s["k"] for s in last_20)
        r_bb = sum(s["bb"] for s in last_20)
        r_hr = sum(s["hr"] for s in last_20)
        r_ip = sum(s["ip"] for s in last_20)
        recent_fip = _calc_fip(r_k, r_bb, r_hr, r_ip)
    else:
        recent_fip = None

    annotated = []
    for start in starts:
        opponent_id = start.get("opponent_id")
        opponent_name = start.get("opponent_name", "")
        home_away = start.get("home_away", "vs")
        game_date = start.get("date", "")
        game_pk = start.get("game_pk")
        home_team_name = start.get("home_team_name", "")
        is_probable = start.get("is_probable", False)
        is_projected = start.get("is_projected", False)
        if is_probable:
            confidence = "CONFIRMED"
        elif is_projected:
            confidence = "PROJECTED"
        else:
            confidence = "UNKNOWN"

        # Weather: cached per (venue_lat, venue_lng, game_date) — i06
        weather_temp = start.get("weather_temp")
        temp_f = weather_temp
        rain_pct = None

        if temp_f is None:
            venue_lat = start.get("venue_lat")
            venue_lng = start.get("venue_lng")
            game_datetime_str = start.get("game_datetime")
            if venue_lat and venue_lng and game_datetime_str:
                weather_key = (round(venue_lat, 3), round(venue_lng, 3), game_date)
                if weather_key not in weather_cache:
                    try:
                        gdt = datetime.fromisoformat(game_datetime_str.replace("Z", "+00:00"))
                        weather_cache[weather_key] = mlb.fetch_weather_forecast(venue_lat, venue_lng, gdt)
                    except Exception:
                        weather_cache[weather_key] = None
                forecast = weather_cache.get(weather_key)
                if forecast:
                    temp_f = forecast.get("temp_f")
                    rain_pct = forecast.get("rain_pct")

        # Batting splits — cached per (team_id, handedness) — i06
        handedness = pitcher.get("throws", "R")
        cache_key = (opponent_id, handedness)
        if cache_key not in splits_cache and opponent_id:
            splits_cache[cache_key] = mlb.fetch_team_batting_splits(opponent_id, handedness)
        splits = splits_cache.get(cache_key, {})

        opponent_ops = splits.get("ops", 0.720)
        opponent_k_pct = splits.get("k_pct") or 22.0

        # Familiarity
        days_since_faced = None
        for rs in recent_starts:
            if opponent_name and opponent_name.lower() in rs.get("opponent_name", "").lower():
                d = _days_since(rs["date"], today)
                if d <= 9:
                    days_since_faced = d
                    break

        park_index = _park_factor_for_team(park_factors, home_team_name)

        score, breakdown = compute_matchup_score(
            opponent_k_pct=opponent_k_pct,
            opponent_ops=opponent_ops,
            park_index_runs=park_index,
            days_since_faced=days_since_faced,
            temp_f=temp_f,
            rain_pct=rain_pct,
            pitcher_hand=handedness,
            pitcher_k_pct=pitcher_k_pct,
            season_fip=season_fip,
            recent_fip=recent_fip,
        )

        annotated.append({
            "date": game_date,
            "opponent": opponent_name,
            "home_away": home_away,
            "score": score,
            "breakdown": breakdown,
            "temp": temp_f,
            "rain_pct": rain_pct,
            "familiarity_flag": days_since_faced is not None and days_since_faced <= 9,
            "familiarity_days": days_since_faced,
            "park_index": park_index,
            "is_probable": is_probable,
            "is_projected": is_projected,
            "confidence": confidence,
            "game_pk": game_pk,
            "projection_reason": start.get("projection_reason", ""),
        })

    return annotated


def _projection_reason(is_probable: bool, is_projected: bool, proj_entry: "dict | None") -> str:
    """Human-readable explanation of why we believe this pitcher starts this game."""
    if is_probable:
        return "MLB confirmed probable pitcher for this game"
    if is_projected and proj_entry:
        last = proj_entry.get("last_start", "")
        days = proj_entry.get("days_since_last_start")
        slot = proj_entry.get("slot")
        next_elig = proj_entry.get("next_eligible", "")
        parts = ["Rotation math"]
        if last:
            parts.append(f"last started {last}")
        if days is not None:
            parts.append(f"{days}d ago")
        if slot:
            parts.append(f"slot {slot} in 5-man rotation")
        if next_elig:
            parts.append(f"next eligible {next_elig}")
        return " · ".join(parts)
    return "No starter announced"


def _extract_pitcher_starts(
    pitcher_team_id: int,
    schedule: list[dict],
    probable_home: dict[int, int],
    probable_away: dict[int, int],
    pitcher_mlbam_id: "int | None",
    projected_home: "dict[int, dict] | None" = None,
    projected_away: "dict[int, dict] | None" = None,
) -> list[dict]:
    """
    Extract games this week where this pitcher is the confirmed probable or
    the rotation-projected starter.

    Games where another pitcher is confirmed OR projected for a slot are excluded.
    is_probable=True  → MLB confirmed probable
    is_projected=True → rotation math projection (PROJECTED tier, not yet confirmed)
    """
    projected_home = projected_home or {}
    projected_away = projected_away or {}
    starts = []
    for game in schedule:
        home_id = game.get("home_team_id")
        away_id = game.get("away_team_id")
        pk = game.get("game_pk")

        if home_id == pitcher_team_id:
            confirmed_id = probable_home.get(pk)
            proj_entry = projected_home.get(pk)
            projected_id = proj_entry["pitcher_id"] if proj_entry else None
            is_probable = pitcher_mlbam_id is not None and confirmed_id == pitcher_mlbam_id
            is_projected = pitcher_mlbam_id is not None and not confirmed_id and projected_id == pitcher_mlbam_id
            if not is_probable and not is_projected:
                continue
            starts.append({
                "date": game["date"],
                "game_pk": pk,
                "opponent_id": away_id,
                "opponent_name": game.get("away_team", ""),
                "home_away": "vs",
                "home_team_name": game.get("home_team", ""),
                "weather_temp": game.get("weather_temp"),
                "venue_lat": game.get("venue_lat"),
                "venue_lng": game.get("venue_lng"),
                "game_datetime": game.get("game_datetime"),
                "is_probable": is_probable,
                "is_projected": is_projected,
                "projection_reason": _projection_reason(is_probable, is_projected, proj_entry if is_projected else None),
            })
        elif away_id == pitcher_team_id:
            confirmed_id = probable_away.get(pk)
            proj_entry = projected_away.get(pk)
            projected_id = proj_entry["pitcher_id"] if proj_entry else None
            is_probable = pitcher_mlbam_id is not None and confirmed_id == pitcher_mlbam_id
            is_projected = pitcher_mlbam_id is not None and not confirmed_id and projected_id == pitcher_mlbam_id
            if not is_probable and not is_projected:
                continue
            starts.append({
                "date": game["date"],
                "game_pk": pk,
                "opponent_id": home_id,
                "opponent_name": game.get("home_team", ""),
                "home_away": "@",
                "home_team_name": game.get("home_team", ""),
                "weather_temp": game.get("weather_temp"),
                "venue_lat": game.get("venue_lat"),
                "venue_lng": game.get("venue_lng"),
                "game_datetime": game.get("game_datetime"),
                "is_probable": is_probable,
                "is_projected": is_projected,
                "projection_reason": _projection_reason(is_probable, is_projected, proj_entry if is_projected else None),
            })
    return starts



# ---------------------------------------------------------------------------
# Data fetch (shared by background refresh and on-demand route)
# ---------------------------------------------------------------------------


def _build_pitcher_data(park_factors: dict, week_offset: int = 0) -> dict:
    """
    Fetch and annotate pitcher data for the given week.
    week_offset=0 → this week, week_offset=1 → next week.
    Returns the template context dict.
    """
    week_start, week_end = _week_range(week_offset)
    today = date.today()

    yahoo = _make_yahoo_connector()
    mlb = MlbStatsConnector()

    # Phase 1: schedule + team IDs + Yahoo roster + Yahoo FA + transactions — all in parallel
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_schedule = ex.submit(mlb.fetch_schedule_with_venue, week_start, week_end)
        f_teams = ex.submit(mlb.fetch_mlb_team_ids)
        f_roster = ex.submit(yahoo.get_my_team_roster)
        f_fa = ex.submit(yahoo.get_free_agents, "A")
        f_txns = ex.submit(mlb.fetch_recent_transactions)

    schedule = f_schedule.result()
    abbr_to_id, id_to_abbr = (
        lambda teams: (
            {t["abbreviation"].upper(): t["team_id"] for t in teams},
            {t["team_id"]: t["abbreviation"] for t in teams},
        )
    )(f_teams.result())
    all_roster = f_roster.result()
    all_fa = f_fa.result()
    unavailable_ids: set[int] = f_txns.result()

    # Annotate schedule with abbreviations
    for game in schedule:
        game["home_abbr"] = id_to_abbr.get(game.get("home_team_id"), "")
        game["away_abbr"] = id_to_abbr.get(game.get("away_team_id"), "")

    # Extract probable pitcher maps: {game_pk: mlbam_id} for home and away — e9k
    probable_home: dict[int, int] = {}
    probable_away: dict[int, int] = {}
    for game in schedule:
        pk = game.get("game_pk")
        if not pk:
            continue
        hp = game.get("probable_home_id")
        ap = game.get("probable_away_id")
        if hp:
            probable_home[pk] = hp
        if ap:
            probable_away[pk] = ap

    # Teams with a game this week — keyed by team_id — 2vg
    teams_playing: set[int] = set()
    for game in schedule:
        if game.get("home_team_id"):
            teams_playing.add(game["home_team_id"])
        if game.get("away_team_id"):
            teams_playing.add(game["away_team_id"])

    # Normalize team_id onto all roster/FA players
    for p in all_roster:
        p["team_id"] = abbr_to_id.get(p.get("team_abbr", "").upper())
    roster_candidates = [p for p in all_roster if p.get("team_id") in teams_playing]

    logger.info("FA raw count: %d", len(all_fa))
    for p in all_fa:
        abbr = p.get("team_abbr", "")
        p["team_id"] = abbr_to_id.get(abbr.upper())
        if p["team_id"] is None:
            logger.debug("FA team_id miss: name=%r abbr=%r", p.get("name"), abbr)
    fa_candidates = [p for p in all_fa if p.get("team_id") in teams_playing]
    logger.info("FA after team filter: %d (teams_playing has %d)", len(fa_candidates), len(teams_playing))

    # Pre-filter FA candidates: SP-eligible and owned by at least 1% of leagues.
    # Yahoo returns ~600 pitchers; 0%-owned players are almost never streamable SPs.
    # This cuts the game-log fetch pool from ~150 to ~80 without extra API calls.
    # The starter gate (last start within 30 days) then does the final cut.
    fa_candidates_filtered = [
        p for p in fa_candidates
        if "SP" in p.get("position", "")
        and (p.get("percent_owned") or 0) >= 1
    ]
    logger.info("FA after SP+owned filter: %d (from %d team-filtered)", len(fa_candidates_filtered), len(fa_candidates))

    all_candidates = roster_candidates + fa_candidates_filtered

    # Phase 2: resolve mlbam_id for candidates missing it (roster players + probable FAs by name)
    names_to_resolve = [
        p.get("name", "") for p in all_candidates if not p.get("mlbam_id") and p.get("name")
    ]
    unique_names = list(dict.fromkeys(names_to_resolve))
    if unique_names:
        with ThreadPoolExecutor(max_workers=min(len(unique_names), 10)) as ex:
            futs = {ex.submit(mlb.resolve_player_mlbam_id, name): name for name in unique_names}
            mlbam_cache: dict[str, dict | None] = {}
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    mlbam_cache[name] = fut.result()
                except Exception:
                    mlbam_cache[name] = None
    else:
        mlbam_cache = {}

    for p in all_candidates:
        if p.get("mlbam_id"):
            continue
        name = p.get("name", "")
        info = mlbam_cache.get(name)
        if info:
            p["mlbam_id"] = info["mlbam_id"]
            if not p.get("throws"):
                p["throws"] = info["throws"]
        else:
            logger.debug("mlbam_id resolution failed for %r", name)

    # Phase 3: prefetch game logs + season stats for all candidates in parallel
    recent_starts_cache: dict = {}   # mlbam_id → [{date, opponent_name, ip, k, bb, hr}]
    season_stats_cache: dict = {}    # mlbam_id → {ip, k, bb, hr, k_per_9}
    mlbam_ids_to_fetch = [p["mlbam_id"] for p in all_candidates if p.get("mlbam_id")]
    unique_mlbam_ids = list(dict.fromkeys(mlbam_ids_to_fetch))
    if unique_mlbam_ids:
        with ThreadPoolExecutor(max_workers=min(len(unique_mlbam_ids) * 2, 20)) as ex:
            log_futs = {ex.submit(mlb.fetch_pitcher_recent_starts, mid, _SEASON): mid for mid in unique_mlbam_ids}
            stat_futs = {ex.submit(mlb.fetch_pitcher_season_stats, mid, _SEASON): mid for mid in unique_mlbam_ids}
            for fut in as_completed(list(log_futs) + list(stat_futs)):
                if fut in log_futs:
                    mid = log_futs[fut]
                    try:
                        recent_starts_cache[mid] = fut.result()
                    except Exception:
                        recent_starts_cache[mid] = []
                else:
                    mid = stat_futs[fut]
                    try:
                        season_stats_cache[mid] = fut.result()
                    except Exception:
                        season_stats_cache[mid] = {}

    # Phase 4: prefetch batting splits + weather for all games in parallel
    splits_cache: dict = {}
    weather_cache: dict = {}

    # Collect unique (opponent_id, handedness) pairs from confirmed starters' schedule
    confirmed_candidates = [
        p for p in all_candidates
        if p.get("mlbam_id") and any(
            0 <= _days_since(rs["date"], today) <= 30
            for rs in recent_starts_cache.get(p["mlbam_id"], [])
        )
    ]

    opponent_handedness_pairs: set[tuple[int, str]] = set()
    weather_keys: set[tuple] = set()
    for p in confirmed_candidates:
        team_id = p.get("team_id")
        handedness = p.get("throws", "R")
        starts_ctx = _extract_pitcher_starts(team_id, schedule, probable_home, probable_away, p.get("mlbam_id"))
        for s in starts_ctx:
            opp_id = s.get("opponent_id")
            if opp_id:
                opponent_handedness_pairs.add((opp_id, handedness))
            if s.get("weather_temp") is None and s.get("venue_lat") and s.get("venue_lng") and s.get("game_datetime"):
                weather_keys.add((round(s["venue_lat"], 3), round(s["venue_lng"], 3), s["date"], s["game_datetime"]))

    n_phase4 = len(opponent_handedness_pairs) + len(weather_keys)
    with ThreadPoolExecutor(max_workers=min(max(n_phase4, 1), 20)) as ex:
        splits_futs = {
            ex.submit(mlb.fetch_team_batting_splits, opp_id, hand): (opp_id, hand)
            for opp_id, hand in opponent_handedness_pairs
        }
        weather_futs = {}
        for lat, lng, game_date, game_datetime_str in weather_keys:
            try:
                gdt = datetime.fromisoformat(game_datetime_str.replace("Z", "+00:00"))
                weather_futs[ex.submit(mlb.fetch_weather_forecast, lat, lng, gdt)] = (lat, lng, game_date)
            except Exception:
                pass

        for fut in as_completed(list(splits_futs) + list(weather_futs)):
            if fut in splits_futs:
                key = splits_futs[fut]
                try:
                    splits_cache[key] = fut.result()
                except Exception:
                    splits_cache[key] = {}
            else:
                key = weather_futs[fut]
                try:
                    weather_cache[key] = fut.result()
                except Exception:
                    weather_cache[key] = None

    # Phase 5: project unconfirmed starters via rotation math — 2t1/aai
    # Build a rotation per team from the candidates who passed the starter gate,
    # then project who likely starts each open slot this week.
    # Projections are injected into probable_home/probable_away so _extract_pitcher_starts
    # picks them up with is_probable=False (they stay as open slots, but the pitcher
    # will see the game in their list instead of another pitcher blocking it).
    confirmed_ids: set[int] = set(probable_home.values()) | set(probable_away.values())
    confirmed_starter_candidates = [
        p for p in all_candidates
        if p.get("mlbam_id") and any(
            0 <= _days_since(rs["date"], today) <= 30
            for rs in recent_starts_cache.get(p["mlbam_id"], [])
        )
    ]
    team_ids_this_week = {p["team_id"] for p in confirmed_starter_candidates if p.get("team_id")}
    rotation_cache: dict[int, list[dict]] = {}
    projected_home: dict[int, dict] = {}   # {game_pk: projection entry}  PROJECTED only
    projected_away: dict[int, dict] = {}

    for tid in team_ids_this_week:
        rotation = build_team_rotation(tid, confirmed_starter_candidates, recent_starts_cache, today)
        rotation_cache[tid] = rotation
        if not rotation:
            continue

        team_games = [g for g in schedule if g.get("home_team_id") == tid or g.get("away_team_id") == tid]
        projections = project_probable_pitchers(tid, team_games, rotation, confirmed_ids, today, unavailable_ids)
        for game_pk, proj in projections.items():
            game = next((g for g in schedule if g.get("game_pk") == game_pk), None)
            if not game:
                continue
            if game.get("home_team_id") == tid:
                if game_pk not in probable_home:
                    projected_home[game_pk] = proj
            elif game.get("away_team_id") == tid:
                if game_pk not in probable_away:
                    projected_away[game_pk] = proj

    def annotate_and_filter_starters(pitchers: list[dict]) -> list[dict]:
        """
        Score each candidate's weekly starts, then keep only pitchers who
        have at least one recent start in the last 30 days (confirmed starter).
        Ignores Yahoo position/slot entirely — erm.
        Game logs and splits are pre-populated in caches by Phase 3/4.
        """
        result = []
        for p in pitchers:
            team_id = p.get("team_id")
            mlbam_id = p.get("mlbam_id")

            recent = recent_starts_cache.get(mlbam_id, [])

            # Confirm this is actually a starter: must have a start within 30 days — erm
            is_confirmed_starter = any(
                0 <= _days_since(rs["date"], today) <= 30
                for rs in recent
            )
            if not is_confirmed_starter:
                logger.debug(
                    "Starter gate: excluded %r (mlbam_id=%s, recent_starts=%d)",
                    p.get("name"), mlbam_id, len(recent),
                )
                continue

            starts_ctx = _extract_pitcher_starts(
                team_id, schedule, probable_home, probable_away, mlbam_id,
                projected_home, projected_away,
            )
            scored_starts = _score_pitcher_starts(
                p, starts_ctx, mlb, park_factors,
                splits_cache, weather_cache, recent_starts_cache, season_stats_cache,
            )
            season_stats = season_stats_cache.get(mlbam_id, {})
            bf = season_stats.get("bf", 0)
            k = season_stats.get("k", 0)
            bb = season_stats.get("bb", 0)
            if bf > 0:
                k_pct = round(k / bf * 100, 1)
                bb_pct = round(bb / bf * 100, 1)
                k_minus_bb = round(k_pct - bb_pct, 1)
            else:
                k_pct = bb_pct = k_minus_bb = None
            result.append({
                **p,
                "starts": scored_starts,
                "start_count": len(scored_starts),
                "score_sum": round(sum(s["score"] for s in scored_starts), 1),
                "k_pct": k_pct,
                "bb_pct": bb_pct,
                "k_minus_bb": k_minus_bb,
                "savant_url": _savant_url(p.get("name", ""), mlbam_id),
            })
        return result

    roster_annotated = annotate_and_filter_starters(roster_candidates)
    roster_annotated.sort(key=lambda p: (-p["start_count"], -p["score_sum"]))

    waiver_annotated = annotate_and_filter_starters(fa_candidates_filtered)
    waiver_annotated.sort(key=lambda p: (-p["start_count"], -p["score_sum"]))

    return {
        "week_start": week_start,
        "week_end": week_end,
        "roster_pitchers": roster_annotated,
        "waiver_pitchers": waiver_annotated,
        "fetched_at": datetime.now(),
    }


# ---------------------------------------------------------------------------
# Background refresh — ei3
# ---------------------------------------------------------------------------

_REFRESH_INTERVAL_SECONDS = 2 * 60 * 60  # 2 hours


def _background_refresh(app_ref) -> None:
    """
    Background thread: refresh both this week and next week every 2 hours.
    """
    while True:
        for offset in (0, 1):
            try:
                logger.info("Background refresh: week_offset=%d", offset)
                data = _build_pitcher_data(app_ref.state.park_factors, offset)
                app_ref.state.pitcher_cache[offset] = data
                logger.info("Background refresh done: offset=%d roster=%d waiver=%d",
                            offset, len(data["roster_pitchers"]), len(data["waiver_pitchers"]))
            except Exception:
                logger.exception("Background refresh failed: week_offset=%d", offset)
        time.sleep(_REFRESH_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _render_index(request: Request, data: dict, week_offset: int) -> str:
    this_monday, _ = _week_range(0)
    next_monday, _ = _week_range(1)
    return templates.get_template("index.html").render(
        {
            "request": request,
            "week_start": data["week_start"],
            "week_end": data["week_end"],
            "roster_pitchers": data["roster_pitchers"],
            "waiver_pitchers": data["waiver_pitchers"],
            "fetched_at": data.get("fetched_at"),
            "week_offset": week_offset,
            "this_week_label": f"This week ({this_monday.strftime('%b %-d')})",
            "next_week_label": f"Next week ({next_monday.strftime('%b %-d')})",
        }
    )


# Minimal HTML shell flushed immediately on cold starts so the browser can
# render the loading bar animation while the server fetches data.
_LOADING_SHELL = """\
<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pitcher Streamer</title>
<style>
#loading-bar{position:fixed;top:0;left:0;height:3px;width:0;
background:linear-gradient(90deg,#28a745,#4fc3f7);z-index:9999;
animation:loading-grow 8s cubic-bezier(0.1,0.4,0.6,1) forwards}
#loading-bar.done{transition:width .15s ease,opacity .4s ease .15s;opacity:0}
@keyframes loading-grow{0%{width:0}30%{width:55%}60%{width:75%}85%{width:88%}100%{width:95%}}
body{font-family:system-ui,sans-serif;font-size:14px;padding:1rem 2rem;background:#f5f5f5;color:#222}
</style></head><body>
<div id="loading-bar"></div>
<p style="color:#666;margin-top:2rem">Loading pitcher data…</p>
"""


@app.get("/")
async def index(request: Request, week: int = 0):
    import asyncio

    app_state = request.app.state
    week_offset = max(0, min(week, 1))

    cache = getattr(app_state, "pitcher_cache", {})
    data = cache.get(week_offset)

    if data is not None:
        # Cache hit — render immediately, no streaming needed
        return HTMLResponse(_render_index(request, data, week_offset))

    # Cold start — stream the loading shell first, then fetch + render
    logger.info("Cold start: fetching week_offset=%d (streaming)", week_offset)

    async def _stream():
        yield _LOADING_SHELL
        # Fetch in a thread so the event loop stays unblocked
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, _build_pitcher_data, app_state.park_factors, week_offset
        )
        app_state.pitcher_cache[week_offset] = data
        # Replace the shell with the full rendered page via a JS redirect trick:
        # close the partial body and emit a script that replaces the document.
        full_html = _render_index(request, data, week_offset)
        escaped = full_html.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
        yield f"<script>document.open();document.write(`{escaped}`);document.close();</script>"

    return StreamingResponse(_stream(), media_type="text/html")
