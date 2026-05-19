"""
Yahoo Fantasy and MLB Stats API connectors for pitcher-streamer.

YahooConnector: OAuth + roster + free agents (copied from sportsball-bot).
MlbStatsConnector: schedule, team IDs, batting splits, game logs, weather.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TTL cache — shared across all MlbStatsConnector instances
# ---------------------------------------------------------------------------

class _TTLCache:
    """Thread-safe in-process cache with per-entry TTLs."""

    def __init__(self) -> None:
        self._store: dict = {}   # key → (value, expires_at)
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return _MISS
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return _MISS
            return value

    def set(self, key, value, ttl_seconds: int) -> None:
        with self._lock:
            self._store[key] = (value, time.monotonic() + ttl_seconds)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_MISS = object()  # sentinel — distinct from None so cached None is valid
_cache = _TTLCache()

_TTL_SCHEDULE    = 30 * 60        # 30 min — probables announced throughout day
_TTL_TEAM_IDS    = 24 * 60 * 60   # 24 h   — stable all season
_TTL_SPLITS      = 6  * 60 * 60   # 6 h    — updates after games finish
_TTL_GAME_LOG    = 4  * 60 * 60   # 4 h    — one update per day after game
_TTL_SEASON_STAT = 4  * 60 * 60   # 4 h
_TTL_TRANSACTIONS = 60 * 60       # 1 h
_TTL_WEATHER     = 30 * 60        # 30 min — hourly forecast
_TTL_PLAYER_ID   = 24 * 60 * 60   # 24 h   — stable all season

_MLB_GAME_CODE = "mlb"
_BASE_URL = "https://statsapi.mlb.com/api/v1"
_SPORT_ID = 1


# ---------------------------------------------------------------------------
# MLB HTTP client
# ---------------------------------------------------------------------------


class MlbHttpClient(Protocol):
    def get(self, url: str, params: Optional[dict] = None, timeout: int = 30) -> "MlbHttpResponse":
        ...


class MlbHttpResponse(Protocol):
    def raise_for_status(self) -> None: ...
    def json(self) -> dict: ...


class RealMlbHttpClient:
    def get(self, url: str, params: Optional[dict] = None, timeout: int = 30):
        import httpx
        return httpx.get(url, params=params or {}, timeout=timeout)


# ---------------------------------------------------------------------------
# MLB Stats connector
# ---------------------------------------------------------------------------


def _parse_ip(ip_str: str) -> float:
    """Convert MLB innings pitched string (e.g. '6.1') to decimal innings."""
    try:
        parts = str(ip_str).split(".")
        full = int(parts[0])
        thirds = int(parts[1]) if len(parts) > 1 else 0
        return full + thirds / 3
    except (ValueError, IndexError):
        return 0.0


def _to_float(val) -> "float | None":
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


class MlbStatsConnector:
    def __init__(self, http_client: Optional[MlbHttpClient] = None) -> None:
        self._http = http_client or RealMlbHttpClient()

    def fetch_schedule(self, start_date: date, end_date: date) -> list[dict]:
        key = ("schedule", start_date.isoformat(), end_date.isoformat())
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        try:
            resp = self._http.get(
                f"{_BASE_URL}/schedule",
                params={
                    "sportId": _SPORT_ID,
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "gameType": "R",
                },
            )
            resp.raise_for_status()
            result = self._parse_schedule(resp.json())
            _cache.set(key, result, _TTL_SCHEDULE)
            return result
        except Exception:
            logger.exception("Failed to fetch MLB schedule %s – %s", start_date, end_date)
            return []

    def fetch_schedule_with_venue(self, start_date: date, end_date: date) -> list[dict]:
        """Like fetch_schedule but includes venue coordinates and weather."""
        key = ("schedule_venue", start_date.isoformat(), end_date.isoformat())
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        try:
            resp = self._http.get(
                f"{_BASE_URL}/schedule",
                params={
                    "sportId": _SPORT_ID,
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "gameType": "R",
                    "hydrate": "venue(location),weather,probablePitcher(note)",
                },
            )
            resp.raise_for_status()
            result = self._parse_schedule_with_venue(resp.json())
            _cache.set(key, result, _TTL_SCHEDULE)
            return result
        except Exception:
            logger.exception("Failed to fetch MLB schedule with venue %s – %s", start_date, end_date)
            return []

    def fetch_weekly_game_counts(self, week_start: date) -> dict[int, int]:
        week_end = week_start + timedelta(days=6)
        games = self.fetch_schedule(week_start, week_end)
        counts: dict[int, int] = {}
        for game in games:
            home_id = game.get("home_team_id")
            away_id = game.get("away_team_id")
            if home_id:
                counts[home_id] = counts.get(home_id, 0) + 1
            if away_id:
                counts[away_id] = counts.get(away_id, 0) + 1
        return counts

    def fetch_mlb_team_ids(self) -> list[dict]:
        key = ("team_ids",)
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        try:
            resp = self._http.get(
                f"{_BASE_URL}/teams",
                params={"sportId": _SPORT_ID, "activeStatus": "Yes"},
            )
            resp.raise_for_status()
            teams = resp.json().get("teams", [])
            result = [
                {
                    "team_id": t["id"],
                    "abbreviation": t.get("abbreviation", ""),
                    "full_name": t.get("name", ""),
                }
                for t in teams
            ]
            _cache.set(key, result, _TTL_TEAM_IDS)
            return result
        except Exception:
            logger.exception("Failed to fetch MLB team IDs")
            return []

    def fetch_team_batting_splits(self, team_id: int, handedness: str) -> dict:
        """
        Fetch batting splits vs LHP or RHP for a team.

        handedness: "L" for vs LHP (sitCodes=vl), "R" for vs RHP (sitCodes=vr).
        Returns {ops, k_pct} or {} on failure.
        """
        key = ("splits", team_id, handedness)
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        sit_code = "vl" if handedness == "L" else "vr"
        try:
            resp = self._http.get(
                f"{_BASE_URL}/teams/{team_id}/stats",
                params={
                    "stats": "statSplits",
                    "group": "hitting",
                    "sitCodes": sit_code,
                    "season": date.today().year,
                },
            )
            resp.raise_for_status()
            splits = resp.json().get("stats", [])
            for split_group in splits:
                for split in split_group.get("splits", []):
                    stat = split.get("stat", {})
                    ops = _to_float(stat.get("ops"))
                    so = _to_float(stat.get("strikeOuts"))
                    pa = _to_float(stat.get("plateAppearances"))
                    k_pct = round(so / pa * 100, 1) if so and pa else None
                    if ops is not None:
                        result = {"ops": ops, "k_pct": k_pct}
                        _cache.set(key, result, _TTL_SPLITS)
                        return result
        except Exception:
            logger.exception("Failed to fetch batting splits for team %s vs %s", team_id, handedness)
        return {}

    def fetch_pitcher_recent_starts(self, mlbam_id: int, season: int) -> list[dict]:
        """
        Return the pitcher's starts this season as:
          [{date, opponent_name, ip, k, bb, hr}]
        ip/k/bb/hr are included for FIP computation.
        """
        key = ("game_log", mlbam_id, season)
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        try:
            resp = self._http.get(
                f"{_BASE_URL}/people/{mlbam_id}/stats",
                params={
                    "stats": "gameLog",
                    "group": "pitching",
                    "season": season,
                },
            )
            resp.raise_for_status()
            stats = resp.json().get("stats", [])
            starts = []
            for group in stats:
                for split in group.get("splits", []):
                    stat = split.get("stat", {})
                    if not stat.get("gamesStarted", 0):
                        continue
                    opponent = split.get("opponent", {}).get("name", "")
                    game_date = split.get("date", "")
                    if game_date:
                        starts.append({
                            "date": game_date,
                            "opponent_name": opponent,
                            "ip": _parse_ip(stat.get("inningsPitched", "0.0")),
                            "k": int(stat.get("strikeOuts", 0)),
                            "bb": int(stat.get("baseOnBalls", 0)),
                            "hr": int(stat.get("homeRuns", 0)),
                        })
            _cache.set(key, starts, _TTL_GAME_LOG)
            return starts
        except Exception:
            logger.exception("Failed to fetch game log for player %s", mlbam_id)
            return []

    def fetch_pitcher_season_stats(self, mlbam_id: int, season: int) -> dict:
        """
        Return season-aggregated pitching stats for FIP and K% computation.
        Returns {ip, k, bb, hr, k_per_9} or {} on failure.
        """
        key = ("season_stats", mlbam_id, season)
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        try:
            resp = self._http.get(
                f"{_BASE_URL}/people/{mlbam_id}/stats",
                params={"stats": "season", "group": "pitching", "season": season},
            )
            resp.raise_for_status()
            for group in resp.json().get("stats", []):
                for split in group.get("splits", []):
                    stat = split.get("stat", {})
                    ip = _parse_ip(stat.get("inningsPitched", "0.0"))
                    if ip == 0:
                        continue
                    result = {
                        "ip": ip,
                        "k": int(stat.get("strikeOuts", 0)),
                        "bb": int(stat.get("baseOnBalls", 0)),
                        "hr": int(stat.get("homeRuns", 0)),
                        "bf": int(stat.get("battersFaced", 0)),
                        "k_per_9": _to_float(stat.get("strikeoutsPer9Inn")) or 0.0,
                    }
                    _cache.set(key, result, _TTL_SEASON_STAT)
                    return result
        except Exception:
            logger.exception("Failed to fetch season stats for player %s", mlbam_id)
        return {}

    def fetch_recent_transactions(self, days_back: int = 7) -> set[int]:
        """
        Fetch recent MLB transactions and return the set of player IDs who were
        placed on IL, optioned to minors, or designated for assignment.
        Used to invalidate PROJECTED starters who are likely unavailable.
        Returns empty set on failure.
        """
        key = ("transactions", days_back, date.today().isoformat())
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        end_date = date.today()
        start_date = end_date - timedelta(days=days_back)
        unavailable_codes = {"IL", "IL60", "IL7", "IL10", "IL15", "BRV", "DFA", "OPTION", "REST"}
        try:
            resp = self._http.get(
                f"{_BASE_URL}/transactions",
                params={
                    "sportId": _SPORT_ID,
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                },
            )
            resp.raise_for_status()
            transactions = resp.json().get("transactions", [])
            unavailable_ids: set[int] = set()
            for txn in transactions:
                type_code = txn.get("typeCode", "")
                if any(type_code.startswith(code) for code in unavailable_codes):
                    person = txn.get("person", {})
                    if pid := person.get("id"):
                        unavailable_ids.add(pid)
            _cache.set(key, unavailable_ids, _TTL_TRANSACTIONS)
            return unavailable_ids
        except Exception:
            logger.warning("Failed to fetch MLB transactions (trailing %d days)", days_back)
            return set()

    def resolve_player_mlbam_id(self, full_name: str) -> "dict | None":
        """
        Resolve a player's MLB AM ID and pitching hand by name.
        Returns {mlbam_id, throws} or None on failure.
        """
        key = ("player_id", full_name)
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        try:
            resp = self._http.get(
                f"{_BASE_URL}/people/search",
                params={"names": full_name, "sportId": _SPORT_ID},
            )
            resp.raise_for_status()
            people = resp.json().get("people", [])
            if not people:
                _cache.set(key, None, _TTL_PLAYER_ID)
                return None
            p = people[0]
            result = {
                "mlbam_id": p["id"],
                "throws": p.get("pitchHand", {}).get("code", "R"),
            }
            _cache.set(key, result, _TTL_PLAYER_ID)
            return result
        except Exception:
            logger.warning("MLB player ID lookup failed for %r", full_name)
            return None

    def fetch_weather_forecast(
        self, lat: float, lng: float, game_datetime: datetime
    ) -> "dict | None":
        """
        Fetch weather forecast from Open-Meteo for a future game.
        Returns {temp_f, humidity_pct, rain_pct} or None on failure.

        Open-Meteo returns hourly times in the venue's local timezone (timezone=auto).
        We convert game_datetime from UTC to local time using utc_offset_seconds from
        the response before matching, so a 6:40 PM CDT game (23:40 UTC) correctly
        matches the T18:00 local slot rather than the T23:00 UTC slot.
        """
        target_hour = game_datetime.replace(minute=0, second=0, microsecond=0)
        key = ("weather", round(lat, 3), round(lng, 3), target_hour.isoformat())
        hit = _cache.get(key)
        if hit is not _MISS:
            return hit
        try:
            import httpx

            resp = httpx.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lng,
                    "hourly": "temperature_2m,relativehumidity_2m,precipitation_probability",
                    "temperature_unit": "fahrenheit",
                    "timezone": "auto",
                    "forecast_days": 7,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            # Convert game time from UTC to venue local time using the offset
            # Open-Meteo returns in its response (e.g. -18000 for CDT).
            utc_offset = data.get("utc_offset_seconds", 0)
            local_hour = target_hour + timedelta(seconds=utc_offset)
            target_str = local_hour.strftime("%Y-%m-%dT%H:%M")

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            idx = None
            for i, t in enumerate(times):
                if t.startswith(target_str):
                    idx = i
                    break
            if idx is None:
                _cache.set(key, None, _TTL_WEATHER)
                return None
            temps = hourly.get("temperature_2m", [])
            humidity = hourly.get("relativehumidity_2m", [])
            rain = hourly.get("precipitation_probability", [])
            # Rain at first pitch → postponed (not a pitcher stat risk).
            # Rain in innings 7–9 → starter is out anyway.
            # The dangerous window is roughly innings 2–6: hours 1–2 after first pitch.
            rain_window = [rain[i] for i in range(idx + 1, min(idx + 3, len(rain)))]
            result = {
                "temp_f": temps[idx] if idx < len(temps) else None,
                "humidity_pct": humidity[idx] if idx < len(humidity) else None,
                "rain_pct": max(rain_window) if rain_window else None,
            }
            _cache.set(key, result, _TTL_WEATHER)
            return result
        except Exception:
            logger.warning("Open-Meteo forecast failed for (%.4f, %.4f)", lat, lng)
            return None

    @staticmethod
    def _parse_schedule(data: dict) -> list[dict]:
        games = []
        for date_entry in data.get("dates", []):
            game_date = date_entry.get("date")
            for game in date_entry.get("games", []):
                teams = game.get("teams", {})
                home = teams.get("home", {}).get("team", {})
                away = teams.get("away", {}).get("team", {})
                games.append({
                    "date": game_date,
                    "game_pk": game.get("gamePk"),
                    "home_team": home.get("name"),
                    "home_team_id": home.get("id"),
                    "away_team": away.get("name"),
                    "away_team_id": away.get("id"),
                    "status": game.get("status", {}).get("abstractGameState"),
                })
        return games

    @staticmethod
    def _parse_schedule_with_venue(data: dict) -> list[dict]:
        games = []
        for date_entry in data.get("dates", []):
            game_date = date_entry.get("date")
            for game in date_entry.get("games", []):
                teams = game.get("teams", {})
                home = teams.get("home", {}).get("team", {})
                away = teams.get("away", {}).get("team", {})

                venue = game.get("venue", {})
                location = venue.get("location", {})
                lat = _to_float(location.get("defaultCoordinates", {}).get("latitude"))
                lng = _to_float(location.get("defaultCoordinates", {}).get("longitude"))

                weather = game.get("weather", {})
                temp_str = weather.get("temp")
                weather_temp = _to_float(temp_str) if temp_str else None

                probable = game.get("teams", {})
                prob_home = probable.get("home", {}).get("probablePitcher", {})
                prob_away = probable.get("away", {}).get("probablePitcher", {})

                games.append({
                    "date": game_date,
                    "game_pk": game.get("gamePk"),
                    "home_team": home.get("name"),
                    "home_team_id": home.get("id"),
                    "away_team": away.get("name"),
                    "away_team_id": away.get("id"),
                    "status": game.get("status", {}).get("abstractGameState"),
                    "venue_name": venue.get("name"),
                    "venue_lat": lat,
                    "venue_lng": lng,
                    "weather_temp": weather_temp,
                    "weather_condition": weather.get("condition"),
                    "game_datetime": game.get("gameDate"),
                    "probable_home_id": prob_home.get("id"),
                    "probable_home_name": prob_home.get("fullName"),
                    "probable_away_id": prob_away.get("id"),
                    "probable_away_name": prob_away.get("fullName"),
                })
        return games


# ---------------------------------------------------------------------------
# Yahoo connector
# ---------------------------------------------------------------------------


class YahooConnector:
    """Yahoo Fantasy connector — OAuth + roster + free agents."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        league_id: int,
        my_team_name: str = "",
        oauth_cache_path: Path = Path("oauth2.json"),
        callback_uri: str = "https://localhost",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._league_id = league_id
        self._my_team_name = my_team_name
        self._oauth_cache_path = Path(oauth_cache_path)
        self._callback_uri = callback_uri
        self._league = None

    def _get_league(self):
        if self._league is not None:
            return self._league

        from yahoo_oauth import OAuth2
        import yahoo_fantasy_api as yfa

        if self._oauth_cache_path.exists():
            self._heal_oauth_json(self._oauth_cache_path)
            self._inject_consumer_creds(self._oauth_cache_path)
            sc = OAuth2(None, None, from_file=str(self._oauth_cache_path))
        else:
            logger.info(
                "No token cache found — starting OAuth flow. "
                "A browser will open. Authorize the app, then Yahoo will redirect to "
                "https://localhost (which won't load — that's fine). "
                "Copy just the 'code' value from the URL (?code=XXXX) and paste it below."
            )
            sc = OAuth2(
                self._client_id,
                self._client_secret,
                callback_uri=self._callback_uri,
                browser_callback=True,
            )
            _default_save = Path("secrets.json")
            if _default_save.exists():
                _default_save.rename(self._oauth_cache_path)
            if self._oauth_cache_path.exists():
                self._inject_consumer_creds(self._oauth_cache_path)

        gm = yfa.Game(sc, _MLB_GAME_CODE)
        league_keys = gm.league_ids(year=date.today().year)
        target_key = next(
            (k for k in league_keys if k.endswith(f".l.{self._league_id}")),
            None,
        )
        if target_key is None:
            raise RuntimeError(
                f"League {self._league_id} not found in Yahoo account. "
                f"Available league keys: {league_keys}"
            )
        self._league = gm.to_league(target_key)
        return self._league

    @staticmethod
    def _heal_oauth_json(path: Path) -> None:
        """
        yahoo_oauth corrupts oauth2.json in two known ways:
          1. Appends a stray '}' after the closing brace (trailing-garbage corruption).
          2. Truncates the file to zero bytes during a write (empty-file corruption).
        In both cases, attempt to recover; delete the file if unrecoverable so the
        next foreground request triggers a fresh OAuth flow instead of looping on errors.
        """
        try:
            raw = path.read_text()
            json.loads(raw)  # fast path — already valid
            return
        except json.JSONDecodeError:
            pass
        except Exception:
            logger.exception("Failed to read oauth2.json")
            return

        if not raw.strip():
            logger.error("oauth2.json is empty — deleting so OAuth re-authorizes on next request")
            path.unlink(missing_ok=True)
            return

        try:
            # Walk back from the end until we find a valid JSON object
            for end in range(len(raw), 0, -1):
                candidate = raw[:end].rstrip()
                if not candidate.endswith("}"):
                    continue
                try:
                    data = json.loads(candidate)
                    path.write_text(json.dumps(data, indent=4, sort_keys=True))
                    logger.info("Healed malformed oauth2.json (trimmed %d chars)", len(raw) - end)
                    return
                except json.JSONDecodeError:
                    continue
            logger.error("Could not heal oauth2.json — deleting so OAuth re-authorizes on next request")
            path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to heal oauth2.json")

    def _inject_consumer_creds(self, path: Path) -> None:
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("consumer_key") != self._client_id:
                data["consumer_key"] = self._client_id
                data["consumer_secret"] = self._client_secret
                with open(path, "w") as f:
                    json.dump(data, f, indent=4, sort_keys=True)
        except Exception:
            logger.exception("Failed to inject consumer creds into %s", path)

    def get_my_team_roster(self) -> list[dict]:
        lg = self._get_league()
        teams = lg.teams()
        my_team_key = self._find_my_team_key(teams)
        team = lg.to_team(my_team_key)
        return self._normalize_roster(team.roster())

    def get_free_agents(self, position: Optional[str] = None) -> list[dict]:
        lg = self._get_league()
        positions = [position] if position else self._all_positions()
        seen_ids: set[str] = set()
        players: list[dict] = []
        for pos in positions:
            try:
                # Use status "A" (all available = FA + waivers). lg.free_agents() is
                # hardcoded to "FA" which returns 0 in waiver-wire leagues.
                for p in lg._fetch_players("A", position=pos):
                    pid = str(p.get("player_id") or p.get("name") or "")
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        players.append(self._normalize_player(p))
            except Exception:
                logger.exception("Failed to fetch free agents for position %s", pos)
        return players

    def _find_my_team_key(self, teams: dict) -> str:
        if self._my_team_name:
            for key, info in teams.items():
                if info.get("name", "").lower() == self._my_team_name.lower():
                    return key
        return next(iter(teams))

    @staticmethod
    def _all_positions() -> list[str]:
        return ["C", "1B", "2B", "3B", "SS", "OF", "SP", "RP"]

    @staticmethod
    def _normalize_player(raw: dict) -> dict:
        name = raw.get("name", "")
        return {
            "yahoo_id": str(raw.get("player_id", "")),
            "name": name.get("full", "") if isinstance(name, dict) else str(name),
            "position": ", ".join(raw.get("eligible_positions", [])),
            "status": raw.get("status", ""),
            "percent_owned": raw.get("percent_owned", 0),
            "mlbam_id": None,  # resolved via resolve_player_mlbam_id()
            "team_abbr": raw.get("editorial_team_abbr", ""),
        }

    @staticmethod
    def _normalize_roster(raw_players: list) -> list[dict]:
        normalized = []
        for p in raw_players:
            if isinstance(p, dict):
                name = p.get("name", "")
                slot = p.get("selected_position", "")
                normalized.append({
                    "yahoo_id": str(p.get("player_id", "")),
                    "name": name.get("full", "") if isinstance(name, dict) else str(name),
                    "position": ", ".join(p.get("eligible_positions", [])),
                    "roster_slot": slot if isinstance(slot, str) else slot.get("position", ""),
                    "status": p.get("status", ""),
                    "team_abbr": p.get("editorial_team_abbr", ""),
                    "mlbam_id": None,  # resolved via MLB Stats API
                })
        return normalized
