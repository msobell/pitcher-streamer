"""
Team rotation builder.

build_team_rotation(team_id, candidates, recent_starts_cache, as_of_date)
  → ordered list of {pitcher_id, name, last_start, slot, next_eligible}

Reconstructs rotation order from trailing game logs already in the cache.
Standard slot = 5 days (4 days rest). Staleness cutoff = 8 days — pitchers
not seen in 8 days are excluded as injured/optioned.
"""

from __future__ import annotations

from datetime import date, timedelta


_ROTATION_REST_DAYS = 4  # pitch every 5th day
_STALENESS_CUTOFF_DAYS = 8


def build_team_rotation(
    team_id: int,
    candidates: "list[dict]",
    recent_starts_cache: "dict[int, list[dict]]",
    as_of_date: "date | None" = None,
) -> list[dict]:
    """
    Build rotation order for a team from cached game logs.

    candidates: all confirmed starters (passed starter gate) for this team.
    recent_starts_cache: {mlbam_id: [{date, opponent_name}, ...]}
    Returns list sorted by last_start desc (slot 1 = most recently used),
    excluding pitchers whose last start is > STALENESS_CUTOFF_DAYS ago.
    """
    if as_of_date is None:
        as_of_date = date.today()

    rotation = []
    for p in candidates:
        if p.get("team_id") != team_id:
            continue
        mlbam_id = p.get("mlbam_id")
        if not mlbam_id:
            continue

        starts = recent_starts_cache.get(mlbam_id, [])
        if not starts:
            continue

        last_start_str = max((s["date"] for s in starts), default=None)
        if not last_start_str:
            continue

        try:
            last_start = date.fromisoformat(last_start_str)
        except ValueError:
            continue

        days_since = (as_of_date - last_start).days
        if days_since > _STALENESS_CUTOFF_DAYS:
            continue

        next_eligible = last_start + timedelta(days=_ROTATION_REST_DAYS + 1)

        rotation.append({
            "pitcher_id": mlbam_id,
            "name": p.get("name", ""),
            "last_start": last_start_str,
            "days_since_last_start": days_since,
            "next_eligible": next_eligible.isoformat(),
            "slot": None,  # assigned below after sorting
        })

    rotation.sort(key=lambda r: r["last_start"], reverse=True)
    for i, entry in enumerate(rotation):
        entry["slot"] = i + 1

    return rotation


def _compute_next_start_with_offdays(
    last_start: date,
    team_game_dates: "set[date]",
) -> date:
    """
    Project next start using 5-day cadence with off-day shifting.
    For each day between last_start and the baseline next start (last_start + 5)
    where the team has no game, add one extra day to the projection.
    """
    baseline = last_start + timedelta(days=_ROTATION_REST_DAYS + 1)
    if not team_game_dates:
        return baseline
    # Count off-days between last_start (exclusive) and baseline (exclusive).
    # team_game_dates only covers the viewed week, while last_start is often in
    # the prior week — days outside the known window are unknown, not off-days,
    # so only days within [min, max] of the schedule window can count.
    window_start = min(team_game_dates)
    window_end = max(team_game_dates)
    off_days = 0
    check = last_start + timedelta(days=1)
    while check < baseline:
        if window_start <= check <= window_end and check not in team_game_dates:
            off_days += 1
        check += timedelta(days=1)
    return baseline + timedelta(days=off_days)


