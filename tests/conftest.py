"""Shared fixtures for pitcher-streamer tests."""

import pytest
import connectors


@pytest.fixture(autouse=True)
def clear_api_cache():
    """Reset the shared TTL cache before every test to prevent cross-test pollution."""
    connectors._cache.clear()
    yield
    connectors._cache.clear()


FAKE_ROSTER = [
    {
        "yahoo_id": "1001",
        "name": "Zack Wheeler",
        "position": "SP",
        "roster_slot": "SP",
        "status": "",
        "team_abbr": "PHI",
        "mlbam_id": None,
    },
    {
        "yahoo_id": "1002",
        "name": "Framber Valdez",
        "position": "SP",
        "roster_slot": "SP",
        "status": "",
        "team_abbr": "HOU",
        "mlbam_id": None,
    },
    {
        "yahoo_id": "1003",
        "name": "Jose Abreu",
        "position": "1B",
        "roster_slot": "1B",
        "status": "",
        "team_abbr": "HOU",
        "mlbam_id": None,
    },
]

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
        "venue_lat": 39.9061,
        "venue_lng": -75.1665,
        "weather_temp": 68.0,
        "weather_condition": "Partly Cloudy",
        "game_datetime": "2026-05-13T18:05:00Z",
    },
    {
        "date": "2026-05-14",
        "game_pk": 1002,
        "home_team": "Houston Astros",
        "home_team_id": 117,
        "away_team": "New York Yankees",
        "away_team_id": 147,
        "status": "Preview",
        "venue_name": "Minute Maid Park",
        "venue_lat": 29.7573,
        "venue_lng": -95.3555,
        "weather_temp": None,
        "weather_condition": None,
        "game_datetime": "2026-05-14T19:10:00Z",
    },
]
