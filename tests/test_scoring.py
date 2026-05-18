"""Unit tests for compute_matchup_score() — pure, no HTTP."""

import pytest
from scoring import compute_matchup_score, _calc_fip, _log5_k_pct, _z

# Helpers that exercise the full new signature
def _score(k_pct=22.0, ops=0.720, park=100, days=None, temp=None, rain=None,
           pitcher_k_pct=None, season_fip=None, recent_fip=None):
    score, _ = compute_matchup_score(
        k_pct, ops, park, days, temp, rain,
        pitcher_k_pct=pitcher_k_pct, season_fip=season_fip, recent_fip=recent_fip,
    )
    return score


def _breakdown(**kw):
    _, bd = compute_matchup_score(
        kw.pop("k_pct", 22.0), kw.pop("ops", 0.720), kw.pop("park", 100),
        kw.pop("days", None), kw.pop("temp", None), kw.pop("rain", None), **kw
    )
    return bd


# --- _calc_fip ---

def test_calc_fip_basic():
    # Wheeler-ish: 30 IP, 29 K, 7 BB, 1 HR
    fip = _calc_fip(29, 7, 1, 30.0)
    assert fip == pytest.approx(3.10 + (13 - 58 + 21) / 30, abs=0.01)

def test_calc_fip_zero_ip():
    assert _calc_fip(10, 5, 1, 0.0) is None


# --- _z ---

def test_z_at_mean():
    assert _z(4.10, 4.10, 0.60) == pytest.approx(0.0)

def test_z_one_sigma_above():
    assert _z(4.70, 4.10, 0.60) == pytest.approx(1.0)

def test_z_one_sigma_below():
    assert _z(3.50, 4.10, 0.60) == pytest.approx(-1.0)


# --- _log5_k_pct ---

def test_log5_symmetry_at_league_avg():
    # Log5 is nonlinear — two avg pitchers produce slightly above the simple avg
    result = _log5_k_pct(22.0, 22.0)
    assert 20.0 < result < 30.0  # sanity bounds

def test_log5_high_k_pitcher_vs_high_k_team():
    result = _log5_k_pct(30.0, 28.0)
    assert result > 22.0  # both above avg → high expected K

def test_log5_low_k_pitcher_vs_low_k_team():
    result = _log5_k_pct(15.0, 16.0)
    assert result < 22.0  # both below avg → low expected K


# --- Park factor ---

def test_baseline():
    assert _score() == 50.0

def test_pitcher_friendly_park_increases_score():
    assert _score(park=90) > _score(park=100)

def test_hitter_friendly_park_decreases_score():
    assert _score(park=110) < _score(park=100)

def test_park_proportional():
    d10 = _score(park=90) - _score(park=100)
    d20 = _score(park=80) - _score(park=100)
    assert abs(d20 - 2 * d10) < 0.1


# --- Opponent offense (Log5 when pitcher_k_pct provided) ---

def test_high_opponent_k_pct_increases_score_with_log5():
    s_high = _score(k_pct=28.0, pitcher_k_pct=24.0)
    s_avg = _score(k_pct=22.0, pitcher_k_pct=24.0)
    assert s_high > s_avg

def test_low_opponent_k_pct_decreases_score_with_log5():
    s_low = _score(k_pct=16.0, pitcher_k_pct=24.0)
    s_avg = _score(k_pct=22.0, pitcher_k_pct=24.0)
    assert s_low < s_avg

def test_ops_fallback_when_no_pitcher_k_pct():
    # Without pitcher_k_pct, falls back to OPS-based offense
    s_good = _score(k_pct=22.0, ops=0.650)
    s_bad = _score(k_pct=22.0, ops=0.800)
    assert s_good > s_bad


# --- Season FIP ---

def test_elite_season_fip_increases_score():
    assert _score(season_fip=2.50) > _score(season_fip=4.10)

def test_poor_season_fip_decreases_score():
    assert _score(season_fip=5.50) < _score(season_fip=4.10)

def test_no_season_fip_neutral():
    # FIP=None → fip_index=100 → season_fip_delta=0
    assert _score(season_fip=None) == _score(season_fip=4.10)


