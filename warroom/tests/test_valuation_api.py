"""The engine wired to HTTP (SPEC 6).

The conftest fixtures ARE the SPEC 5.3 worked example — `players` seeds the
seven-player pool and `league` carries num_teams=2, PG:1/C:1/UTIL:1 and
pts:1.0 — so every number asserted here is hand-computed in the spec rather
than read back off the implementation:

    espn_id   4     1     2     5     3     6     7
    value    25    10     0     0   -10   -10   -20
    slot      C    PG    PG     C    PG     C   UTIL

Three of these tests exist because the bug they catch is invisible otherwise.

GET /players outer-joins valuations, and the league_id predicate has to sit in
the ON clause: in the WHERE it discards every NULL-valuation row, so the join
quietly becomes an inner one and a synced-but-unvalued pool returns an empty
page while still answering 200.

Recompute has to advance computed_at. A Core upsert re-applies no server
default on the UPDATE path, so an omitted column pins every row at its
first-ever compute and a fresh cache reads as permanently stale — the row count
stays right and the endpoint still answers 200.

And value ties are real: two players on the same replacement level are both
exactly 0.0. Ordering by value alone is non-deterministic, so the reads break
ties on espn_player_id, which is also what makes them reproduce SPEC 5.3
exactly rather than approximately.
"""

import uuid

import pytest
from sqlalchemy import func, select

from warroom.models import League, Player, ScoringFormat, Valuation
from warroom.tests.conftest import SEASON, SPEC_53_EXPECTED_RANKING

SPEC_53_VALUES = [25.0, 10.0, 0.0, 0.0, -10.0, -10.0, -20.0]
SPEC_53_SLOTS = ["C", "PG", "PG", "C", "PG", "C", "UTIL"]


def compute(client, league, headers):
    return client.post(f"/leagues/{league.id}/valuations/compute", headers=headers)


def espn_ids(players):
    """players row id -> espn_player_id, for reading responses back."""
    return {str(p.id): p.espn_player_id for p in players}


def valuation_order(client, league, headers, players):
    body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=headers).json()
    ids = espn_ids(players)
    return [ids[v["player_id"]] for v in body["items"]]


def player_page(client, league, headers, query=""):
    return client.get(f"/leagues/{league.id}/players?limit=200{query}", headers=headers).json()


@pytest.fixture
def categories_league(db, user_a):
    """A stored categories league — SPEC 3's enum admits one, the engine refuses it."""
    row = League(
        user_id=user_a.id,
        espn_league_id=90210,
        season=SEASON,
        name="Category League",
        scoring_format=ScoringFormat.CATEGORIES,
        num_teams=10,
        roster_size=13,
        roster_slots={"PG": 1},
        point_weights={},
    )
    db.add(row)
    db.commit()
    return row


