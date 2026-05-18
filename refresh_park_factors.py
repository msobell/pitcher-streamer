"""
Run once per season to refresh park_factors.json from Baseball Savant.

Usage:
    python refresh_park_factors.py

Reads BASEBALL_SAVANT_YEAR from env (set in .envrc). Writes park_factors.json
in the current directory. The app startup will fail if this file is missing or
its season doesn't match config.yaml.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

_SAVANT_URL = "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_park_factors(year: int) -> dict[str, dict]:
    resp = httpx.get(
        _SAVANT_URL,
        params={"type": "year", "year": year},
        headers=_HEADERS,
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()

    match = re.search(r"var data\s*=\s*(\[[\s\S]*?\]);", resp.text)
    if not match:
        raise RuntimeError("Could not find 'var data' JSON block in Baseball Savant page")

    rows = json.loads(match.group(1))
    if not rows:
        raise RuntimeError("Baseball Savant returned an empty data array")

    teams: dict[str, dict] = {}
    for row in rows:
        name = row.get("name_display_club") or row.get("team_name", "")
        if not name:
            continue
        teams[name] = {
            "index_runs": row.get("index_runs", 100),
            "index_hr": row.get("index_hr", 100),
            "index_woba": row.get("index_woba", 100),
            "index_so": row.get("index_so", 100),
            "venue_name": row.get("venue_name", ""),
        }

    return teams


def main() -> None:
    year_str = os.environ.get("BASEBALL_SAVANT_YEAR", "")
    if not year_str:
        print("ERROR: BASEBALL_SAVANT_YEAR env var not set. Set it in .envrc.")
        sys.exit(1)

    year = int(year_str)
    print(f"Fetching park factors for {year} from Baseball Savant...")

    try:
        teams = fetch_park_factors(year)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    out = {
        "season": year,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "teams": teams,
    }

    out_path = Path("park_factors.json")
    out_path.write_text(json.dumps(out, indent=2))
    print(f"Wrote {len(teams)} teams to {out_path}")


if __name__ == "__main__":
    main()