# --- Recent FIP ---

def test_hot_recent_fip_increases_score():
    assert _score(recent_fip=2.00) > _score(recent_fip=4.10)

def test_cold_recent_fip_decreases_score():
    assert _score(recent_fip=6.00) < _score(recent_fip=4.10)


# --- Familiarity ---

def test_familiarity_within_5_days():
    # clamp may affect result if base score is near boundary; test the delta direction
    assert _score(days=5) <= _score() - 12 + 0.1  # -12 penalty (may be clamped)

def test_familiarity_within_9_days():
    assert _score(days=7) <= _score() - 8 + 0.1

def test_familiarity_exactly_9_days():
    assert _score(days=9) <= _score() - 8 + 0.1

def test_no_familiarity_penalty_after_9_days():
    assert _score(days=10) == _score()

def test_no_familiarity_penalty_none():
    assert _score(days=None) == _score()


# --- Weather ---

def test_cold_weather_boost():
    assert _score(temp=54) == pytest.approx(_score() + 5, abs=0.1)

def test_hot_weather_penalty():
    assert _score(temp=83) == pytest.approx(_score() - 5, abs=0.1)

def test_moderate_temp_no_adjustment():
    assert _score(temp=70) == _score()

def test_rain_penalty():
    assert _score(rain=51) == pytest.approx(_score() - 15, abs=0.1)

def test_rain_exactly_50_no_penalty():
    assert _score(rain=50) == pytest.approx(_score(), abs=0.1)

def test_none_temp_skips():
    assert _score(temp=None) == _score()

def test_none_rain_skips():
    assert _score(rain=None) == _score()


# --- Combined ---

def test_combined_all_favorable():
    s, _ = compute_matchup_score(
        opponent_k_pct=28.0, opponent_ops=0.650, park_index_runs=90,
        days_since_faced=None, temp_f=50.0, rain_pct=10.0,
        pitcher_k_pct=26.0, season_fip=2.80, recent_fip=2.50,
    )
    assert s > 60

def test_combined_all_unfavorable():
    s, _ = compute_matchup_score(
        opponent_k_pct=16.0, opponent_ops=0.820, park_index_runs=115,
        days_since_faced=3, temp_f=88.0, rain_pct=65.0,
        pitcher_k_pct=18.0, season_fip=5.50, recent_fip=6.00,
    )
    assert s < 30


# --- Breakdown structure ---

def test_breakdown_total_matches_score():
    s, bd = compute_matchup_score(25.0, 0.680, 95, 7, 62.0, 20.0,
                                   pitcher_k_pct=24.0, season_fip=3.20, recent_fip=3.00)
    assert bd["total"] == s

def test_breakdown_deltas_sum_to_total():
    # Use inputs that won't hit the clamp (all moderate)
    s, bd = compute_matchup_score(24.0, 0.710, 98, None, 68.0, 20.0,
                                   pitcher_k_pct=23.0, season_fip=3.80, recent_fip=3.90)
    computed = (bd["baseline"] + bd["park_delta"] + bd["season_fip_delta"] +
                bd["offense_delta"] + bd["recent_fip_delta"] +
                bd["familiarity_delta"] + bd["temp_delta"] + bd["rain_delta"])
    assert abs(computed - bd["total"]) < 0.15  # small rounding tolerance

def test_breakdown_has_required_keys():
    s, bd = compute_matchup_score(22.0, 0.720, 100, None, None, None)
    for key in ("baseline", "park_index", "park_delta",
                "season_fip", "season_fip_delta", "offense_delta",
                "recent_fip", "recent_fip_delta",
                "days_since_faced", "familiarity_delta",
                "temp_f", "temp_delta", "rain_pct", "rain_delta", "total"):
        assert key in bd, f"Missing key: {key}"

def test_breakdown_familiarity_none_when_no_recent():
    bd = _breakdown(days=None)
    assert bd["days_since_faced"] is None
    assert bd["familiarity_delta"] == 0.0

def test_breakdown_rain_input_stored():
    bd = _breakdown(rain=75.0)
    assert bd["rain_pct"] == 75.0
    assert bd["rain_delta"] == -15.0