class TestCompute:
    def test_compute_reports_what_it_valued(self, client, auth_a, league, players):
        r = compute(client, league, auth_a)

        assert r.status_code == 200
        assert r.json()["players_valued"] == len(players)

    def test_one_row_per_pool_player(self, client, db, auth_a, league, players):
        compute(client, league, auth_a)

        assert db.scalar(select(func.count()).select_from(Valuation)) == len(players)

    def test_the_ranking_matches_the_spec_worked_example(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        assert valuation_order(client, league, auth_a, players) == SPEC_53_EXPECTED_RANKING

    def test_the_values_match_the_spec_worked_example(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()

        assert [v["value"] for v in body["items"]] == SPEC_53_VALUES

    def test_players_are_credited_at_their_scarcest_slot(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()

        assert [v["assigned_slot"] for v in body["items"]] == SPEC_53_SLOTS

    def test_the_scarce_centre_outranks_the_deeper_guard(self, client, auth_a, league, players):
        """The entire claim the feature rests on (SPEC 5).

        Cal Center projects 45 and Paul Guard 50, and the centre still wins,
        because centre is the scarcer slot. Rank by raw points and this
        assertion inverts — which is the difference between the tool being
        smarter than ESPN's default rank and being a re-skin of it.
        """
        compute(client, league, auth_a)

        body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()
        top, second = body["items"][0], body["items"][1]

        assert top["projected_points"] == 45.0
        assert second["projected_points"] == 50.0
        assert top["value"] > second["value"]

    def test_replacement_points_are_the_last_starter(self, client, auth_a, league, players):
        """num_teams=2, so replacement is the 2nd best at each slot: PG 40, C 20."""
        compute(client, league, auth_a)

        body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()
        by_slot = {v["assigned_slot"]: v["replacement_points"] for v in body["items"]}

        assert by_slot["PG"] == 40.0
        assert by_slot["C"] == 20.0
        assert by_slot["UTIL"] == 45.0

    def test_recompute_updates_rather_than_duplicating(self, client, db, auth_a, league, players):
        compute(client, league, auth_a)
        compute(client, league, auth_a)

        assert db.scalar(select(func.count()).select_from(Valuation)) == len(players)

    def test_recompute_advances_computed_at(self, client, db, auth_a, league, players):
        """The upsert must name computed_at in its ON CONFLICT set_."""
        compute(client, league, auth_a)
        db.expire_all()
        before = sorted(db.scalars(select(Valuation.computed_at)))

        compute(client, league, auth_a)
        db.expire_all()
        after = sorted(db.scalars(select(Valuation.computed_at)))

        assert all(later > earlier for earlier, later in zip(before, after, strict=True))

    def test_recompute_picks_up_changed_projections(self, client, db, auth_a, league, players):
        """A resync changes projections; the cached value must follow."""
        compute(client, league, auth_a)
        before = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()[
            "items"
        ][0]["projected_points"]

        top = next(p for p in players if p.espn_player_id == 4)
        top.projections = {"pts": 999.0}
        db.commit()
        compute(client, league, auth_a)

        after = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()[
            "items"
        ][0]["projected_points"]

        assert before == 45.0
        assert after == 999.0

    def test_recompute_refreshes_the_value_not_just_the_projection(
        self, client, db, auth_a, league, players
    ):
        """Every computed column has to appear in the upsert's ON CONFLICT
        set_. Asserting only projected_points lets a missing `value` through,
        and a board sorted by a stale value looks entirely plausible."""
        compute(client, league, auth_a)

        worst = next(p for p in players if p.espn_player_id == 7)
        worst.projections = {"pts": 500.0}
        db.commit()
        compute(client, league, auth_a)

        body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()
        ids = espn_ids(players)
        now_top = next(v for v in body["items"] if ids[v["player_id"]] == 7)

        # 500 projected against the UTIL replacement, which has itself moved.
        assert now_top["value"] > 25.0
        assert body["items"][0]["player_id"] == now_top["player_id"]

    def test_only_the_leagues_own_season_is_valued(self, client, db, auth_a, league, players):
        """players is season-scoped shared reference data (SPEC 0.2). Without
        the season filter a database holding two seasons values them as one
        pool, which silently wrecks every replacement level."""
        db.add(
            Player(
                espn_player_id=555,
                season=league.season - 1,
                name="Last Year",
                pro_team="LAL",
                positions=["PG"],
                projections={"pts": 9999.0},
            )
        )
        db.commit()

        r = compute(client, league, auth_a)

        assert r.json()["players_valued"] == len(players)
        assert db.scalar(select(func.count()).select_from(Valuation)) == len(players)
        body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()
        assert [v["value"] for v in body["items"]] == SPEC_53_VALUES

    def test_another_leagues_valuations_are_untouched(
        self, client, db, auth_a, league, players, user_a
    ):
        """unique(league_id, player_id) is per league: two leagues on one season
        keep separate valuations, and computing one must not disturb the other."""
        other = League(
            user_id=user_a.id,
            espn_league_id=league.espn_league_id + 1,
            season=SEASON,
            name="Deeper league",
            scoring_format=ScoringFormat.POINTS,
            # 4 teams, so replacement levels — and every value — differ.
            num_teams=4,
            roster_size=13,
            roster_slots=league.roster_slots,
            point_weights=league.point_weights,
        )
        db.add(other)
        db.commit()

        compute(client, league, auth_a)
        compute(client, other, auth_a)

        assert db.scalar(select(func.count()).select_from(Valuation)) == 2 * len(players)
        mine = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()
        theirs = client.get(f"/leagues/{other.id}/valuations?limit=200", headers=auth_a).json()
        assert [v["value"] for v in mine["items"]] != [v["value"] for v in theirs["items"]]


class TestComputeRefusals:
    def test_a_categories_league_is_422_not_500(self, client, auth_a, categories_league, players):
        """The engine raises ValueError by design; unhandled that is a 500 on a
        request the user got wrong rather than one that broke."""
        r = compute(client, categories_league, auth_a)

        assert r.status_code == 422
        assert "points" in r.json()["detail"]

    def test_a_refused_categories_compute_writes_nothing(
        self, client, db, auth_a, categories_league, players
    ):
        compute(client, categories_league, auth_a)

        assert db.scalar(select(func.count()).select_from(Valuation)) == 0

    def test_an_unsynced_league_is_422(self, client, auth_a, league):
        """No `players` fixture: the pool is empty, which is 'sync me first',
        not an empty valuation set."""
        r = compute(client, league, auth_a)

        assert r.status_code == 422
        assert "ync" in r.json()["detail"]

    def test_a_league_that_does_not_exist_is_404(self, client, auth_a):
        r = client.post(f"/leagues/{uuid.uuid4()}/valuations/compute", headers=auth_a)

        assert r.status_code == 404


class TestListPlayers:
    def test_the_pool_is_listed_before_any_valuation_exists(self, client, auth_a, league, players):
        """The outer-join test. league_id in the WHERE instead of the ON makes
        this return an empty page — while still answering 200."""
        body = player_page(client, league, auth_a)

        assert body["total"] == len(players)
        assert len(body["items"]) == len(players)
        assert all(item["valuation"] is None for item in body["items"])

    def test_the_player_payload_carries_the_cached_pool_data(self, client, auth_a, league, players):
        body = player_page(client, league, auth_a, "&sort=name")
        first = body["items"][0]["player"]

        assert first["name"] == "Cal Center"
        assert first["pro_team"] == "BOS"
        assert first["positions"] == ["C"]
        assert first["projections"] == {"pts": 45.0}
        assert first["season"] == SEASON

    def test_the_valuation_is_attached_once_computed(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        body = player_page(client, league, auth_a)

        assert all(item["valuation"] is not None for item in body["items"])
        assert [item["player"]["espn_player_id"] for item in body["items"]] == (
            SPEC_53_EXPECTED_RANKING
        )

    def test_default_sort_is_value_descending(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        body = player_page(client, league, auth_a)

        assert [item["valuation"]["value"] for item in body["items"]] == SPEC_53_VALUES

    def test_sort_by_projected_points(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        body = player_page(client, league, auth_a, "&sort=projected_points")

        assert [item["valuation"]["projected_points"] for item in body["items"]] == [
            50.0,
            45.0,
            40.0,
            30.0,
            25.0,
            20.0,
            10.0,
        ]

    def test_sort_by_name(self, client, auth_a, league, players):
        body = player_page(client, league, auth_a, "&sort=name")

        assert [item["player"]["name"] for item in body["items"]] == sorted(p.name for p in players)

    def test_unvalued_players_sort_last_not_first(self, client, db, auth_a, league, players):
        """NULLS LAST. Postgres puts NULLs first for DESC by default, which
        would park every unvalued player at the top of the board."""
        compute(client, league, auth_a)
        newcomer = Player(
            espn_player_id=4242,
            season=SEASON,
            name="Zed Rookie",
            pro_team="LAL",
            positions=["SG"],
            projections={"pts": 5.0},
        )
        db.add(newcomer)
        db.commit()

        body = player_page(client, league, auth_a)

        assert body["total"] == len(players) + 1
        assert body["items"][-1]["player"]["name"] == "Zed Rookie"
        assert body["items"][-1]["valuation"] is None

    @pytest.mark.parametrize(
        ("position", "expected"),
        [("PG", 3), ("C", 3), ("SF", 1), ("SG", 0), ("PF", 0)],
    )
    def test_filter_by_position(self, client, auth_a, league, players, position, expected):
        body = player_page(client, league, auth_a, f"&position={position}")

        assert body["total"] == expected
        assert len(body["items"]) == expected
        assert all(position in item["player"]["positions"] for item in body["items"])

    def test_an_unknown_position_is_422(self, client, auth_a, league, players):
        """Only the five real positions are stored, so G and F match nothing —
        rejecting them beats answering 200 with an empty page."""
        r = client.get(f"/leagues/{league.id}/players?position=G", headers=auth_a)

        assert r.status_code == 422

    def test_pagination_slices_without_changing_the_total(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        body = client.get(f"/leagues/{league.id}/players?limit=3&offset=3", headers=auth_a).json()

        assert body["total"] == len(players)
        assert body["limit"] == 3
        assert body["offset"] == 3
        assert [item["player"]["espn_player_id"] for item in body["items"]] == [5, 3, 6]

    def test_paging_through_covers_the_pool_exactly_once(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        seen = []
        for offset in range(0, len(players), 2):
            page = client.get(
                f"/leagues/{league.id}/players?limit=2&offset={offset}", headers=auth_a
            ).json()
            seen += [item["player"]["espn_player_id"] for item in page["items"]]

        assert seen == SPEC_53_EXPECTED_RANKING

    def test_the_total_respects_the_position_filter(self, client, auth_a, league, players):
        body = client.get(
            f"/leagues/{league.id}/players?position=PG&limit=1", headers=auth_a
        ).json()

        assert body["total"] == 3
        assert len(body["items"]) == 1

    @pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "sort=elo"])
    def test_bad_paging_or_sort_is_422(self, client, auth_a, league, players, query):
        assert (
            client.get(f"/leagues/{league.id}/players?{query}", headers=auth_a).status_code == 422
        )

    def test_a_league_that_does_not_exist_is_404(self, client, auth_a):
        assert client.get(f"/leagues/{uuid.uuid4()}/players", headers=auth_a).status_code == 404


class TestTieBreaking:
    """Ordering has to be total, not merely mostly-total.

    Two players on the same replacement level are worth exactly the same, and
    `ORDER BY value DESC` alone leaves their order to the planner. The fixture
    pool hides this — it is inserted in espn_player_id order, so physical order
    already agrees — so these tests insert a tied pair BACKWARDS and check the
    response puts them back in ascending espn_player_id.
    """

    @pytest.fixture
    def tied_pair(self, db, players, league):
        """Two interchangeable players, inserted highest id first.

        Both are SG, so in a PG/C/UTIL league they are UTIL-only, and both
        project 1.0 — far too low to move any replacement level, so the rest of
        the SPEC 5.3 answer is undisturbed.
        """
        rows = [
            Player(
                espn_player_id=espn_player_id,
                season=SEASON,
                name=name,
                pro_team="NYK",
                positions=["SG"],
                projections={"pts": 1.0},
            )
            for espn_player_id, name in ((900, "Zeta Wing"), (800, "Alpha Wing"))
        ]
        db.add_all(rows)
        db.commit()
        return rows

    def test_tied_valuations_come_back_in_espn_id_order(
        self, client, auth_a, league, players, tied_pair
    ):
        compute(client, league, auth_a)

        order = valuation_order(client, league, auth_a, list(players) + list(tied_pair))

        assert order[-2:] == [800, 900]

    def test_tied_players_come_back_in_espn_id_order(
        self, client, auth_a, league, players, tied_pair
    ):
        compute(client, league, auth_a)

        body = player_page(client, league, auth_a)

        assert [item["player"]["espn_player_id"] for item in body["items"]][-2:] == [800, 900]

    def test_the_tied_pair_really_is_tied(self, client, auth_a, league, players, tied_pair):
        """Otherwise the two tests above would pass on value alone."""
        compute(client, league, auth_a)

        body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()
        last_two = body["items"][-2:]

        assert last_two[0]["value"] == last_two[1]["value"]


class TestListValuations:
    def test_an_uncomputed_league_is_an_empty_page_not_a_404(self, client, auth_a, league, players):
        body = client.get(f"/leagues/{league.id}/valuations", headers=auth_a).json()

        assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}

    def test_valuations_come_back_best_first(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        assert valuation_order(client, league, auth_a, players) == SPEC_53_EXPECTED_RANKING

    def test_the_payload_carries_the_whole_computation(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        top = client.get(f"/leagues/{league.id}/valuations", headers=auth_a).json()["items"][0]

        assert top["projected_points"] == 45.0
        assert top["replacement_points"] == 20.0
        assert top["value"] == 25.0
        assert top["assigned_slot"] == "C"
        assert top["league_id"] == str(league.id)
        assert top["computed_at"] is not None

    def test_pagination(self, client, auth_a, league, players):
        compute(client, league, auth_a)

        body = client.get(
            f"/leagues/{league.id}/valuations?limit=2&offset=1", headers=auth_a
        ).json()
        ids = espn_ids(players)

        assert body["total"] == len(players)
        assert [ids[v["player_id"]] for v in body["items"]] == [1, 2]

    def test_only_this_leagues_valuations_are_returned(
        self, client, db, auth_a, league, players, user_a
    ):
        other = League(
            user_id=user_a.id,
            espn_league_id=league.espn_league_id + 1,
            season=SEASON,
            name="Other",
            scoring_format=ScoringFormat.POINTS,
            num_teams=4,
            roster_size=13,
            roster_slots=league.roster_slots,
            point_weights=league.point_weights,
        )
        db.add(other)
        db.commit()
        compute(client, league, auth_a)
        compute(client, other, auth_a)

        body = client.get(f"/leagues/{league.id}/valuations?limit=200", headers=auth_a).json()

        assert body["total"] == len(players)
        assert all(v["league_id"] == str(league.id) for v in body["items"])
