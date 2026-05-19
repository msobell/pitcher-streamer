"""Unit tests for MlbStatsConnector with mocked HTTP."""

import json
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
    import connectors
    def _bad_get(*a, **kw):
        raise RuntimeError("Open-Meteo down")

    monkeypatch.setattr("httpx.get", _bad_get)
    conn = MlbStatsConnector()
    result = conn.fetch_weather_forecast(39.9, -75.2, datetime(2026, 5, 14, 18, 0))
    assert result is None


def test_fetch_weather_forecast_parses_matching_hour(monkeypatch):
    import httpx

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
    payload = {
        "transactions": [
            {"typeCode": "IL10", "person": {"id": 554430, "fullName": "Zack Wheeler"}},
            {"typeCode": "OPTION", "person": {"id": 622663, "fullName": "Framber Valdez"}},
            {"typeCode": "TRADE", "person": {"id": 543037, "fullName": "Gerrit Cole"}},  # not unavailable
        ]
    }
    conn = MlbStatsConnector(http_client=_mock_http(payload))
    result = conn.fetch_recent_transactions()
    assert 554430 in result
    assert 622663 in result
    assert 543037 not in result


def test_fetch_recent_transactions_returns_empty_on_error():
    client = MagicMock()
    client.get.side_effect = RuntimeError("network error")
    conn = MlbStatsConnector(http_client=client)
    assert conn.fetch_recent_transactions() == set()
