"""Seed a demo league and player pool without touching ESPN.

The dev console (`/dev`) needs a league with a player pool before anything
interesting happens, and `POST /leagues` calls the real ESPN API. This fills
the same tables through the same service — services.sync, fed by
FakePlayerDataSource — so what lands is what a real sync would land, minus the
network and the credentials.

    python scripts/seed_demo.py --email you@example.com

Register that email in the console first; this attaches the league to an
existing user rather than inventing one, so there is no password to guess.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from warroom.db import SessionLocal
from warroom.models import League, ScoringFormat, User
from warroom.services import sync as sync_service
from warroom.valuation.data_source import FakePlayerDataSource
from warroom.valuation.domain import LeagueSettings, PlayerProjection

# Weights shaped like a real ESPN points league: made and missed shots are
# scored and raw points are not, which is exactly why the engine reads the
# league's own weights instead of hardcoding pts=1.0 (SPEC 2.2).
POINT_WEIGHTS = {
    "fgm": 2.0,
    "fgmi": -0.5,
    "ftm": 1.0,
    "ftmi": -0.5,
    "3pm": 3.0,
    "reb": 1.2,
    "ast": 1.5,
    "stl": 3.0,
    "blk": 3.0,
    "to": -1.0,
}
ROSTER_SLOTS = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UTIL": 3}

FIRST = [
    "Nik",
    "Shai",
    "Luka",
    "Jay",
    "Dev",
    "Ant",
    "Tyr",
    "Dom",
    "Bam",
    "Cade",
    "Trae",
    "Zion",
    "Alp",
    "Mik",
    "Des",
    "Fran",
    "Paolo",
    "Jal",
    "Evan",
    "Scoot",
    "Jos",
    "Vic",
    "Chet",
    "Amen",
]
LAST = [
    "Jokis",
    "Gilge",
    "Doncet",
    "Tatem",
    "Booke",
    "Edwar",
    "Halib",
    "Sabon",
    "Adeba",
    "Cunni",
    "Youngs",
    "Willi",
    "Sheng",
    "Bridg",
    "Hayes",
    "Wemby",
    "Banch",
    "Bruns",
    "Mobly",
    "Hendr",
    "Giddy",
    "Marka",
    "Caste",
    "Risch",
]
TEAMS = ["BOS", "DEN", "LAL", "OKC", "NYK", "MIL", "PHI", "MIN", "CLE", "DAL"]
# Cycled so the pool has singles, guards, forwards and bigs in realistic mix.
POSITIONS = [("PG",), ("SG",), ("SF",), ("PF",), ("C",), ("PG", "SG"), ("SF", "PF"), ("PF", "C")]


def make_pool(count: int) -> list[PlayerProjection]:
    """A pool deep enough that every starting slot reaches a replacement level."""
    pool = []
    for i in range(count):
        # Convex decay: steepest at the top, like a real projection curve.
        strength = 1.0 - (i / count) ** 0.65
        pool.append(
            PlayerProjection(
                espn_player_id=100_000 + i,
                name=f"{FIRST[i % len(FIRST)]} {LAST[(i * 7) % len(LAST)]}"
                + (f" {i // len(FIRST)}" if i >= len(FIRST) else ""),
                positions=POSITIONS[i % len(POSITIONS)],
                pro_team=TEAMS[i % len(TEAMS)],
                stats={
                    "fgm": round(300 + 450 * strength, 1),
                    "fgmi": round(300 + 380 * strength, 1),
                    "ftm": round(90 + 380 * strength, 1),
                    "ftmi": round(40 + 90 * strength, 1),
                    "3pm": round(40 + 220 * strength, 1),
                    "reb": round(180 + 720 * strength, 1),
                    "ast": round(90 + 620 * strength, 1),
                    "stl": round(35 + 105 * strength, 1),
                    "blk": round(20 + 160 * strength, 1),
                    "to": round(70 + 230 * strength, 1),
                },
            )
        )
    return pool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="an account you already registered")
    parser.add_argument("--season", type=int, default=2027)
    parser.add_argument("--teams", type=int, default=10)
    parser.add_argument("--roster-size", type=int, default=13)
    parser.add_argument("--players", type=int, default=150)
    parser.add_argument("--name", default="Demo League")
    parser.add_argument("--espn-league-id", type=int, default=419087)
    args = parser.parse_args()

    settings = LeagueSettings(
        scoring_format="points",
        num_teams=args.teams,
        roster_slots=ROSTER_SLOTS,
        point_weights=POINT_WEIGHTS,
        roster_size=args.roster_size,
    )
    source = FakePlayerDataSource(settings=settings, players=make_pool(args.players))

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == args.email.lower()))
        if user is None:
            print(
                f"No account for {args.email!r}. Register it at /dev first — this "
                f"attaches a league to an existing user rather than inventing one.",
                file=sys.stderr,
            )
            return 1

        league = db.scalar(
            select(League).where(
                League.user_id == user.id,
                League.espn_league_id == args.espn_league_id,
                League.season == args.season,
            )
        )
        if league is None:
            league = League(
                user_id=user.id,
                espn_league_id=args.espn_league_id,
                season=args.season,
                name=args.name,
                scoring_format=ScoringFormat.POINTS,
                num_teams=args.teams,
                roster_size=args.roster_size,
                roster_slots=ROSTER_SLOTS,
                point_weights=POINT_WEIGHTS,
            )
            db.add(league)
            verb = "created"
        else:
            verb = "refreshed"

        # The same call POST /leagues/{id}/sync makes, so the rows are identical
        # to a real sync's — only the data source differs.
        synced = sync_service.sync_league(db, league, source)
        db.commit()

        print(f"{verb} league {league.name!r} ({league.id}) for {user.email}")
        print(f"  {args.teams} teams · roster {args.roster_size} · season {args.season}")
        print(f"  {synced} players in the pool")
        print("\nNow, in the console: pick the league, hit 'compute valuations'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