def project_probable_pitchers(
    team_id: int,
    week_games: "list[dict]",
    rotation: "list[dict]",
    confirmed_probable_ids: "set[int]",
    as_of_date: "date | None" = None,
    unavailable_ids: "set[int] | None" = None,
) -> "dict[int, dict]":
    """
    For each game this week where no probable is confirmed, project which
    rotation pitcher is likely to start.

    Uses 5-day cadence with off-day shifting (aai): for each off-day between
    a pitcher's last start and their baseline next start, the projection shifts
    forward one day. Doubleheaders are handled independently per game_pk.

    week_games: all games for this team (raw schedule rows: {date, game_pk}).
    rotation: output of build_team_rotation — ordered by last_start desc.
    confirmed_probable_ids: set of mlbam_ids already confirmed as probables.
    Returns {game_pk: {pitcher_id, name, slot, confidence='PROJECTED'}}.
    """
    if as_of_date is None:
        as_of_date = date.today()

    if not rotation:
        return {}

    unavailable_ids = unavailable_ids or set()

    # Build the set of dates this team has a game (for off-day shift calculation)
    team_game_dates: set[date] = set()
    for g in week_games:
        try:
            team_game_dates.add(date.fromisoformat(g["date"]))
        except (ValueError, KeyError):
            pass

    projections: dict[int, dict] = {}

    # Build a map of confirmed game dates per pitcher so we can advance their
    # last_start to account for a confirmed start earlier this week when
    # projecting a potential second start.
    confirmed_game_date: dict[int, date] = {}
    for g in week_games:
        pk = g.get("game_pk")
        if not pk:
            continue
        try:
            gdate = date.fromisoformat(g["date"])
        except (ValueError, KeyError):
            continue
        home_id = g.get("probable_home_id")
        away_id = g.get("probable_away_id")
        for pid in (home_id, away_id):
            if pid and pid in confirmed_probable_ids:
                existing = confirmed_game_date.get(pid)
                if existing is None or gdate > existing:
                    confirmed_game_date[pid] = gdate

    # Build available list: include confirmed probables (they may start twice),
    # but advance their last_start to their confirmed game date so the 5-day
    # cadence is calculated from that start, not their last logged start.
    available = []
    for r in rotation:
        if r["pitcher_id"] in unavailable_ids:
            continue
        entry = dict(r)
        confirmed = confirmed_game_date.get(r["pitcher_id"])
        if confirmed is not None:
            # Advance last_start to the confirmed game so second-start projection
            # is based on the right date. days_since is 0 on the day itself.
            entry = dict(r)
            entry["last_start"] = confirmed.isoformat()
            entry["days_since_last_start"] = (as_of_date - confirmed).days
        available.append(entry)

    # Open games: this team's slot has no confirmed probable yet. Games where
    # the team's starter is already announced must be excluded — otherwise a
    # projection gets "spent" on a game that doesn't need one, starving later
    # genuinely-open games of that pitcher.
    def _team_slot_confirmed(g: dict) -> bool:
        if g.get("home_team_id") == team_id:
            return bool(g.get("probable_home_id"))
        if g.get("away_team_id") == team_id:
            return bool(g.get("probable_away_id"))
        return False

    open_games = sorted(
        [g for g in week_games if g.get("game_pk") and not _team_slot_confirmed(g)],
        key=lambda g: g["date"],
    )

    # Track which pitcher fills each open slot — a pitcher can appear at most
    # once in projections (one projected start per week beyond their confirmed).
    assigned_this_week: set[int] = set()

    for game in open_games:
        pk = game.get("game_pk")
        game_date_str = game.get("date", "")
        try:
            game_date = date.fromisoformat(game_date_str)
        except ValueError:
            continue

        # Find the best matching pitcher: project each available pitcher's next start,
        # pick the one whose projected date is closest to (and not after) game_date.
        best_match = None
        best_delta = None

        for entry in available:
            if entry["pitcher_id"] in assigned_this_week:
                continue
            try:
                last_start = date.fromisoformat(entry["last_start"])
            except ValueError:
                continue
            projected = _compute_next_start_with_offdays(last_start, team_game_dates)
            delta = (game_date - projected).days
            # Accept if projected falls on or before the game date (pitcher is due or overdue)
            if delta >= 0 and (best_delta is None or delta < best_delta):
                best_match = entry
                best_delta = delta

        if best_match is not None:
            projections[pk] = {
                "pitcher_id": best_match["pitcher_id"],
                "name": best_match["name"],
                "slot": best_match["slot"],
                "confidence": "PROJECTED",
            }
            assigned_this_week.add(best_match["pitcher_id"])

    return projections
