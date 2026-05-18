"""Unit tests for rotation.py — pure functions, no HTTP."""

from datetime import date, timedelta

from rotation import build_team_rotation, project_probable_pitchers


def _make_candidate(mlbam_id, name, team_id):
    return {"mlbam_id": mlbam_id, "name": name, "team_id": team_id}


def _recent(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------------------
# build_team_rotation
# ---------------------------------------------------------------------------


def test_build_team_rotation_orders_by_last_start():
    c1 = _make_candidate(1, "Ace", 143)
    c2 = _make_candidate(2, "Two", 143)
    c3 = _make_candidate(3, "Three", 143)

    cache = {
        1: [{"date": _recent(5), "opponent_name": "X"}],
        2: [{"date": _recent(7), "opponent_name": "Y"}],  # within 8-day cutoff
        3: [{"date": _recent(2), "opponent_name": "Z"}],
    }
    rotation = build_team_rotation(143, [c1, c2, c3], cache)

    assert len(rotation) == 3
    assert rotation[0]["pitcher_id"] == 3   # most recent (2 days ago)
    assert rotation[1]["pitcher_id"] == 1   # 5 days ago
    assert rotation[2]["pitcher_id"] == 2   # 7 days ago


def test_build_team_rotation_assigns_slot_numbers():
    c1 = _make_candidate(1, "Ace", 143)
    c2 = _make_candidate(2, "Two", 143)

    cache = {
        1: [{"date": _recent(4), "opponent_name": "X"}],
        2: [{"date": _recent(8), "opponent_name": "Y"}],  # exactly at cutoff — included
    }
    rotation = build_team_rotation(143, [c1, c2], cache)

    assert len(rotation) == 2
    assert rotation[0]["slot"] == 1
    assert rotation[1]["slot"] == 2


def test_build_team_rotation_excludes_stale_pitchers():
    c1 = _make_candidate(1, "Active", 143)
    c2 = _make_candidate(2, "Stale", 143)

    cache = {
        1: [{"date": _recent(5), "opponent_name": "X"}],
        2: [{"date": _recent(10), "opponent_name": "Y"}],  # > 8 day cutoff
    }
    rotation = build_team_rotation(143, [c1, c2], cache)
    assert len(rotation) == 1
    assert rotation[0]["pitcher_id"] == 1


def test_build_team_rotation_excludes_other_teams():
    c_phi = _make_candidate(1, "PHI Ace", 143)
    c_hou = _make_candidate(2, "HOU Ace", 117)

    cache = {
        1: [{"date": _recent(5), "opponent_name": "X"}],
        2: [{"date": _recent(5), "opponent_name": "Y"}],
    }
    rotation = build_team_rotation(143, [c_phi, c_hou], cache)
    assert len(rotation) == 1
    assert rotation[0]["pitcher_id"] == 1


def test_build_team_rotation_empty_when_no_candidates():
    assert build_team_rotation(143, [], {}) == []


def test_build_team_rotation_excludes_missing_mlbam_id():
    c = {"mlbam_id": None, "name": "Unknown", "team_id": 143}
    cache = {}
    assert build_team_rotation(143, [c], cache) == []


# ---------------------------------------------------------------------------
# project_probable_pitchers
# ---------------------------------------------------------------------------


def _daily_games(start_days_ago, count, base_pk=900):
    """Return game dicts for `count` consecutive days starting `start_days_ago` days ago."""
    return [
        {"game_pk": base_pk + i, "date": _recent(start_days_ago - i), "is_probable": False}
        for i in range(count)
    ]


def test_project_probable_pitchers_assigns_eligible_pitcher():
    # Pitcher started 5 days ago; team has had games every day (no off-days).
    # Projected next start = last_start + 5 = today → should match today's game.
    rotation = [
        {"pitcher_id": 1, "name": "Ace", "last_start": _recent(5), "slot": 1,
         "next_eligible": date.today().isoformat()},
    ]
    # Games for last 5 days + today (no off-days)
    games = _daily_games(5, 6)  # days 5,4,3,2,1,0 ago
    today_pk = games[-1]["game_pk"]
    result = project_probable_pitchers(143, games, rotation, confirmed_probable_ids=set())
    assert today_pk in result
    assert result[today_pk]["pitcher_id"] == 1
    assert result[today_pk]["confidence"] == "PROJECTED"


def test_project_probable_pitchers_confirmed_eligible_for_second_start():
    # Pitcher is confirmed for today's game. Their confirmed game date is today,
    # so their next eligible start is today+5 — outside this week's remaining games.
    # They should NOT be projected for any additional game this week.
    today = date.today()
    rotation = [
        {"pitcher_id": 1, "name": "Ace", "last_start": _recent(5), "slot": 1,
         "next_eligible": today.isoformat()},
    ]
    # games: confirmed game today + open game tomorrow
    today_game = {"game_pk": 100, "date": today.isoformat(), "probable_home_id": 1,
                  "probable_away_id": None, "is_probable": False}
    tomorrow_game = {"game_pk": 101, "date": (today + timedelta(days=1)).isoformat(),
                     "probable_home_id": None, "probable_away_id": None, "is_probable": False}
    result = project_probable_pitchers(143, [today_game, tomorrow_game], rotation,
                                       confirmed_probable_ids={1})
    # Pitcher confirmed today → next eligible today+5 → not projected for tomorrow
    assert all(v["pitcher_id"] != 1 for v in result.values())


def test_project_probable_pitchers_skips_ineligible():
    # Pitcher started 1 day ago — projected next start is in 4 days.
    rotation = [
        {"pitcher_id": 1, "name": "Ace", "last_start": _recent(1), "slot": 1,
         "next_eligible": (date.today() + timedelta(days=4)).isoformat()},
    ]
    # Only today's game
    games = [{"game_pk": 999, "date": date.today().isoformat(), "is_probable": False}]
    result = project_probable_pitchers(143, games, rotation, confirmed_probable_ids=set())
    assert 999 not in result


def test_project_probable_pitchers_empty_rotation():
    result = project_probable_pitchers(143, [{"game_pk": 1, "date": date.today().isoformat(), "is_probable": False}], [], set())
    assert result == {}


def test_project_probable_pitchers_offday_shifts_projection():
    # Pitcher started 5 days ago, but the team had an off-day 2 days ago.
    # Projection should shift by 1 day (today+1).
    today = date.today()
    last_start = today - timedelta(days=5)
    # Games: 5d ago (start), 4d ago, 3d ago — skip 2d ago (off-day) — 1d ago, today, tomorrow
    game_dates = [
        last_start,
        last_start + timedelta(days=1),
        last_start + timedelta(days=2),
        # day 3 = last_start+3 = 2 days ago: off-day (no game)
        today - timedelta(days=1),
        today,
        today + timedelta(days=1),
    ]
    games = [
        {"game_pk": 900 + i, "date": d.isoformat(), "is_probable": False}
        for i, d in enumerate(game_dates)
    ]
    rotation = [
        {"pitcher_id": 1, "name": "Ace", "last_start": last_start.isoformat(), "slot": 1,
         "next_eligible": (last_start + timedelta(days=5)).isoformat()},
    ]
    result = project_probable_pitchers(143, games, rotation, confirmed_probable_ids=set())
    # Projected date = last_start + 5 + 1 (one off-day) = today + 1
    tomorrow_pk = next((g["game_pk"] for g in games if g["date"] == (today + timedelta(days=1)).isoformat()), None)
    assert tomorrow_pk is not None
    assert tomorrow_pk in result
    assert result[tomorrow_pk]["pitcher_id"] == 1
