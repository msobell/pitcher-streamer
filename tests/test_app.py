"""FastAPI route tests with all external deps stubbed."""

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fake data
# ---------------------------------------------------------------------------

FAKE_PARK_FACTORS = {
    "season": 2026,
    "fetched_at": "2026-05-13T10:00:00+00:00",
    "teams": {
        "Phillies": {"index_runs": 98, "index_hr": 95, "index_woba": 97, "index_so": 102},
        "Dodgers": {"index_runs": 103, "index_hr": 108, "index_woba": 102, "index_so": 98},
        "Astros": {"index_runs": 96, "index_hr": 93, "index_woba": 95, "index_so": 104},
    },
}

FAKE_TEAM_IDS = [
    {"team_id": 143, "abbreviation": "PHI", "full_name": "Philadelphia Phillies"},
    {"team_id": 119, "abbreviation": "LAD", "full_name": "Los Angeles Dodgers"},
    {"team_id": 117, "abbreviation": "HOU", "full_name": "Houston Astros"},
    {"team_id": 147, "abbreviation": "NYY", "full_name": "New York Yankees"},
]

# PHI home vs LAD (game 1001, Wheeler probable)
# HOU home vs NYY (game 1002); HOU home vs LAD (game 1003)
FAKE_SCHEDULE = [
    {
        "date": "2026-05-13",
        "game_pk": 1001,
        "home_team": "Philadelphia Phillies",
        "home_team_id": 143,
        "away_team": "Los Angeles Dodgers",
        "away_team_id": 119,
        "status": "Preview",
        "venue_name": "Citizens Bank Park",
        "venue_lat": 39.9,
        "venue_lng": -75.2,
        "weather_temp": 68.0,
        "weather_condition": "Clear",
        "game_datetime": "2026-05-13T18:05:00Z",
        "probable_home_id": 554430,       # Wheeler's mlbam_id
        "probable_home_name": "Zack Wheeler",
        "probable_away_id": None,
        "probable_away_name": None,
    },
    {
        "date": "2026-05-15",
        "game_pk": 1002,
        "home_team": "Houston Astros",
        "home_team_id": 117,
        "away_team": "New York Yankees",
        "away_team_id": 147,
        "status": "Preview",
        "venue_name": "Minute Maid Park",
        "venue_lat": 29.8,
        "venue_lng": -95.4,
        "weather_temp": None,
        "weather_condition": None,
        "game_datetime": "2026-05-15T19:10:00Z",
        "probable_home_id": 622663,       # Framber Valdez
        "probable_home_name": "Framber Valdez",
        "probable_away_id": 543037,       # Gerrit Cole
        "probable_away_name": "Gerrit Cole",
    },
    {
        "date": "2026-05-16",
        "game_pk": 1003,
        "home_team": "Houston Astros",
        "home_team_id": 117,
        "away_team": "Los Angeles Dodgers",
        "away_team_id": 119,
        "status": "Preview",
        "venue_name": "Minute Maid Park",
        "venue_lat": 29.8,
        "venue_lng": -95.4,
        "weather_temp": None,
        "weather_condition": None,
        "game_datetime": "2026-05-16T19:10:00Z",
        "probable_home_id": None,
        "probable_home_name": None,
        "probable_away_id": None,
        "probable_away_name": None,
    },
]

# Recent start within 30 days — passes the confirmed-starter gate
_RECENT_DATE = (date.today() - timedelta(days=5)).isoformat()
_OLD_DATE = (date.today() - timedelta(days=35)).isoformat()

FAKE_ROSTER = [
    {
        "yahoo_id": "1001",
        "name": "Zack Wheeler",
        "position": "SP",
        "roster_slot": "SP",
        "status": "",
        "team_abbr": "PHI",
        "mlbam_id": 554430,
        "throws": "R",
    },
    {
        "yahoo_id": "1002",
        "name": "Dylan Lee",
        "position": "RP",          # reliever — should be excluded by starter gate
        "roster_slot": "RP",
        "status": "",
        "team_abbr": "HOU",
        "mlbam_id": 999001,
        "throws": "L",
    },
    {
        "yahoo_id": "1003",
        "name": "Jose Abreu",
        "position": "1B",
        "roster_slot": "1B",
        "status": "",
        "team_abbr": "HOU",
        "mlbam_id": None,
        "throws": "R",
    },
]

FAKE_FA_SPS = [
    {
        "yahoo_id": "2001",
        "name": "Framber Valdez",
        "position": "SP",
        "status": "",
        "team_abbr": "HOU",
        "mlbam_id": 622663,
        "throws": "L",
        "percent_owned": 45.0,
    },
    {
        "yahoo_id": "2002",
        "name": "Gerrit Cole",
        "position": "SP",
        "status": "",
        "team_abbr": "NYY",
        "mlbam_id": 543037,
        "throws": "R",
        "percent_owned": 88.0,
    },
    {
        "yahoo_id": "2003",
        "name": "Marcus Stroman",
        "position": "SP",
        "status": "",
        "team_abbr": "CHC",   # no game this week — filtered by teams_playing
        "mlbam_id": 596133,
        "throws": "R",
        "percent_owned": 12.0,
    },
]

