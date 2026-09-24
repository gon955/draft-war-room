"""Survival is predicted off the market's board, not off our own.

`survival_probabilities` answers "will this player still be there when I pick
again?". It used to decay each opponent's interest by a player's rank on OUR
board — which assumes the other nine managers share our valuation. They do not;
that is the whole premise of the app.

The error was not random. It was largest exactly where we disagree with the
market, which is exactly the players the recommendation tells you to target: we
rate someone 5th, the room rates them 60th, and the model reported them as
likely to be taken. Their score was docked for a risk that was not there, and
the board nudged you to reach for a player nobody else wanted.

`field_order` now re-sorts the candidates the way the ROOM sees them before the
decay is applied. Which seats each opponent still needs is read off our own
eligibility rules either way — that is roster shape, not an opinion about value.
"""

import pytest

from warroom.valuation.domain import LeagueSettings, PlayerProjection, PlayerValue
from warroom.valuation.draft import LiveValue, field_order, survival_probabilities

SETTINGS = LeagueSettings(
    scoring_format="points",
    num_teams=2,
    roster_slots={"PG": 1, "C": 1, "UTIL": 1},
    roster_size=3,
    point_weights={"pts": 1.0},
)


def live(pid: int, value: float, positions=("PG",)) -> LiveValue:
    player = PlayerProjection(
        espn_player_id=pid, name=f"P{pid}", positions=positions, stats={"pts": value}
    )
    return LiveValue(
        value=PlayerValue(player, value, 0.0, value, positions[0]),
        preseason_replacement=0.0,
        preseason_value=value,
    )


def ids(ranked):
    return [c.value.player.espn_player_id for c in ranked]


# Our board and the market's disagree completely: we like 1 best, they like 3.
OURS = [live(1, 300.0), live(2, 200.0), live(3, 100.0)]
MARKET = {3: 0, 2: 1, 1: 2}


class TestFieldOrder:
    def test_full_market_weight_uses_the_market_order(self):
        assert ids(field_order(OURS, MARKET, 1.0)) == [3, 2, 1]

    def test_zero_weight_restores_our_own_order(self):
        """The escape hatch has to actually work: a room that drafts nothing
        like ESPN should be able to turn this off."""
        assert ids(field_order(OURS, MARKET, 0.0)) == [1, 2, 3]

    def test_no_market_data_falls_back_to_our_order(self):
        """A pool synced before espn_projected_points existed yields an empty
        mapping. That must be the old behaviour, not an error."""
        assert ids(field_order(OURS, {}, 1.0)) == [1, 2, 3]
        assert ids(field_order(OURS, None, 1.0)) == [1, 2, 3]

    def test_a_half_weight_blends_the_two(self):
        # blended key: player 1 -> 0.5*0 + 0.5*2 = 1.0, player 2 -> 1.0,
        # player 3 -> 1.0. All tie, so the espn_player_id tie-break decides.
        assert ids(field_order(OURS, MARKET, 0.5)) == [1, 2, 3]

    def test_a_player_the_market_has_no_view_on_keeps_our_position(self):
        """Dropping them to the end would claim they are certain to survive,
        which is a strong statement to make out of missing data."""
        ranked = [live(1, 300.0), live(2, 200.0), live(9, 250.0)]

        assert ids(field_order(ranked, {1: 5, 2: 0}, 1.0)) == [2, 9, 1]


class TestSurvivalUsesIt:
    @pytest.fixture
    def opponents(self):
        """Two intervening picks, both by teams with every seat still open."""
        return [[], []]

    def test_a_player_we_like_and_the_market_does_not_survives_more(self, opponents):
        ours = survival_probabilities(OURS, opponents, SETTINGS, market_weight=0.0)
        market = survival_probabilities(OURS, opponents, SETTINGS, market_rank=MARKET)

        # We rank player 1 first; the market ranks them last.
        assert market[1] > ours[1]

    def test_a_player_the_market_likes_survives_less(self, opponents):
        ours = survival_probabilities(OURS, opponents, SETTINGS, market_weight=0.0)
        market = survival_probabilities(OURS, opponents, SETTINGS, market_rank=MARKET)

        assert market[3] < ours[3]

    def test_the_old_behaviour_is_still_reachable(self, opponents):
        with_market = survival_probabilities(
            OURS, opponents, SETTINGS, market_rank=MARKET, market_weight=0.0
        )
        without = survival_probabilities(OURS, opponents, SETTINGS)

        assert with_market == without

    def test_every_probability_stays_a_probability(self, opponents):
        out = survival_probabilities(OURS, opponents, SETTINGS, market_rank=MARKET)

        assert all(0.0 <= p <= 1.0 for p in out.values())

    def test_no_intervening_picks_means_everybody_survives(self):
        """You are on the clock for the last time; nothing can be taken."""
        out = survival_probabilities(OURS, [], SETTINGS, market_rank=MARKET)

        assert set(out.values()) == {1.0}

    def test_opponent_need_is_still_read_off_our_eligibility(self):
        """Re-ordering the board must not disturb WHICH players each opponent
        is shopping for — that is roster shape, the same for everyone."""
        guards = [live(1, 300.0, ("PG",)), live(2, 200.0, ("PG",))]
        centre = [live(3, 250.0, ("C",))]
        ranked = guards + centre
        # One opponent whose only open seat is C (PG and UTIL already filled).
        roster = [
            PlayerProjection(espn_player_id=90, name="g", positions=("PG",), stats={}),
            PlayerProjection(espn_player_id=91, name="u", positions=("PG",), stats={}),
        ]

        out = survival_probabilities(ranked, [roster], SETTINGS, market_rank={1: 0, 2: 1, 3: 2})

        # The centre is the only player fitting their open seat, so they are
        # the one at risk however the market ranks them.
        assert out[3] < out[1]
