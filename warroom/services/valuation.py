"""Run the valuation engine and cache its output (SPEC 4, 5).

Loads the league's settings and cached player pool, maps rows to the engine's
domain objects, calls value_over_replacement, and upserts the valuations table
(projected_points, replacement_points, value, assigned_slot, computed_at).

The engine itself stays pure and framework-free — this is the only place that
knows both it and the DB.

Three things decided in advance, each of them a trap already paid for elsewhere:

  * REFUSE A CATEGORIES LEAGUE with a 422 before calling the engine. SPEC 3's
    scoring_format enum admits both formats and POST /leagues will happily store
    a categories league, but value_over_replacement raises ValueError on one by
    design (SPEC 5.2). Unhandled, that ValueError is a 500 on a request the user
    got wrong, not a request that broke.

  * NAME computed_at IN THE UPSERT'S on_conflict_do_update set_. A Core upsert
    does not re-apply a server default on the update path, so an omitted
    computed_at pins every row at its first-ever compute and a freshly
    recomputed cache reads as permanently stale. services.sync already had to
    do exactly this for players.updated_at.

  * SELECT THE POOL BY league.season, not by "every player row". players is
    season-scoped shared reference data (SPEC 0.2), so a database holding two
    seasons would otherwise value them as one pool and wreck every replacement
    level.
"""

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from warroom.models import League, Player, ScoringFormat, Valuation
from warroom.valuation.domain import LeagueSettings, PlayerProjection
from warroom.valuation.engine import value_over_replacement


class NotAPointsLeague(ValueError):
    """A categories league, which value_over_replacement refuses (SPEC 5.2).

    Raised here rather than letting the engine's ValueError escape, so the
    route can answer 422 instead of 500 — the request was wrong, not broken.
    """


class EmptyPlayerPool(RuntimeError):
    """No cached players for this league's season: sync before valuing.

    Also what keeps the upsert below safe — insert().values([]) is a SQL syntax
    error, so this guard is load-bearing and not merely a nicety.
    """


def settings_for(league: League) -> LeagueSettings:
    """The League row as the engine's framework-free settings object.

    scoring_format needs .value. The column is a ScoringFormat enum and the
    engine compares against the literal string "points", so handing it the enum
    member makes value_over_replacement raise for a perfectly good points
    league — a failure that reads like bad data and is not.
    """
    return LeagueSettings(
        scoring_format=league.scoring_format.value,
        num_teams=league.num_teams,
        roster_slots=league.roster_slots,
        point_weights=league.point_weights,
        roster_size=league.roster_size,
    )


def to_projection(player: Player) -> PlayerProjection:
    """A players row as engine input.

    positions becomes a tuple to match the frozen dataclass; a list would work
    today because the engine only ever reads it, but the contract says tuple.
    """
    return PlayerProjection(
        espn_player_id=player.espn_player_id,
        name=player.name,
        positions=tuple(player.positions),
        stats=player.projections,
        pro_team=player.pro_team,
    )


def compute_valuations(db: Session, league: League) -> int:
    """Value the league's whole cached pool and upsert the results.

    Nothing here commits. The caller owns the transaction boundary, same as
    services.sync, which is what lets the upsert and the stale-row delete land
    together or not at all.
    """
    if league.scoring_format is not ScoringFormat.POINTS:
        raise NotAPointsLeague(
            "Valuation is value over replacement, which is points-league only; "
            f"this league is scored by {league.scoring_format.value}."
        )

    pool = list(db.scalars(select(Player).where(Player.season == league.season)))
    if not pool:
        raise EmptyPlayerPool(
            f"No cached players for season {league.season}. Sync the league first."
        )

    # THE mapping that matters. The engine speaks espn_player_id end to end —
    # PlayerValue.player.espn_player_id is its only identifier — while
    # valuations.player_id is the players row UUID. unique(espn_player_id,
    # season) plus the season filter above is what makes this dict total.
    row_id_by_espn_id = {p.espn_player_id: p.id for p in pool}

    results = value_over_replacement([to_projection(p) for p in pool], settings_for(league))

    rows = [
        {
            "league_id": league.id,
            "player_id": row_id_by_espn_id[result.player.espn_player_id],
            "projected_points": result.projected_points,
            "replacement_points": result.replacement_points,
            "value": result.value,
            "assigned_slot": result.assigned_slot,
        }
        for result in results
    ]

    stmt = insert(Valuation).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["league_id", "player_id"],
        set_={
            "projected_points": stmt.excluded.projected_points,
            "replacement_points": stmt.excluded.replacement_points,
            "value": stmt.excluded.value,
            "assigned_slot": stmt.excluded.assigned_slot,
            # Both, explicitly. A Core upsert re-applies no server default on
            # the UPDATE path, so omitting these pins every row at its
            # first-ever compute and a freshly recomputed cache reads as
            # permanently stale. services.sync does the same for players.
            "computed_at": func.clock_timestamp(),
            "updated_at": func.clock_timestamp(),
        },
    )
    db.execute(stmt)

    # Belt and braces, and deliberately so. No path reaches this today: an ORM
    # delete of a players row fails loudly (Player.valuations carries no delete
    # cascade, by design — models.py), a DB-level delete cascades the valuation
    # away with it, and sync never rewrites players.season. What this buys is
    # that "a recompute leaves exactly the current pool valued" holds by
    # construction, rather than by an argument about what else in the codebase
    # does and does not mutate a player's season.
    db.execute(
        delete(Valuation).where(
            Valuation.league_id == league.id,
            Valuation.player_id.not_in([row["player_id"] for row in rows]),
        )
    )

    return len(rows)
