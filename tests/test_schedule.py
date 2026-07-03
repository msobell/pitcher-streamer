"""Unit tests for MlbStatsConnector with mocked HTTP."""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from connectors import MlbStatsConnector


def _mock_http(payload: dict):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    client = MagicMock()
    client.get.return_value = resp
    return client


def _make_schedule_response(games: list[dict]) -> dict:
    """Wrap raw games in MLB Stats API schedule response shape."""
    by_date: dict[str, list] = {}
    for g in games:
        by_date.setdefault(g["date"], []).append(g["_raw"])
    return {
        "dates": [
            {"date": d, "games": raw_games}
            for d, raw_games in by_date.items()
        ]
    }


def _make_game(game_pk, home_id, home_name, away_id, away_name, game_date="2026-05-13",
               venue_lat=40.0, venue_lng=-74.0, weather_temp=None, game_datetime=None):
    return {
        "date": game_date,
        "_raw": {
            "gamePk": game_pk,
            "gameDate": game_datetime or f"{game_date}T18:05:00Z",
            "teams": {
                "home": {"team": {"id": home_id, "name": home_name}},
                "away": {"team": {"id": away_id, "name": away_name}},
            },
            "status": {"abstractGameState": "Preview"},
            "venue": {
                "name": "Test Park",
                "location": {
                    "defaultCoordinates": {"latitude": venue_lat, "longitude": venue_lng}
                },
            },
            "weather": {"temp": str(weather_temp), "condition": "Clear"} if weather_temp else {},
        },
    }


# --- fetch_schedule_with_venue ---

def test_fetch_schedule_with_venue_parses_coords():
    game = _make_game(1, 143, "Phillies", 119, "Dodgers", venue_lat=39.9, venue_lng=-75.2)
    http = _mock_http(_make_schedule_response([game]))
    conn = MlbStatsConnector(http_client=http)

    result = conn.fetch_schedule_with_venue(date(2026, 5, 13), date(2026, 5, 13))
    assert len(result) == 1
    assert result[0]["venue_lat"] == pytest.approx(39.9)
    assert result[0]["venue_lng"] == pytest.approx(-75.2)


def test_fetch_schedule_with_venue_parses_weather():
    game = _make_game(2, 117, "Astros", 147, "Yankees", weather_temp=72)
    http = _mock_http(_make_schedule_response([game]))
    conn = MlbStatsConnector(http_client=http)

    result = conn.fetch_schedule_with_venue(date(2026, 5, 14), date(2026, 5, 14))
    assert result[0]["weather_temp"] == pytest.approx(72.0)
    assert result[0]["weather_condition"] == "Clear"


def test_fetch_schedule_with_venue_missing_weather_is_none():
    game = _make_game(3, 108, "Angels", 133, "Athletics")
    http = _mock_http(_make_schedule_response([game]))
    conn = MlbStatsConnector(http_client=http)

    result = conn.fetch_schedule_with_venue(date(2026, 5, 13), date(2026, 5, 13))
    assert result[0]["weather_temp"] is None


def test_fetch_schedule_with_venue_returns_empty_on_error():
    client = MagicMock()
    client.get.side_effect = RuntimeError("network error")
    conn = MlbStatsConnector(http_client=client)
    result = conn.fetch_schedule_with_venue(date(2026, 5, 13), date(2026, 5, 13))
    assert result == []


# --- fetch_weekly_game_counts ---

def test_fetch_weekly_game_counts_counts_correctly():
    games = [
        _make_game(1, 143, "PHI", 119, "LAD", "2026-05-13"),
        _make_game(2, 143, "PHI", 117, "HOU", "2026-05-14"),
        _make_game(3, 119, "LAD", 147, "NYY", "2026-05-15"),
    ]
    http = _mock_http(_make_schedule_response(games))
    conn = MlbStatsConnector(http_client=http)

    counts = conn.fetch_weekly_game_counts(date(2026, 5, 13))
    assert counts[143] == 2   # PHI plays twice (home both)
    assert counts[119] == 2   # LAD plays twice (away + home)
    assert counts[117] == 1   # HOU plays once
    assert counts[147] == 1   # NYY plays once


# --- fetch_mlb_team_ids ---

