"""
Matchup scoring — Stream-o-Nator weights, z-score normalized inputs, Log5 K%.

Component weights (from Rudy Gamble's Stream-o-Nator regression on Razzball,
https://razzball.com/launch-of-stream-o-nator-the-fantasy-baseball-pitcher-streaming-tool/):
  39%  Park factor
  31%  Pitcher season-long skill (FIP)
  17%  Opposing offense (Log5 expected K%)
  14%  Pitcher recent skill (last 20 starts FIP)

Each input is z-score normalized (subtract mean, divide by σ) before applying
weights, so the weights reflect true relative contribution to score variance
rather than being distorted by differing input scales.

Population σ values are hardcoded from MLB historical distributions:
  Park index:   σ ≈ 8   (most parks 85–115)
  Season FIP:   σ ≈ 0.60 (SP range roughly 2.5–5.5)
  Recent FIP:   σ ≈ 0.80 (more variable over 20 starts)
  Log5 K%:      σ ≈ 4.0  (realistic matchup range ~14–32%)

SCALE maps one combined σ of all weighted components to ≈10 score points,
giving a realistic full range of roughly 30–70 for typical matchups.

Log5 expected K%:
  E[K%] = (B × P) / (0.84 × B × P + 0.16)
  where B = opponent K% vs handedness (decimal), P = pitcher K% (decimal)

Situational adjustments are applied as flat modifiers after the weighted score
(not part of the regression weights):
  familiarity · temperature · rain risk
"""

from __future__ import annotations

# FIP constant — approximate for 2026 (recalibrated each season to match ERA)
_FIP_CONSTANT = 3.10

# Population means
_MEAN_PARK = 100.0
_MEAN_FIP = 4.10
_MEAN_K_PCT = 22.0
_MEAN_OPS = 0.720

# Population standard deviations (MLB historical distributions)
_SIGMA_PARK = 8.0
_SIGMA_SEASON_FIP = 0.60
_SIGMA_RECENT_FIP = 0.80
_SIGMA_K_PCT = 4.0
_SIGMA_OPS = 0.060

# Stream-o-Nator weights
_W_PARK = 0.39
_W_SEASON_FIP = 0.31
_W_OFFENSE = 0.17
_W_RECENT_FIP = 0.14

# Scale factor: 1 combined weighted σ → this many score points.
# SCALE=15 means a 1σ park shift (8 index pts) contributes 0.39 × 15 ≈ 5.9 pts.
# Typical realistic range: ~30–70. Scores are clamped to [10, 90].
_SCALE = 15.0


def _calc_fip(k: int, bb: int, hr: int, ip: float) -> "float | None":
    """FIP = (13×HR + 3×BB − 2×K) / IP + FIP_constant. Returns None if IP=0."""
    if ip <= 0:
        return None
    return round((13 * hr + 3 * bb - 2 * k) / ip + _FIP_CONSTANT, 2)


def _z(value: float, mean: float, sigma: float) -> float:
    """Z-score: positive = better than average for the pitcher (signs handled per-component)."""
    return (value - mean) / sigma


def _log5_k_pct(pitcher_k_pct: float, opponent_k_pct: float) -> float:
    """
    Log5 expected K% for a pitcher vs a lineup.
      E[K%] = (B × P) / (0.84 × B × P + 0.16)
    where B = opponent K% (as decimal), P = pitcher K% (as decimal).
    """
    b = opponent_k_pct / 100
    p = pitcher_k_pct / 100
    denom = 0.84 * b * p + 0.16
    if denom == 0:
        return _MEAN_K_PCT
    return round((b * p / denom) * 100, 1)


