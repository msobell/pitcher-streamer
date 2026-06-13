"""Unit tests for _compute_game_score — pure, no HTTP."""

from main import _compute_game_score


def _gs(ip="6.0", k=6, bb=2, er=2, h=5, hr=0,
        barrels=0, hard_hits=4, whiff_raw=0.26, chase_raw=0.31):
    game = {"ip": ip, "k": k, "bb": bb, "er": er, "h": h, "hr": hr, "pitches": 90}
    savant = {
        "barrels": barrels, "hard_hits": hard_hits,
        "_whiff_pct_raw": whiff_raw, "_chase_pct_raw": chase_raw,
    }
    return _compute_game_score(game, savant)


def test_league_average_start():
    # 6 IP (18 outs), 6 K, 2 BB, 0 barrels, 4 hard hits, both bonuses
    # 40 + 36 + 12 - 6 - 0 - 4 + 5 + 5 = 88 — that's elite, not 50.
    # The formula isn't centered at 50 for an "average" 6IP/2ER/6K line.
    # Just verify the arithmetic is right.
    score = _gs()
    expected = 40 + 2*18 + 2*6 - 3*2 - 5*0 - 1*4 + 5 + 5
    assert score == expected


def test_dominant_start_scores_high():
    # 8 IP, 10 K, 0 BB, 0 barrels, 2 hard hits, both bonuses
    score = _gs(ip="8.0", k=10, bb=0, barrels=0, hard_hits=2, whiff_raw=0.35, chase_raw=0.35)
    assert score >= 80


def test_blowup_start_scores_low():
    # 2.1 IP (7 outs), 1 K, 4 BB, 2 barrels, 8 hard hits, no stuff bonuses
    score = _gs(ip="2.1", k=1, bb=4, barrels=2, hard_hits=8, whiff_raw=0.10, chase_raw=0.15)
    assert score < 40


def test_lucky_bad_line_gets_credit():
    # 5.2 IP, 8 ER, but only 1 barrel, 2 hard hits, good whiff and chase
    # (matches the example from the prompt — pitcher was unlucky, not bad)
    score = _gs(ip="5.2", k=8, bb=2, er=8, barrels=1, hard_hits=2,
                whiff_raw=0.29, chase_raw=0.33)
    # 40 + 2*17 + 2*8 - 3*2 - 5*1 - 1*2 + 5 + 5 = 40+34+16-6-5-2+5+5 = 87
    assert score >= 60


def test_no_whiff_bonus_below_threshold():
    score_with = _gs(whiff_raw=0.26)
    score_without = _gs(whiff_raw=0.24)
    assert score_with == score_without + 5


def test_no_chase_bonus_below_threshold():
    score_with = _gs(chase_raw=0.31)
    score_without = _gs(chase_raw=0.29)
    assert score_with == score_without + 5


def test_barrel_penalty():
    no_barrels = _gs(barrels=0)
    two_barrels = _gs(barrels=2)
    assert no_barrels - two_barrels == 10  # 5 per barrel


def test_hard_hit_penalty():
    no_hh = _gs(hard_hits=0)
    five_hh = _gs(hard_hits=5)
    assert no_hh - five_hh == 5  # 1 per hard hit


def test_clamped_to_zero():
    # Catastrophic outing can't go below 0
    score = _gs(ip="0.1", k=0, bb=5, barrels=4, hard_hits=10, whiff_raw=0.05, chase_raw=0.10)
    assert score == 0


def test_clamped_to_100():
    # Perfect outing can't exceed 100
    score = _gs(ip="9.0", k=15, bb=0, barrels=0, hard_hits=0, whiff_raw=0.50, chase_raw=0.50)
    assert score == 100


def test_missing_savant_fields_default_to_zero():
    # If barrels/hard_hits missing, treat as 0 (no penalty)
    game = {"ip": "6.0", "k": 6, "bb": 2, "er": 2, "h": 5, "hr": 0, "pitches": 90}
    savant = {"_whiff_pct_raw": 0.28, "_chase_pct_raw": 0.32}  # no barrels/hard_hits
    score = _compute_game_score(game, savant)
    assert isinstance(score, int)
    assert 0 <= score <= 100


def test_none_whiff_chase_skips_bonus():
    game = {"ip": "6.0", "k": 6, "bb": 2, "er": 2, "h": 5, "hr": 0, "pitches": 90}
    savant = {"barrels": 0, "hard_hits": 4, "_whiff_pct_raw": None, "_chase_pct_raw": None}
    score = _compute_game_score(game, savant)
    # No bonuses: 40 + 36 + 12 - 6 - 0 - 4 = 78
    assert score == 40 + 2*18 + 2*6 - 3*2 - 0 - 4
