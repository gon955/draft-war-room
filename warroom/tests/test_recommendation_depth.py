"""Bench rounds must respect what you already have at each position.

The bug this is written against, reported from a real 10-team league: a manager
holding five centres kept being recommended more centres.

The cause was two defects compounding, both of which only appear once your
starting lineup is full:

  1. lineup_values_by_mask returns the best lineup over any SUBSET of seats,
     and the caller took max(). A value is measured over a replacement level
     that rises all draft, so by the middle rounds real starters price
     negative — and a negative starter is "better left out", so the optimiser
     reported their seat as EMPTY. Every candidate eligible for that seat then
     looked like they added their whole value rather than the difference over
     the incumbent. A fantasy lineup cannot leave a seat empty: an empty seat
     scores nothing and a negative-VALUE player still scores positive POINTS.

  2. marginal_values clamps at zero, so once the lineup is full every
     candidate reads 0.0 and the sort fell through to league-wide live value —
     a number that knows nothing about your roster. In a league scoring
     oreb/dreb/blk that is centres, for ever, however many you have.

`lineup_delta` is the fix for the second: the same quantity unclamped, which
still orders a full lineup by how far each candidate sits below the starter
they would have to displace.
"""

import pytest

from warroom.models import Board, League, MockDraft, MockPick, Player, ScoringFormat, User
from warroom.services.recommendation import recommend
from warroom.tests.conftest import SEASON

# One PG seat, one C seat, one flex, and FOUR teams. The team count matters:
# with two, league demand at each slot drops to one once my roster consumes a
# seat, so the replacement level lands exactly on the best available player
# there — every candidate then prices at 0.0 and the fixture cannot tell the
# two orderings apart. Four teams leaves demand of three, so replacement is set
# by the filler below and the spares carry real values.
SLOTS = {"PG": 1, "C": 1, "UTIL": 1}

# Centres are the high scorers, exactly as in the real league where rebounding
# carries the scoring. A board sorted on league-wide value therefore offers a
# centre for ever; a board that knows your roster should not.
#
# Hand-computed, so a change in the maths surfaces as a failure here rather
# than as a number nobody can check. Replacement lands on the 3rd-best
# available at each slot: PG 180, C 280, UTIL 300.
#
#   player          raw    live value        best lineup delta
#   Spare Guard     480    480-180 = 300     -20   (displaces My Guard)
#   Spare Centre    800    800-280 = 520    -150   (displaces My Centre B)
#
# League value prefers the centre, 520 to 300. The roster-aware number prefers
# the guard, -20 to -150. That disagreement is the whole test.
POOL = [
    (1, "My Centre A", ("C",), 1000.0),
    (2, "My Centre B", ("C",), 950.0),
    (3, "My Guard", ("PG",), 500.0),
    (4, "Spare Centre", ("C",), 800.0),
    (5, "Spare Guard", ("PG",), 480.0),
    (6, "Filler Centre 1", ("C",), 300.0),
    (7, "Filler Centre 2", ("C",), 280.0),
    (8, "Filler Centre 3", ("C",), 260.0),
    (9, "Filler Guard 1", ("PG",), 200.0),
    (10, "Filler Guard 2", ("PG",), 180.0),
    (11, "Filler Guard 3", ("PG",), 160.0),
]


@pytest.fixture
def depth_players(db) -> list[Player]:
    rows = [
        Player(
            espn_player_id=pid,
            season=SEASON,
            name=name,
            pro_team="BOS",
            positions=list(positions),
            projections={"pts": pts},
        )
        for pid, name, positions, pts in POOL
    ]
    db.add_all(rows)
    db.commit()
    return rows