def test_fetch_mlb_team_ids_returns_list():
    payload = {
        "teams": [
            {"id": 143, "abbreviation": "PHI", "name": "Philadelphia Phillies"},
            {"id": 119, "abbreviation": "LAD", "name": "Los Angeles Dodgers"},
        ]
    }
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_mlb_team_ids()

    assert len(result) == 2
    phi = next(t for t in result if t["team_id"] == 143)
    assert phi["abbreviation"] == "PHI"
    assert phi["full_name"] == "Philadelphia Phillies"


# --- fetch_team_batting_splits ---

def test_fetch_team_batting_splits_vs_lhp():
    payload = {
        "stats": [
            {
                "splits": [
                    {
                        "stat": {
                            "ops": "0.750",
                            "strikeOuts": 120,
                            "plateAppearances": 500,
                        }
                    }
                ]
            }
        ]
    }
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_team_batting_splits(143, "L")

    assert result["ops"] == pytest.approx(0.750)
    assert result["k_pct"] == pytest.approx(24.0)


def test_fetch_team_batting_splits_vs_rhp():
    payload = {
        "stats": [
            {
                "splits": [
                    {
                        "stat": {
                            "ops": "0.680",
                            "strikeOuts": 200,
                            "plateAppearances": 800,
                        }
                    }
                ]
            }
        ]
    }
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_team_batting_splits(119, "R")

    assert result["ops"] == pytest.approx(0.680)
    assert result["k_pct"] == pytest.approx(25.0)


def test_fetch_team_batting_splits_returns_empty_on_error():
    client = MagicMock()
    client.get.side_effect = RuntimeError("timeout")
    conn = MlbStatsConnector(http_client=client)
    assert conn.fetch_team_batting_splits(143, "R") == {}


# --- fetch_pitcher_recent_starts ---

def test_fetch_pitcher_recent_starts_returns_starts_only():
    payload = {
        "stats": [
            {
                "splits": [
                    {
                        "date": "2026-05-08",
                        "opponent": {"name": "Los Angeles Dodgers"},
                        "stat": {"gamesStarted": 1, "inningsPitched": "6.0"},
                    },
                    {
                        "date": "2026-05-03",
                        "opponent": {"name": "San Diego Padres"},
                        "stat": {"gamesStarted": 0},  # relief appearance
                    },
                ]
            }
        ]
    }
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_pitcher_recent_starts(12345, 2026)

    assert len(result) == 1
    assert result[0]["date"] == "2026-05-08"
    assert result[0]["opponent_name"] == "Los Angeles Dodgers"


def test_fetch_pitcher_recent_starts_returns_empty_on_error():
    client = MagicMock()
    client.get.side_effect = RuntimeError("404")
    conn = MlbStatsConnector(http_client=client)
    assert conn.fetch_pitcher_recent_starts(99999, 2026) == []


# --- fetch_weather_forecast ---

def test_fetch_weather_forecast_returns_none_on_failure(monkeypatch):
    def _bad_get(*a, **kw):
        raise RuntimeError("Open-Meteo down")

    monkeypatch.setattr("httpx.get", _bad_get)
    conn = MlbStatsConnector()
    result = conn.fetch_weather_forecast(39.9, -75.2, datetime(2026, 5, 14, 18, 0))
    assert result is None


def test_fetch_weather_forecast_parses_matching_hour(monkeypatch):
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "hourly": {
            "time": ["2026-05-14T18:00", "2026-05-14T19:00"],
            "temperature_2m": [68.0, 70.0],
            "relativehumidity_2m": [55, 57],
            "precipitation_probability": [10, 15],
        }
    }
    monkeypatch.setattr("httpx.get", lambda *a, **kw: fake_resp)

    conn = MlbStatsConnector()
    result = conn.fetch_weather_forecast(39.9, -75.2, datetime(2026, 5, 14, 18, 5))
    assert result is not None
    assert result["temp_f"] == pytest.approx(68.0)
    assert result["humidity_pct"] == 55
    assert result["rain_pct"] == 15  # max over hours 1–2 post-first-pitch (T19 only in this fixture)


# --- resolve_player_mlbam_id ---

def test_resolve_player_mlbam_id_returns_id_and_throws():
    payload = {
        "people": [
            {
                "id": 554430,
                "fullName": "Zack Wheeler",
                "pitchHand": {"code": "R"},
            }
        ]
    }
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.resolve_player_mlbam_id("Zack Wheeler")
    assert result is not None
    assert result["mlbam_id"] == 554430
    assert result["throws"] == "R"