FAKE_SPLITS = {"ops": 0.710, "k_pct": 23.5}

# Recent start (within 30 days) — confirmed starter
FAKE_GAME_LOG_RECENT = [{"date": _RECENT_DATE, "opponent_name": "Los Angeles Dodgers"}]
# Old start (> 30 days) — fails starter gate
FAKE_GAME_LOG_OLD = [{"date": _OLD_DATE, "opponent_name": "Houston Astros"}]
# Reliever: no starts at all
FAKE_GAME_LOG_NO_STARTS: list = []


def _make_game_log_side_effect(recent_ids: set, old_ids: set, no_start_ids: set):
    """Return different game logs based on mlbam_id."""
    def side_effect(mlbam_id, season):
        if mlbam_id in recent_ids:
            return FAKE_GAME_LOG_RECENT
        if mlbam_id in old_ids:
            return FAKE_GAME_LOG_OLD
        if mlbam_id in no_start_ids:
            return FAKE_GAME_LOG_NO_STARTS
        return FAKE_GAME_LOG_RECENT  # default: recent starter
    return side_effect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_yahoo(roster=None, fa=None):
    m = MagicMock()
    m.get_my_team_roster.return_value = roster if roster is not None else FAKE_ROSTER
    m.get_free_agents.return_value = fa if fa is not None else FAKE_FA_SPS
    return m


def _make_mock_mlb(schedule=None, game_log_fn=None, forecast=None):
    m = MagicMock()
    m.fetch_schedule_with_venue.return_value = schedule if schedule is not None else FAKE_SCHEDULE
    m.fetch_mlb_team_ids.return_value = FAKE_TEAM_IDS
    m.fetch_team_batting_splits.return_value = FAKE_SPLITS
    if game_log_fn is not None:
        m.fetch_pitcher_recent_starts.side_effect = game_log_fn
    else:
        # Default: Wheeler and FAs are recent starters; Dylan Lee has no starts
        m.fetch_pitcher_recent_starts.side_effect = _make_game_log_side_effect(
            recent_ids={554430, 622663, 543037, 596133},
            old_ids=set(),
            no_start_ids={999001},
        )
    m.fetch_weather_forecast.return_value = forecast
    m.fetch_recent_transactions.return_value = set()
    m.fetch_pitcher_season_stats.return_value = {
        "ip": 30.0, "k": 29, "bb": 7, "hr": 1, "bf": 120, "k_per_9": 8.5
    }
    # resolve_player_mlbam_id: return mlbam_id already set on the player (no-op in tests)
    # since test fixtures pre-populate mlbam_id, this returns None (skipped by _resolve guard)
    m.resolve_player_mlbam_id.return_value = None
    return m


def _make_client(tmp_path, mock_yahoo, mock_mlb):
    import main as main_module

    (tmp_path / "park_factors.json").write_text(json.dumps(FAKE_PARK_FACTORS))

    with patch.object(main_module, "_PARK_FACTORS_PATH", tmp_path / "park_factors.json"), \
         patch.object(main_module, "_SEASON", 2026), \
         patch.object(main_module, "_LEAGUE_ID", 9999), \
         patch.object(main_module, "_MY_TEAM_NAME", "Test Team"), \
         patch("main._make_yahoo_connector", return_value=mock_yahoo), \
         patch("main.MlbStatsConnector", return_value=mock_mlb):
        with TestClient(main_module.app) as c:
            # Pre-populate cache for both week offsets so tests always get the
            # full rendered page rather than the loading shell.
            for offset in (0, 1):
                if offset not in c.app.state.pitcher_cache:
                    data = main_module._build_pitcher_data(c.app.state.park_factors, offset)
                    c.app.state.pitcher_cache[offset] = data
            yield c


@pytest.fixture
def client(tmp_path):
    yield from _make_client(tmp_path, _make_mock_yahoo(), _make_mock_mlb())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_index_returns_200(client):
    assert client.get("/").status_code == 200


def test_response_is_html(client):
    assert "text/html" in client.get("/").headers["content-type"]


def test_roster_table_present(client):
    assert "My Roster" in client.get("/").text


def test_waiver_table_present(client):
    assert "Waiver Wire" in client.get("/").text


def test_rostered_starter_appears(client):
    assert "Zack Wheeler" in client.get("/").text


def test_reliever_on_roster_excluded(client):
    # Dylan Lee has no starts in game log — starter gate must exclude him
    assert "Dylan Lee" not in client.get("/").text


def test_batter_on_roster_excluded(client):
    # Jose Abreu has no mlbam_id — skipped by starter gate
    assert "Jose Abreu" not in client.get("/").text


