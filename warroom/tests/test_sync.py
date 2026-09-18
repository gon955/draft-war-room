"""ESPN adapter contract at the HTTP boundary (SPEC 6).

FakePlayerDataSource returns fixtures; assert the sync endpoint writes the
expected players rows, settings and point weights, and that re-syncing updates
rather than duplicating (unique(espn_player_id, season)).

The adapter's own translation is already covered without a network call in
warroom/valuation/tests/test_data_source.py.

The re-sync tests carry most of the weight here. An upsert that silently stops
updating looks identical to one that works — the row count stays right, the
endpoint still answers 200, and only the contents are stale. So they assert on
changed values and on players.updated_at, which is the only record of when the
pool was last refreshed and which a Core on-conflict upsert does NOT advance on
its own.
"""

import uuid

import pytest
from sqlalchemy import func, select

from warroom.models import League, Player, ScoringFormat
from warroom.tests.conftest import SEASON
from warroom.valuation.data_source import FakePlayerDataSource, fixed_source_factory
from warroom.valuation.domain import LeagueSettings, PlayerProjection


def point_source(client, settings, players):
    """Repoint the app at a source returning exactly these fixtures."""
    from warroom.deps import get_data_source_factory

    client.app.dependency_overrides[get_data_source_factory] = lambda: fixed_source_factory(
        FakePlayerDataSource(settings=settings, players=players)
    )


def one_player(espn_player_id=1, name="Paul Guard", positions=("PG",), pts=50.0, pro_team="BOS"):
    return PlayerProjection(
        espn_player_id=espn_player_id,
        name=name,
        positions=positions,
        stats={"pts": pts},
        pro_team=pro_team,
    )


def sync(client, league, headers):
    return client.post(f"/leagues/{league.id}/sync", headers=headers)


class TestTheResponse:
    def test_sync_reports_what_it_wrote(self, client, auth_a, league, player_pool):
        r = sync(client, league, auth_a)

        assert r.status_code == 200
        assert r.json()["players_synced"] == len(player_pool)
        assert r.json()["scoring_format"] == "points"

    def test_synced_at_advances_between_syncs(self, client, auth_a, league):
        """Not league.updated_at: a re-sync with unchanged settings emits no
        UPDATE on the league row, so that column would report the last settings
        change while the pool was in fact refreshed."""
        first = sync(client, league, auth_a).json()["synced_at"]
        second = sync(client, league, auth_a).json()["synced_at"]

        assert second > first


class TestThePlayerPool:
    def test_sync_writes_the_pool(self, client, db, auth_a, league, player_pool):
        assert db.scalar(select(func.count()).select_from(Player)) == 0

        sync(client, league, auth_a)

        assert db.scalar(select(func.count()).select_from(Player)) == len(player_pool)

    def test_every_field_lands_where_it_belongs(self, client, db, auth_a, league):
        point_source(
            client,
            LeagueSettings(
                scoring_format="points",
                num_teams=2,
                roster_slots={"PG": 1},
                roster_size=13,
                point_weights={"pts": 1.0},
            ),
            [
                one_player(
                    espn_player_id=99,
                    name="Nikola Test",
                    positions=("PF", "C"),
                    pts=42.0,
                    pro_team="DEN",
                )
            ],
        )

        sync(client, league, auth_a)

        row = db.scalar(select(Player).where(Player.espn_player_id == 99))
        assert row.name == "Nikola Test"
        assert row.pro_team == "DEN"
        assert row.positions == ["PF", "C"]
        assert row.projections == {"pts": 42.0}
        # Season comes from the league being synced, never from the projection.
        assert row.season == league.season

    def test_the_pool_is_season_scoped(self, client, db, auth_a, league, players):
        """players rows are keyed (espn_player_id, season). A sync for one season
        must not touch another season's cache of the same real players."""
        other = Player(
            espn_player_id=players[0].espn_player_id,
            season=SEASON + 1,
            name="Older Self",
            pro_team="LAL",
            positions=["SG"],
            projections={"pts": 1.0},
        )
        db.add(other)
        db.commit()

        sync(client, league, auth_a)
        db.expire_all()

        untouched = db.scalar(
            select(Player).where(
                Player.espn_player_id == players[0].espn_player_id, Player.season == SEASON + 1
            )
        )
        assert untouched.name == "Older Self"