def test_resolve_player_mlbam_id_returns_none_when_not_found():
    payload = {"people": []}
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    assert conn.resolve_player_mlbam_id("Nobody McUnknown") is None


def test_resolve_player_mlbam_id_returns_none_on_error():
    client = MagicMock()
    client.get.side_effect = RuntimeError("network error")
    conn = MlbStatsConnector(http_client=client)
    assert conn.resolve_player_mlbam_id("Zack Wheeler") is None


# --- fetch_recent_transactions ---

def test_fetch_recent_transactions_returns_unavailable_ids():
    # Real API shapes: IL moves arrive as typeCode SC with the direction only
    # in the description; optioned = OPT, DFA = DES, recalled = CU.
    payload = {
        "transactions": [
            {"typeCode": "SC", "date": "2026-06-28",
             "description": "Philadelphia Phillies placed RHP Zack Wheeler on the 15-day injured list.",
             "person": {"id": 554430, "fullName": "Zack Wheeler"}},
            {"typeCode": "OPT", "date": "2026-06-28",
             "description": "Houston Astros optioned LHP Framber Valdez to Sugar Land Space Cowboys.",
             "person": {"id": 622663, "fullName": "Framber Valdez"}},
            {"typeCode": "DES", "date": "2026-06-29",
             "description": "New York Mets designated RHP Adrian Houser for assignment.",
             "person": {"id": 605288, "fullName": "Adrian Houser"}},
            {"typeCode": "SC", "date": "2026-06-29",
             "description": "Detroit Tigers transferred RHP Jackson Jobe from the 15-day injured list to the 60-day injured list.",
             "person": {"id": 693433, "fullName": "Jackson Jobe"}},
            {"typeCode": "TR", "date": "2026-06-30",
             "description": "New York Yankees traded RHP Gerrit Cole.",
             "person": {"id": 543037, "fullName": "Gerrit Cole"}},  # not unavailable
        ]
    }
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_recent_transactions()
    assert result == {554430, 622663, 605288, 693433}


def test_fetch_recent_transactions_activation_clears_earlier_placement():
    # Placed on the IL, then activated within the window → available again.
    # Listed out of order to verify chronological processing.
    payload = {
        "transactions": [
            {"typeCode": "SC", "date": "2026-06-30",
             "description": "Chicago Cubs activated LHP Matthew Boyd from the 15-day injured list.",
             "person": {"id": 571510, "fullName": "Matthew Boyd"}},
            {"typeCode": "SC", "date": "2026-06-24",
             "description": "Chicago Cubs placed LHP Matthew Boyd on the 15-day injured list.",
             "person": {"id": 571510, "fullName": "Matthew Boyd"}},
            {"typeCode": "CU", "date": "2026-06-29",
             "description": "Milwaukee Brewers recalled RHP Logan Henderson.",
             "person": {"id": 694297, "fullName": "Logan Henderson"}},
            {"typeCode": "OPT", "date": "2026-06-25",
             "description": "Milwaukee Brewers optioned RHP Logan Henderson to Nashville Sounds.",
             "person": {"id": 694297, "fullName": "Logan Henderson"}},
        ]
    }
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    assert conn.fetch_recent_transactions() == set()


def test_fetch_recent_transactions_returns_empty_on_error():
    client = MagicMock()
    client.get.side_effect = RuntimeError("network error")
    conn = MlbStatsConnector(http_client=client)
    assert conn.fetch_recent_transactions() == set()


# --- fetch_game_boxscore ---

def _make_boxscore_response(pitcher_id: int, side: str, stats: dict, note: str = "") -> dict:
    return {
        "teams": {
            side: {
                "pitchers": [pitcher_id],
                "players": {
                    f"ID{pitcher_id}": {
                        "person": {"id": pitcher_id, "fullName": "Test Pitcher"},
                        "stats": {"pitching": {"note": note, **stats}},
                    }
                },
            },
            "home" if side == "away" else "away": {"pitchers": [], "players": {}},
        }
    }