def compute_matchup_score(
    opponent_k_pct: float,
    opponent_ops: float,
    park_index_runs: float,
    days_since_faced: "int | None",
    temp_f: "float | None",
    rain_pct: "float | None",
    pitcher_hand: str = "R",
    pitcher_k_pct: "float | None" = None,
    season_fip: "float | None" = None,
    recent_fip: "float | None" = None,
) -> tuple[float, dict]:
    """
    Returns (score, breakdown).

    Each of the four weighted components is z-scored then multiplied by its
    Stream-o-Nator weight and a common scale factor. This ensures the 39% park
    weight actually contributes 39% of score variance, regardless of each
    input's raw numeric range.
    """
    baseline = 50.0

    # --- Park factor (39%) ---
    # Lower park index = pitcher-friendly = positive z
    park_z = _z(_MEAN_PARK, park_index_runs, _SIGMA_PARK)  # flipped: mean - value
    park_delta = round(_W_PARK * park_z * _SCALE, 1)

    # --- Pitcher season FIP (31%) ---
    # Lower FIP = better pitcher = positive z
    if season_fip is not None:
        season_fip_z = _z(_MEAN_FIP, season_fip, _SIGMA_SEASON_FIP)  # flipped: mean - value
    else:
        season_fip_z = 0.0
    season_fip_delta = round(_W_SEASON_FIP * season_fip_z * _SCALE, 1)

    # --- Opponent offense (17%) ---
    if pitcher_k_pct is not None and opponent_k_pct > 0:
        expected_k = _log5_k_pct(pitcher_k_pct, opponent_k_pct)
        # Higher expected K% = better for pitcher = positive z
        offense_z = _z(expected_k, _MEAN_K_PCT, _SIGMA_K_PCT)
        offense_label = "log5_k"
    else:
        # Fallback: lower OPS = better for pitcher = positive z
        expected_k = None
        offense_z = _z(_MEAN_OPS, opponent_ops, _SIGMA_OPS)  # flipped: mean - value
        offense_label = "ops_fallback"
    offense_delta = round(_W_OFFENSE * offense_z * _SCALE, 1)

    # --- Pitcher recent FIP (14%) ---
    if recent_fip is not None:
        recent_fip_z = _z(_MEAN_FIP, recent_fip, _SIGMA_RECENT_FIP)  # flipped: mean - value
    else:
        recent_fip_z = 0.0
    recent_fip_delta = round(_W_RECENT_FIP * recent_fip_z * _SCALE, 1)

    score = baseline + park_delta + season_fip_delta + offense_delta + recent_fip_delta

    # --- Situational adjustments (flat modifiers, not in regression weights) ---
    familiarity_delta = 0.0
    if days_since_faced is not None:
        if days_since_faced <= 5:
            familiarity_delta = -12.0
        elif days_since_faced <= 9:
            familiarity_delta = -8.0
    score += familiarity_delta

    temp_delta = 0.0
    if temp_f is not None:
        if temp_f < 55:
            temp_delta = 5.0
        elif temp_f > 82:
            temp_delta = -5.0
    score += temp_delta

    rain_delta = 0.0
    if rain_pct is not None and rain_pct > 50:
        rain_delta = -15.0
    score += rain_delta
    score = max(10.0, min(90.0, score))

    breakdown = {
        "baseline": baseline,
        "pitcher_hand": pitcher_hand,
        # Park
        "park_index": park_index_runs,
        "park_z": round(park_z, 2),
        "park_delta": park_delta,
        # Season FIP
        "season_fip": season_fip,
        "season_fip_z": round(season_fip_z, 2),
        "season_fip_delta": season_fip_delta,
        # Opponent offense
        "offense_label": offense_label,
        "opponent_k_pct": round(opponent_k_pct, 1),
        "pitcher_k_pct": round(pitcher_k_pct, 1) if pitcher_k_pct is not None else None,
        "expected_k_pct": expected_k,
        "opponent_ops": round(opponent_ops, 3),
        "offense_z": round(offense_z, 2),
        "offense_delta": offense_delta,
        # Recent FIP
        "recent_fip": recent_fip,
        "recent_fip_z": round(recent_fip_z, 2),
        "recent_fip_delta": recent_fip_delta,
        # Situational
        "days_since_faced": days_since_faced,
        "familiarity_delta": familiarity_delta,
        "temp_f": temp_f,
        "temp_delta": temp_delta,
        "rain_pct": rain_pct,
        "rain_delta": rain_delta,
        "total": round(score, 1),
    }

    return round(score, 1), breakdown
