"""Pull completed seasons from ESPN once and cache them for calibration.

Every constant in valuation/stats.py is measured against ESPN's projections
versus what then happened. This is the one script in the calibration set that
touches the network; everything else reads the cache it writes, so a
measurement can be rerun, and argued with, without ESPN being up or unchanged.

    python -m scripts.calibration.fetch                 # 2024-2026, league 19048
    python -m scripts.calibration.fetch --seasons 2026 --refresh

Reads ESPN_S2 / ESPN_SWID from the environment or .env. They are needed for a
private league and are never written to the cache.

One file per season, scripts/calibration/data/<season>.json:

    settings    that season's scoring weights and slots — a past season's
                applied totals are scored under THAT season's rules, which
                need not be this one's
    players     per player: projected and actual stat lines (lowercase codes,
                same as the app), ESPN's projected and actual fantasy totals,
                positions, and the prior seasons' actuals exactly as sync
                stores them in players.history

Players come from the same pool the app syncs — the top free agents plus every
rostered player — so what is measured is what gets valued.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from warroom.valuation.data_source import (
    FREE_AGENT_POOL_SIZE,
    EspnDataSource,
    actual_stats_of,
    positions_of,
    projected_stats_of,
)

DATA = Path(__file__).parent / "data"
LEAGUE_ID = 19048
SEASONS = (2024, 2025, 2026)


def _load_dotenv(path: Path) -> None:
    """Just enough .env parsing for two cookies, without a new dependency.

    Values already in the environment win, so an explicit export overrides
    the file.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _applied(player_stats: dict[str, Any], split: str) -> float | None:
    value = player_stats.get(split, {}).get("applied_total")
    return float(value) if isinstance(value, (int, float)) else None


def fetch_season(source: EspnDataSource, season: int) -> dict[str, Any]:
    league = source._league(LEAGUE_ID, season)
    settings = source.get_league_settings(LEAGUE_ID, season)

    pool = list(league.free_agents(size=FREE_AGENT_POOL_SIZE))
    pool += [p for team in league.teams for p in team.roster]
    unique = {p.playerId: p for p in pool}

    history = source.get_player_history(LEAGUE_ID, season, list(unique))

    players = []
    for pid, p in unique.items():
        players.append(
            {
                "id": pid,
                "name": p.name,
                "positions": list(positions_of(p.eligibleSlots, getattr(p, "position", ""))),
                "projected": projected_stats_of(p.stats, season),
                "actual": actual_stats_of(p.stats, season),
                "projected_points": _applied(p.stats, f"{season}_projected"),
                "actual_points": _applied(p.stats, f"{season}_total"),
                # String keys, exactly as players.history stores them, so
                # anything built here reads production rows unchanged.
                "history": {str(s): line for s, line in history[pid].items()},
            }
        )

    return {
        "season": season,
        "league_id": LEAGUE_ID,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "settings": {
            "point_weights": settings.point_weights,
            "roster_slots": settings.roster_slots,
            "num_teams": settings.num_teams,
        },
        "players": players,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seasons", type=int, nargs="+", default=list(SEASONS))
    parser.add_argument("--refresh", action="store_true", help="refetch seasons already cached")
    args = parser.parse_args()

    _load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    source = EspnDataSource(
        espn_s2=os.environ.get("ESPN_S2"), swid=os.environ.get("ESPN_SWID"), timeout=30.0
    )

    DATA.mkdir(exist_ok=True)
    for season in args.seasons:
        path = DATA / f"{season}.json"
        if path.exists() and not args.refresh:
            print(f"{season}: cached at {path} (--refresh to refetch)")
            continue
        started = time.monotonic()
        data = fetch_season(source, season)
        path.write_text(json.dumps(data, indent=1, sort_keys=True))
        played = sum(1 for p in data["players"] if p["actual"].get("gp"))
        print(
            f"{season}: {len(data['players'])} players, {played} with games played "
            f"-> {path} ({time.monotonic() - started:.1f}s)"
        )


if __name__ == "__main__":
    main()