def test_fetch_game_boxscore_home_pitcher():
    payload = _make_boxscore_response(
        554430, "home",
        {"inningsPitched": "6.1", "earnedRuns": 2, "hits": 5,
         "baseOnBalls": 1, "strikeOuts": 8, "homeRuns": 0,
         "pitchesThrown": 95},
        note="(W, 5-2)",
    )
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_game_boxscore(999001, 554430)
    assert result is not None
    assert result["ip"] == "6.1"
    assert result["er"] == 2
    assert result["k"] == 8
    assert result["hr"] == 0
    assert result["pitches"] == 95
    assert result["decision"] == "W"


def test_fetch_game_boxscore_away_pitcher():
    payload = _make_boxscore_response(
        622663, "away",
        {"inningsPitched": "5.0", "earnedRuns": 3, "hits": 7,
         "baseOnBalls": 2, "strikeOuts": 5, "homeRuns": 1,
         "pitchesThrown": 88},
        note="(L, 2-4)",
    )
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_game_boxscore(999002, 622663)
    assert result is not None
    assert result["decision"] == "L"
    assert result["hr"] == 1


def test_fetch_game_boxscore_no_decision():
    payload = _make_boxscore_response(
        543037, "home",
        {"inningsPitched": "4.2", "earnedRuns": 4, "hits": 8,
         "baseOnBalls": 3, "strikeOuts": 4, "homeRuns": 2,
         "pitchesThrown": 80},
        note="",
    )
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_game_boxscore(999003, 543037)
    assert result is not None
    assert result["decision"] is None


def test_fetch_game_boxscore_pitcher_not_in_game():
    payload = _make_boxscore_response(
        999999, "home",
        {"inningsPitched": "6.0", "earnedRuns": 1},
    )
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    # Pitcher 554430 didn't pitch in this game
    result = conn.fetch_game_boxscore(999004, 554430)
    assert result is None


def test_fetch_game_boxscore_returns_none_on_error():
    client = MagicMock()
    client.get.side_effect = RuntimeError("network error")
    conn = MlbStatsConnector(http_client=client)
    assert conn.fetch_game_boxscore(999005, 554430) is None


# --- fetch_game_savant ---

def _make_savant_gf_response(pitcher_id: int, pitches: list[dict]) -> dict:
    return {"home_pitchers": {str(pitcher_id): pitches}, "away_pitchers": {}}


def test_fetch_game_savant_computes_whiff_and_chase(monkeypatch):
    pitches = [
        # In-zone swinging strike → whiff, not a chase
        {"description": "Swinging Strike", "isInZone": True, "savantIsInZone": False,
         "hit_speed": "", "is_barrel": False, "pitch_type": "FF", "start_speed": 95.0},
        # Out-of-zone foul → chase, not a whiff
        {"description": "Foul", "isInZone": False, "savantIsInZone": False,
         "hit_speed": "", "is_barrel": False, "pitch_type": "SL", "start_speed": 84.0},
        # Out-of-zone ball (not swung at) → not a chase
        {"description": "Ball", "isInZone": False, "savantIsInZone": False,
         "hit_speed": "", "is_barrel": False, "pitch_type": "SL", "start_speed": 83.5},
        # In-zone ball in play → BIP, hard hit, barrel
        {"description": "In play, out(s)", "isInZone": True, "savantIsInZone": False,
         "hit_speed": "98.5", "is_barrel": True, "pitch_type": "FF", "start_speed": 94.5},
        # Called strike — no swing
        {"description": "Called Strike", "isInZone": True, "savantIsInZone": False,
         "hit_speed": "", "is_barrel": False, "pitch_type": "FF", "start_speed": 95.5},
    ]
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = _make_savant_gf_response(554430, pitches)
    monkeypatch.setattr("httpx.get", lambda *a, **kw: fake_resp)

    conn = MlbStatsConnector()
    result = conn.fetch_game_savant(822982, 554430)
    assert result is not None
    # Swings: swinging strike + foul + BIP = 3. Whiff = 1 (swinging strike only).
    assert result["whiff_pct"] == 33       # round(1/3 * 100)
    # Out-of-zone pitches: foul + ball = 2. Chases (swings outside zone): foul = 1.
    assert result["chase_pct"] == 50       # round(1/2 * 100)
    assert result["hard_hit_pct"] == 100   # 1 BIP at 98.5 >= 95 mph
    assert result["barrels"] == 1
    assert result["avg_ev"] == pytest.approx(98.5)
    assert result["avg_fb_velo"] == pytest.approx((95.0 + 94.5 + 95.5) / 3, abs=0.1)


