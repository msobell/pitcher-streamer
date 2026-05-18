"""Unit tests for YahooConnector with mocked yahoo_fantasy_api."""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from connectors import YahooConnector


def _make_raw_player(player_id, full_name, eligible_positions, status="", team_abbr="NYY"):
    # Matches yahoo_fantasy_api's actual free_agents() return format:
    # name is a plain string, eligible_positions is a list of strings.
    return {
        "player_id": player_id,
        "name": full_name,
        "eligible_positions": eligible_positions if isinstance(eligible_positions, list) else [eligible_positions],
        "status": status,
        "editorial_team_abbr": team_abbr,
        "percent_owned": 45.0,
    }


def _make_roster_player(player_id, full_name, eligible_positions, slot, status="", team_abbr="NYY"):
    # Matches yahoo_fantasy_api's actual roster() return format:
    # name is a plain string, eligible_positions is a list, selected_position is a string.
    return {
        "player_id": player_id,
        "name": full_name,
        "eligible_positions": eligible_positions if isinstance(eligible_positions, list) else [eligible_positions],
        "selected_position": slot,
        "status": status,
        "editorial_team_abbr": team_abbr,
    }


@pytest.fixture
def connector(tmp_path):
    return YahooConnector(
        client_id="fake_id",
        client_secret="fake_secret",
        league_id=9999,
        my_team_name="Test Team",
        oauth_cache_path=tmp_path / "oauth2.json",
    )


@pytest.fixture
def mock_league():
    return MagicMock()


def test_get_my_team_roster_returns_normalized(connector, mock_league):
    raw = [
        _make_roster_player(1, "Zack Wheeler", "SP", "SP", team_abbr="PHI"),
        _make_roster_player(2, "Jose Abreu", "1B", "1B", team_abbr="HOU"),
        _make_roster_player(3, "Framber Valdez", "SP", "SP", team_abbr="HOU"),
    ]
    mock_team = MagicMock()
    mock_team.roster.return_value = raw
    mock_league.teams.return_value = {"101.l.9999.t.1": {"name": "Test Team"}}
    mock_league.to_team.return_value = mock_team

    connector._league = mock_league
    result = connector.get_my_team_roster()

    assert len(result) == 3
    sp = next(p for p in result if p["name"] == "Zack Wheeler")
    assert sp["yahoo_id"] == "1"
    assert sp["roster_slot"] == "SP"
    assert sp["team_abbr"] == "PHI"


def test_get_my_team_roster_slot_as_dict(connector, mock_league):
    """roster_slot dict form is still handled defensively (older yahoo_fantasy_api versions)."""
    raw = [
        {
            "player_id": 5,
            "name": "Shane Bieber",
            "eligible_positions": ["SP", "P"],
            "selected_position": {"position": "SP"},
            "status": "",
            "editorial_team_abbr": "CLE",
        }
    ]
    mock_team = MagicMock()
    mock_team.roster.return_value = raw
    mock_league.teams.return_value = {"101.l.9999.t.1": {"name": "Test Team"}}
    mock_league.to_team.return_value = mock_team

    connector._league = mock_league
    result = connector.get_my_team_roster()
    assert result[0]["roster_slot"] == "SP"


def test_get_free_agents_sp_deduplicates(connector, mock_league):
    fa_list = [
        _make_raw_player(10, "Dylan Cease", "SP", team_abbr="SD"),
        _make_raw_player(11, "Chris Sale", "SP", team_abbr="ATL"),
        _make_raw_player(10, "Dylan Cease", "SP", team_abbr="SD"),  # duplicate
    ]
    mock_league._fetch_players.return_value = fa_list
    mock_league.teams.return_value = {"101.l.9999.t.1": {"name": "Test Team"}}

    connector._league = mock_league
    result = connector.get_free_agents("SP")

    names = [p["name"] for p in result]
    assert names.count("Dylan Cease") == 1
    assert len(result) == 2


def test_get_free_agents_normalized_fields(connector, mock_league):
    mock_league._fetch_players.return_value = [
        _make_raw_player(20, "Logan Webb", ["SP", "P"], team_abbr="SF")
    ]
    mock_league.teams.return_value = {"101.l.9999.t.1": {"name": "Test Team"}}

    connector._league = mock_league
    result = connector.get_free_agents("SP")

    p = result[0]
    assert p["yahoo_id"] == "20"
    assert p["name"] == "Logan Webb"
    assert "SP" in p["position"]
    assert p["team_abbr"] == "SF"
    assert p["percent_owned"] == 45.0


def test_get_free_agents_exception_returns_empty(connector, mock_league):
    mock_league._fetch_players.side_effect = RuntimeError("API down")
    mock_league.teams.return_value = {}

    connector._league = mock_league
    result = connector.get_free_agents("SP")
    assert result == []