@pytest.fixture
def depth_league(db, user_a: User) -> League:
    row = League(
        user_id=user_a.id,
        espn_league_id=778899,
        season=SEASON,
        name="Depth league",
        scoring_format=ScoringFormat.POINTS,
        num_teams=4,
        roster_size=3,
        roster_slots=SLOTS,
        point_weights={"pts": 1.0},
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def full_lineup_mock(db, user_a, depth_league, depth_players, client, auth_a) -> MockDraft:
    """My three seats filled with two centres and a guard.

    PG and UTIL and C are all occupied, so every remaining pick is bench depth
    and no candidate can improve the starting lineup.
    """
    client.post(f"/leagues/{depth_league.id}/valuations/compute", headers=auth_a)

    board = Board(user_id=user_a.id, league_id=depth_league.id, name="b")
    db.add(board)
    db.commit()
    mock = MockDraft(board_id=board.id, name="m", my_draft_slot=1)
    db.add(mock)
    db.commit()

    by_espn = {p.espn_player_id: p for p in depth_players}
    picks = []
    teams = 4
    for n in range(1, teams * 3 + 1):
        rnd = (n - 1) // teams + 1
        pos = (n - 1) % teams + 1
        slot = pos if rnd % 2 else teams - pos + 1
        picks.append(
            MockPick(
                mock_draft_id=mock.id,
                pick_number=n,
                round=rnd,
                team_slot=slot,
                is_mine=slot == 1,
                player_id=None,
            )
        )
    db.add_all(picks)
    db.commit()

    # Fill MY three seats: two centres and one guard.
    mine = [p for p in picks if p.is_mine][:3]
    for pick, espn_id in zip(mine, (1, 2, 3)):
        pick.player_id = by_espn[espn_id].id
    db.commit()
    return mock


def by_name(results):
    return {r.player.name: r for r in results}


class TestAFullLineupIsRankedByYourRoster:
    def test_every_candidate_is_bench_depth(self, db, full_lineup_mock):
        """Premise check. If anything here could still start, the rest of this
        file is testing the wrong situation."""
        results, _ = recommend(db, full_lineup_mock)

        assert all(r.marginal == 0.0 for r in results)
        assert all(r.score == 0.0 for r in results)

    def test_the_spare_guard_outranks_the_better_spare_centre(self, db, full_lineup_mock):
        """THE bug. Spare Centre prices at 520 and Spare Guard at 300, so a
        board ordered on league-wide value offers the centre — to a manager
        whose centre seats are both full and who is one deep at guard."""
        results, _ = recommend(db, full_lineup_mock)

        order = [r.player.name for r in sorted(results, key=lambda r: -r.lineup_delta)]
        assert order.index("Spare Guard") < order.index("Spare Centre")

    def test_the_league_wide_order_would_have_said_the_opposite(self, db, full_lineup_mock):
        """Guards the test above against passing for the wrong reason: it is
        only meaningful while league value really does prefer the centre."""
        results, _ = recommend(db, full_lineup_mock)
        found = by_name(results)

        assert found["Spare Centre"].live.value.value > found["Spare Guard"].live.value.value

    def test_the_delta_measures_the_gap_to_the_displaced_starter(self, db, full_lineup_mock):
        """Spare Guard is 20 short of the starter he would displace; Spare
        Centre is 150 short of his. Both are bench depth, and one is far
        closer to being useful than the other."""
        results, _ = recommend(db, full_lineup_mock)
        found = by_name(results)

        assert found["Spare Guard"].lineup_delta < 0
        assert found["Spare Centre"].lineup_delta < 0
        assert found["Spare Guard"].lineup_delta > found["Spare Centre"].lineup_delta

    def test_the_api_exposes_the_number_behind_the_order(self, client, auth_a, full_lineup_mock):
        r = client.get(f"/mocks/{full_lineup_mock.id}/recommendation", headers=auth_a)

        assert r.status_code == 200
        items = r.json()["items"]
        assert items[0]["player"]["name"] == "Spare Guard"
        deltas = [i["lineup_delta"] for i in items]
        assert deltas == sorted(deltas, reverse=True)


class TestSeatsAreNotLeftEmpty:
    def test_a_negative_starter_still_occupies_their_seat(self, db, full_lineup_mock):
        """The first defect, stated directly. If the optimiser drops a starter
        whose value has gone negative, the seat reads empty and every eligible
        candidate is credited with their whole value instead of the difference
        — which is how a sixth centre came to look as good as a first."""
        results, _ = recommend(db, full_lineup_mock)

        # Nobody left can start, so no delta may be positive. A seat wrongly
        # reported empty shows up here immediately as a positive delta.
        assert all(r.lineup_delta <= 0.0 for r in results)