def test_fetch_game_savant_csw_fstrike_zone_swords(monkeypatch):
    pitches = [
        # First pitch (0-0), called strike → counts toward CSW and F-Strike
        {"description": "Called Strike", "isInZone": True, "pre_balls": 0, "pre_strikes": 0,
         "hit_speed": "", "is_barrel": False},
        # First pitch (0-0), ball → F-Strike denominator only, not numerator
        {"description": "Ball", "isInZone": False, "pre_balls": 0, "pre_strikes": 0,
         "hit_speed": "", "is_barrel": False},
        # Mid-count swinging strike in zone → CSW + whiff + in-zone swing (no contact)
        {"description": "Swinging Strike", "isInZone": True, "pre_balls": 1, "pre_strikes": 1,
         "hit_speed": "", "is_barrel": False, "isSword": True},
        # Mid-count in-zone foul → in-zone swing WITH contact
        {"description": "Foul", "isInZone": True, "pre_balls": 1, "pre_strikes": 2,
         "hit_speed": "", "is_barrel": False},
    ]
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = _make_savant_gf_response(554430, pitches)
    monkeypatch.setattr("httpx.get", lambda *a, **kw: fake_resp)

    conn = MlbStatsConnector()
    result = conn.fetch_game_savant(822982, 554430)
    assert result is not None
    # CSW = (whiffs 1 + called strikes 1) / 4 pitches = 50%
    assert result["csw_pct"] == 50
    # First pitches: 2 (the two 0-0 pitches). Strikes among them: 1 (called strike).
    assert result["f_strike_pct"] == 50
    # In-zone swings: swinging strike + foul = 2. Contact among them: foul = 1.
    assert result["zone_contact_pct"] == 50
    assert result["swords"] == 1


def test_fetch_game_savant_pitcher_not_in_game(monkeypatch):
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {"home_pitchers": {}, "away_pitchers": {}}
    monkeypatch.setattr("httpx.get", lambda *a, **kw: fake_resp)

    conn = MlbStatsConnector()
    assert conn.fetch_game_savant(822982, 554430) is None


def test_fetch_game_savant_returns_none_on_error(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    conn = MlbStatsConnector()
    assert conn.fetch_game_savant(822982, 554430) is None


# --- fetch_fangraphs_team_woba ---

def test_fetch_fangraphs_team_woba_maps_abbreviations(monkeypatch):
    # Include one team that needs abbreviation mapping (ARI → AZ)
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "data": [
            {"TeamNameAbb": "LAD", "wOBA": 0.3438},
            {"TeamNameAbb": "ARI", "wOBA": 0.3100},   # should map to AZ
            {"TeamNameAbb": "CHW", "wOBA": 0.3240},   # should map to CWS
            {"TeamNameAbb": "KCR", "wOBA": 0.3180},   # should map to KC
            {"TeamNameAbb": "SDP", "wOBA": 0.2940},   # should map to SD
            {"TeamNameAbb": "SFG", "wOBA": 0.2960},   # should map to SF
            {"TeamNameAbb": "TBR", "wOBA": 0.3260},   # should map to TB
            {"TeamNameAbb": "WSN", "wOBA": 0.3280},   # should map to WSH
        ]
    }
    monkeypatch.setattr("httpx.get", lambda *a, **kw: fake_resp)

    conn = MlbStatsConnector()
    result = conn.fetch_fangraphs_team_woba(2026)

    assert result["LAD"] == pytest.approx(0.344, abs=0.001)
    assert "ARI" not in result
    assert result["AZ"] == pytest.approx(0.310, abs=0.001)
    assert "CHW" not in result
    assert result["CWS"] == pytest.approx(0.324, abs=0.001)
    assert result["KC"] == pytest.approx(0.318, abs=0.001)
    assert result["SD"] == pytest.approx(0.294, abs=0.001)
    assert result["SF"] == pytest.approx(0.296, abs=0.001)
    assert result["TB"] == pytest.approx(0.326, abs=0.001)
    assert result["WSH"] == pytest.approx(0.328, abs=0.001)


def test_fetch_fangraphs_team_woba_returns_empty_on_error(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("FG down")))
    conn = MlbStatsConnector()
    assert conn.fetch_fangraphs_team_woba(2026) == {}
