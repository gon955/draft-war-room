"""League routes (SPEC 4).

    POST   /leagues              authed       owner = caller; pulls settings,
                                              point weights and the player pool
                                              from a data source built around
                                              the caller's own ESPN cookie
    GET    /leagues              authed       only the caller's leagues
    GET    /leagues/{id}         owner only
    POST   /leagues/{id}/sync    owner only   refresh players / projections
    DELETE /leagues/{id}         owner only

Creation and sync both go through services.sync, never through espn-api here,
and the data source arrives via Depends so tests can swap in the fake.

Denied access is 404, never 403 (SPEC 0.3). That rule lives in warroom.authz and
every handler below asks it rather than filtering on user_id inline.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from warroom.authz import require_league_owner
from warroom.crypto import InvalidToken, MissingFernetKey, decrypt, encrypt
from warroom.deps import CurrentUser, DataSourceFactory, DbSession
from warroom.models import League
from warroom.schemas.league import LeagueCreate, LeagueOut, SyncResult
from warroom.services import sync as sync_service
from warroom.valuation.data_source import ProjectionsUnavailable

router = APIRouter(prefix="/leagues", tags=["leagues"])


def _league_cookie(league: League) -> str | None:
    """The league's stored ESPN cookie in plaintext, or None if it has none.

    A league with no stored cookie is not an error: a public league needs no
    credential, and get_data_source_factory falls back to the server's own.

    InvalidToken means the row was written under a previous FERNET_KEY or has
    been tampered with — indistinguishable by design, and unrecoverable either
    way. crypto.decrypt deliberately leaves the response here, because only
    this layer knows it should read as "reconnect your league" rather than a 500.
    """
    if not league.espn_s2_encrypted:
        return None

    try:
        return decrypt(league.espn_s2_encrypted)
    except InvalidToken:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "This league's stored ESPN credential can no longer be read, so it "
                "cannot be synced. Reconnect the league to supply it again."
            ),
        ) from None


def _upstream_unavailable(league_id: int, season: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"ESPN player projections are currently unavailable for league {league_id} season {season}.",
    )


@router.post("", response_model=LeagueOut, status_code=status.HTTP_201_CREATED)
def create_league(
    payload: LeagueCreate, db: DbSession, user: CurrentUser, source_factory: DataSourceFactory
) -> League:

    try:
        # Encrypted at rest (SPEC 2.3), and never read back out by an endpoint.
        espn_s2_encrypted = encrypt(payload.espn_s2) if payload.espn_s2 else None
    except MissingFernetKey:
        # A deployment with no FERNET_KEY cannot store a private-league cookie,
        # and crypto.encrypt is right to refuse a plaintext fallback. Left
        # uncaught this surfaced as a bare 500 with a traceback, which reads as
        # a bug in the app rather than the operator error it is. 503, because
        # the request is fine and the server is not — and it names the fix.
        #
        # Public leagues send no cookie, never reach encrypt, and are
        # unaffected, so this must not become a startup check.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This server is not configured to store private-league "
                "credentials, so the ESPN cookie cannot be saved. Connect the "
                "league without it if it is public, or ask the operator to set "
                "FERNET_KEY."
            ),
        ) from None

    league = League(
        user_id=user.id,
        espn_league_id=payload.espn_league_id,
        season=payload.season,
        name=payload.name,
        espn_s2_encrypted=espn_s2_encrypted,
    )

    # This fetch uses the cookie the caller just supplied, taken from the
    # payload in plaintext — encrypting it is for storage, not for use. Without
    # this, connecting a PRIVATE league would fetch with whatever cookie the
    # server itself holds and fail for everyone but the deployment's owner.
    source = source_factory(payload.espn_s2)

    try:
        sync_service.sync_league_settings(db, league, source)
        db.add(league)

        sync_service.sync_player_pool(db, league, source)
        db.commit()

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"League {payload.espn_league_id} for season {payload.season} already exists for this account",
        ) from None
    except ProjectionsUnavailable:
        db.rollback()
        raise _upstream_unavailable(payload.espn_league_id, payload.season)

    return league


@router.get("", response_model=list[LeagueOut])
def list_leagues(db: DbSession, user: CurrentUser) -> list[League]:
    stmt = select(League).where(League.user_id == user.id).order_by(League.created_at)

    return db.scalars(stmt).all()


@router.get("/{league_id}", response_model=LeagueOut)
def get_league(league_id: uuid.UUID, db: DbSession, user: CurrentUser) -> League:

    return require_league_owner(db, league_id, user)


@router.post("/{league_id}/sync", response_model=SyncResult)
def sync_league(
    league_id: uuid.UUID, db: DbSession, user: CurrentUser, source_factory: DataSourceFactory
) -> SyncResult:

    league = require_league_owner(db, league_id, user)
    source = source_factory(_league_cookie(league))

    try:
        players_synced = sync_service.sync_league(db, league, source)
        db.commit()
    except ProjectionsUnavailable:
        db.rollback()
        raise _upstream_unavailable(league.espn_league_id, league.season)

    # Not league.updated_at: a re-sync whose settings are unchanged emits no
    # UPDATE on the league row, so that column reports the last settings change
    # rather than this sync — while the player pool was in fact refreshed.
    return SyncResult(
        players_synced=players_synced,
        scoring_format=league.scoring_format,
        synced_at=datetime.now(UTC),
    )


@router.delete("/{league_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_league(league_id: uuid.UUID, db: DbSession, user: CurrentUser) -> None:
    league = require_league_owner(db, league_id, user)

    # League.boards cascades all, delete-orphan, so this takes every board built
    # on the league with it — including boards other users hold shares on, and
    # their rankings. The most destructive endpoint in the app, and the cascade
    # makes that invisible at the call site.
    db.delete(league)
    db.commit()