class TestResync:
    def test_resyncing_updates_rather_than_duplicating(
        self, client, db, auth_a, league, players, player_pool
    ):
        """unique(espn_player_id, season) — the row count must not move."""
        before = db.scalar(select(func.count()).select_from(Player))
        assert before == len(players)

        sync(client, league, auth_a)
        sync(client, league, auth_a)

        assert db.scalar(select(func.count()).select_from(Player)) == len(player_pool)

    def test_changed_projections_overwrite_the_old_ones(self, client, db, auth_a, league, players):
        original = players[0]
        point_source(
            client,
            LeagueSettings(
                scoring_format="points",
                num_teams=2,
                roster_slots={"PG": 1},
                roster_size=13,
                point_weights={"pts": 1.0},
            ),
            [
                one_player(
                    espn_player_id=original.espn_player_id,
                    name="Renamed",
                    positions=("PG", "SG"),
                    pts=123.0,
                    pro_team="LAL",
                )
            ],
        )

        sync(client, league, auth_a)
        db.expire_all()

        row = db.get(Player, original.id)
        assert row.name == "Renamed"
        assert row.pro_team == "LAL"
        assert row.positions == ["PG", "SG"]
        assert row.projections == {"pts": 123.0}

    def test_updated_at_advances_on_a_resync(self, client, db, auth_a, league, players):
        """The Core on-conflict upsert does not fire the ORM's onupdate, so
        sync sets updated_at explicitly. Without that this column freezes at
        first insert and nothing else notices."""
        sync(client, league, auth_a)
        db.expire_all()
        first = db.scalar(select(Player.updated_at).where(Player.espn_player_id == 1))

        sync(client, league, auth_a)
        db.expire_all()
        second = db.scalar(select(Player.updated_at).where(Player.espn_player_id == 1))

        assert second > first


class TestLeagueSettings:
    def test_sync_refreshes_stale_settings(self, client, db, auth_a, league):
        """The commissioner can change scoring mid-season; the point weights are
        read from the league, never hardcoded (SPEC 2.2)."""
        league.point_weights = {"pts": 999.0}
        league.num_teams = 99
        db.commit()

        sync(client, league, auth_a)
        db.expire_all()

        refreshed = db.get(League, league.id)
        assert refreshed.point_weights == {"pts": 1.0}
        assert refreshed.num_teams == 2

    def test_the_scoring_format_is_stored_as_an_enum(self, client, db, auth_a, league):
        """The domain hands over a plain string; the column is a Postgres enum."""
        sync(client, league, auth_a)
        db.expire_all()

        assert db.get(League, league.id).scoring_format is ScoringFormat.POINTS


class TestAuthorization:
    def test_the_owner_can_sync(self, client, auth_a, league):
        assert sync(client, league, auth_a).status_code == 200

    def test_a_stranger_gets_404(self, client, auth_b, league):
        assert sync(client, league, auth_b).status_code == 404

    def test_an_edit_share_holder_cannot_sync(self, client, auth_b, league, edit_share):
        """Syncing spends the owner's ESPN session and overwrites shared
        reference data, so it stays with whoever connected the league."""
        assert sync(client, league, auth_b).status_code == 403

    def test_unauthenticated_is_401(self, client, league):
        assert client.post(f"/leagues/{league.id}/sync").status_code == 401

    def test_a_league_that_does_not_exist_is_404(self, client, auth_a):
        assert client.post(f"/leagues/{uuid.uuid4()}/sync", headers=auth_a).status_code == 404


class TestUpstreamFailure:
    @pytest.fixture
    def empty_source(self, client, league_settings):
        point_source(client, league_settings, [])

    def test_an_empty_pool_is_a_clean_4xx(self, client, auth_a, league, empty_source):
        assert sync(client, league, auth_a).status_code == 422

    def test_an_empty_pool_does_not_wipe_a_good_one(
        self, client, db, auth_a, league, players, empty_source
    ):
        """The rule the module's docstring states outright: do not write an empty
        pool over a good one. A refused sync must cost nothing."""
        sync(client, league, auth_a)
        db.expire_all()

        assert db.scalar(select(func.count()).select_from(Player)) == len(players)
        assert db.scalar(select(Player.name).where(Player.espn_player_id == 1)) == "Paul Guard"