def test_waiver_pitcher_with_start_appears(client):
    resp = client.get("/")
    assert "Framber Valdez" in resp.text
    assert "Gerrit Cole" in resp.text


def test_waiver_pitcher_no_game_this_week_filtered(client):
    # Marcus Stroman (CHC) — team not in teams_playing
    assert "Marcus Stroman" not in client.get("/").text


def test_two_start_pitcher_gets_highlight_class(client):
    # Framber Valdez (HOU) has 2 home games: game 1002 (is_probable) + game 1003 (open slot)
    assert "two-starts" in client.get("/").text


def test_start_count_excludes_other_pitcher_confirmed_games(tmp_path):
    # Wheeler is probable for PHI game 1001.
    # Add a 2nd PHI game where someone else is the confirmed probable.
    # Wheeler's start_count should remain 1 (only the game where he's probable).
    extra_phi_game = {
        "date": "2026-05-15",
        "game_pk": 1004,
        "home_team": "Philadelphia Phillies",
        "home_team_id": 143,
        "away_team": "New York Yankees",
        "away_team_id": 147,
        "status": "Preview",
        "venue_name": "Citizens Bank Park",
        "venue_lat": 39.9,
        "venue_lng": -75.2,
        "weather_temp": 65.0,
        "weather_condition": "Clear",
        "game_datetime": "2026-05-15T18:05:00Z",
        "probable_home_id": 999999,  # some other pitcher, not Wheeler
        "probable_home_name": "Other Guy",
        "probable_away_id": None,
        "probable_away_name": None,
    }
    schedule = FAKE_SCHEDULE + [extra_phi_game]
    mock_mlb = _make_mock_mlb(schedule=schedule)
    mock_yahoo = _make_mock_yahoo(fa=[])
    for c in _make_client(tmp_path, mock_yahoo, mock_mlb):
        text = c.get("/").text
        # Wheeler still appears (has 1 confirmed start), not filtered out
        assert "Zack Wheeler" in text
        # Game 1004 should NOT appear for Wheeler (other pitcher is probable there)
        assert "mlb.com/gameday/1004" not in text


def test_score_badge_present(client):
    assert "score-badge" in client.get("/").text


def test_week_range_in_header(client):
    assert str(date.today().year) in client.get("/").text


def test_probable_star_rendered(client):
    # Wheeler is probable for game 1001 — ★ should appear
    assert "★" in client.get("/").text


def test_gameday_link_rendered(client):
    assert "mlb.com/gameday/1001" in client.get("/").text


def test_score_breakdown_details_present(client):
    # <details> elements wrap each start cell for expandable breakdown
    assert "<details>" in client.get("/").text


def test_breakdown_baseline_shown(client):
    assert "Baseline" in client.get("/").text


def test_rain_flag_rendered_when_rain_high(tmp_path):
    rainy_schedule = [{**FAKE_SCHEDULE[0], "weather_temp": None}]
    mock_mlb = _make_mock_mlb(
        schedule=rainy_schedule,
        forecast={"temp_f": 58.0, "humidity_pct": 80, "rain_pct": 75},
    )
    mock_yahoo = _make_mock_yahoo(fa=[])
    for c in _make_client(tmp_path, mock_yahoo, mock_mlb):
        assert "⛈" in c.get("/").text


def test_familiarity_flag_rendered(tmp_path):
    recent_log = [{"date": _RECENT_DATE, "opponent_name": "Los Angeles Dodgers"}]
    mock_mlb = _make_mock_mlb(
        schedule=[FAKE_SCHEDULE[0]],
        game_log_fn=lambda mid, s: recent_log,
    )
    mock_yahoo = _make_mock_yahoo(fa=[])
    for c in _make_client(tmp_path, mock_yahoo, mock_mlb):
        assert "⚠" in c.get("/").text


def test_startup_fails_on_missing_park_factors(tmp_path):
    import asyncio
    import main as main_module

    missing = tmp_path / "no_park_factors.json"
    with patch.object(main_module, "_PARK_FACTORS_PATH", missing), \
         patch.object(main_module, "_SEASON", 2026):
        with pytest.raises(SystemExit, match="park_factors.json"):
            asyncio.run(main_module.lifespan(main_module.app).__aenter__())


def test_startup_fails_on_wrong_season(tmp_path):
    import asyncio
    import main as main_module

    wrong_pf = tmp_path / "park_factors.json"
    wrong = {**FAKE_PARK_FACTORS, "season": 2025}
    wrong_pf.write_text(json.dumps(wrong))

    with patch.object(main_module, "_PARK_FACTORS_PATH", wrong_pf), \
         patch.object(main_module, "_SEASON", 2026):
        with pytest.raises(SystemExit, match="season=2025"):
            asyncio.run(main_module.lifespan(main_module.app).__aenter__())
