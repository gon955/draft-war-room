"""ESPN -> database sync (SPEC 2, 4).

Takes a PlayerDataSource (injected, never constructed here) and upserts what it
returns into the leagues and players tables: settings, point weights, roster
slots, and the season-scoped player pool.

Nothing here commits. The caller owns the transaction boundary, which is what
lets a route write the league row and its whole player pool in one atomic
commit — or roll both back together.

Two things this must handle, both observed against the live API:
  * ProjectionsUnavailable — ESPN publishes nothing for a season until close to
    opening night. Surface it as a clear 4xx, do not write an empty pool over a
    good one.
  * Partial projections are normal: ESPN projects a few hundred players, and the
    rest are genuinely replacement-level. Do not discard them.
"""

from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from warroom.models import League, Player, ScoringFormat
from warroom.valuation.data_source import PlayerDataSource, ProjectionsUnavailable
from warroom.valuation.domain import LeagueSettings


def _settings_to_columns(settings: LeagueSettings) -> dict[str, Any]:
    return {
        "scoring_format": ScoringFormat(settings.scoring_format),
        "num_teams": settings.num_teams,
        "roster_slots": settings.roster_slots,
        "roster_size": settings.roster_size,
        "point_weights": settings.point_weights,
    }


def sync_league_settings(db: Session, league: League, source: PlayerDataSource) -> LeagueSettings:

    settings = source.get_league_settings(league.espn_league_id, league.season)

    for col, val in _settings_to_columns(settings).items():
        setattr(league, col, val)

    return settings


def sync_player_pool(db: Session, league: League, source: PlayerDataSource) -> int:

    pool = source.get_player_pool(league.espn_league_id, league.season)

    if not pool:
        raise ProjectionsUnavailable(
            f"No player projections returned for league {league.espn_league_id} in season {league.season}."
        )
    rows = [
        {
            "espn_player_id": p.espn_player_id,
            "season": league.season,
            "name": p.name,
            "pro_team": p.pro_team,
            "positions": list(p.positions),
            "projections": dict(p.stats),
        }
        for p in pool
    ]

    stmt = insert(Player).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["espn_player_id", "season"],
        set_={
            "name": stmt.excluded.name,
            "pro_team": stmt.excluded.pro_team,
            "positions": stmt.excluded.positions,
            "projections": stmt.excluded.projections,
            # Force updated_at explicitly because standard ORM mixin hooks don't fire on Core engine upserts
            "updated_at": func.clock_timestamp(),
        },
    )

    db.execute(stmt)
    return len(rows)


def sync_league(db: Session, league: League, source: PlayerDataSource) -> int:
    """
    Orchestrates the synchronous fetch sequence for configuration updates and structural pools.

    Updates records in memory/transaction space. Does not issue an explicit database commit.
    """
    sync_league_settings(db, league, source)
    players_synced = sync_player_pool(db, league, source)
    return players_synced
