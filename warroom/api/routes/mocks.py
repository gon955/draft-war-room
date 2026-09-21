"""Mock draft routes (SPEC 4).

    POST /boards/{id}/mocks       edit access
    GET  /boards/{id}/mocks       board access
    POST /mocks/{id}/picks        edit access   set a player on a pick_number
    GET  /mocks/{id}/picks        board access  the draft board itself
    GET  /mocks/{id}/best-available  board access

best-available is the payoff view — everyone not yet picked, ordered by this
board's user_rank where set and by computed value where not.

So it reads: players (by the board's league season) LEFT JOIN rankings (this
board) LEFT JOIN valuations (this league), minus the player_ids already on a
mock_picks row for this draft. Driven off players rather than rankings because
rankings only holds players the user has touched — the best available player is
usually one nobody has annotated yet. ORDER BY user_rank NULLS LAST, then value
DESC, which is what "manual rank overrides the computed order" (SPEC 5.4) means
once both are in the same query.

Two things to get right when the picks land:
  * generate a draft's picks from num_teams x roster_size on the league row —
    and note that sync will write roster_size=0 if ESPN returns no
    lineupSlotCounts, which would silently mean zero rounds.
  * validate a pick's player against the board's league season, the same check
    create_ranking does, and against unique(mock_draft_id, pick_number).

If the schedule slips, SPEC 8 says cut this before cutting tests or the authz
matrix.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session, selectinload

from warroom.authz import require_board_access, require_edit_access
from warroom.deps import CurrentUser, DbSession
from warroom.models import MockDraft, MockPick, Player, Ranking, User, Valuation
from warroom.schemas.mock import (
    BestAvailableOut,
    LineupOut,
    LiveValueOut,
    MadePickOut,
    MockCreate,
    MockOut,
    MockWithProgressOut,
    PickOut,
    PickSet,
    PickWithPlayerOut,
    RecommendationOut,
    RecommendationPage,
    RecommendationSort,
    SeatOut,
    SimulateIn,
    SimulateOut,
)
from warroom.schemas.player import Page, PlayerOut, Position, ValuationOut
from warroom.schemas.ranking import RankingOut
from warroom.services.lineup import my_lineup
from warroom.services.recommendation import NotValuedYet, recommend
from warroom.services.simulation import simulate
from warroom.services.valuation import NotAPointsLeague

router = APIRouter(tags=["mocks"])

LimitParam = Annotated[int, Query(ge=1, le=200)]
OffsetParam = Annotated[int, Query(ge=0)]


def generate_picks(
    mock_draft_id: uuid.UUID, num_teams: int, rounds: int, my_draft_slot: int
) -> list[dict]:
    """Every slot of a snake draft, in pick order.

    Odd rounds run slot 1..N, even rounds run N..1, so the teams at each end of
    the order pick twice in a row at the turn — which is the entire reason
    draft position is worth modelling.

    Pure, so the ordering is checkable against a hand-drawn board with no
    database; the route only has to persist what comes back.
    """
    picks = []
    for rnd in range(1, rounds + 1):
        for position in range(1, num_teams + 1):
            slot = position if rnd % 2 else num_teams - position + 1
            picks.append(
                {
                    "mock_draft_id": mock_draft_id,
                    "pick_number": (rnd - 1) * num_teams + position,
                    "round": rnd,
                    "team_slot": slot,
                    "is_mine": slot == my_draft_slot,
                    "player_id": None,
                }
            )
    return picks


def _mock_for(db: Session, mock_id: uuid.UUID, user: User, authorize) -> MockDraft:
    """Load a mock and authorize against the board that owns it.

    `authorize` is require_board_access or require_edit_access — the same
    fetch-then-authorize shape as _ranking_for_edit and _tier_for_edit, and for
    the same reason: permission lives on the parent board, and a mock whose
    board the caller cannot see is reported as a missing mock, never a
    forbidden one.
    """
    mock = db.get(MockDraft, mock_id)
    if mock is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    authorize(db, mock.board_id, user)
    return mock


def _with_progress(mock: MockDraft, total: int | None, made: int | None) -> MockWithProgressOut:
    return MockWithProgressOut(
        **MockOut.model_validate(mock).model_dump(),
        picks_total=total or 0,
        picks_made=made or 0,
    )


@router.post(
    "/boards/{board_id}/mocks",
    response_model=MockWithProgressOut,
    status_code=status.HTTP_201_CREATED,
)
def create_mock(
    board_id: uuid.UUID, payload: MockCreate, db: DbSession, user: CurrentUser
) -> MockWithProgressOut:
    """Create a mock and lay out its whole board of empty picks.

    The picks exist from creation because SPEC 4 sets a player ON a pick_number
    rather than appending one, and mock_picks.player_id is nullable precisely
    so an undrafted slot has somewhere to be.
    """
    board = require_edit_access(db, board_id, user)
    league = board.league

    # sync writes roster_size=0 when ESPN returns no lineupSlotCounts, and zero
    # rounds means a mock with no picks at all that still answers 201.
    if league.num_teams < 1 or league.roster_size < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "This league has no roster settings yet, so a draft board cannot "
                "be laid out. Sync the league first."
            ),
        )

    if payload.my_draft_slot > league.num_teams:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Draft slot must be between 1 and {league.num_teams}.",
        )

    mock = MockDraft(board_id=board_id, name=payload.name, my_draft_slot=payload.my_draft_slot)
    db.add(mock)
    # Flush for the id — the picks below carry it as a foreign key.
    db.flush()

    rows = generate_picks(mock.id, league.num_teams, league.roster_size, payload.my_draft_slot)
    db.execute(insert(MockPick), rows)
    db.commit()

    return _with_progress(mock, len(rows), 0)


@router.get("/boards/{board_id}/mocks", response_model=list[MockWithProgressOut])
def list_mocks(board_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[MockWithProgressOut]:
    require_board_access(db, board_id, user)

    # count(*) is every slot on the board; count(player_id) skips NULLs, which
    # is exactly "picks actually made". Grouped, so this is one query for the
    # whole list rather than two per mock.
    progress = (
        select(
            MockPick.mock_draft_id.label("mock_id"),
            func.count().label("total"),
            func.count(MockPick.player_id).label("made"),
        )
        .group_by(MockPick.mock_draft_id)
        .subquery()
    )

    rows = db.execute(
        select(MockDraft, progress.c.total, progress.c.made)
        .outerjoin(progress, progress.c.mock_id == MockDraft.id)
        .where(MockDraft.board_id == board_id)
        .order_by(MockDraft.created_at)
    ).all()

    return [_with_progress(mock, total, made) for mock, total, made in rows]


@router.post("/mocks/{mock_id}/picks", response_model=PickOut)
def set_pick(mock_id: uuid.UUID, payload: PickSet, db: DbSession, user: CurrentUser) -> MockPick:
    """Record a pick, or clear one by sending player_id null."""
    mock = _mock_for(db, mock_id, user, require_edit_access)

    pick = db.scalar(
        select(MockPick).where(
            MockPick.mock_draft_id == mock.id, MockPick.pick_number == payload.pick_number
        )
    )
    if pick is None:
        # The board is laid out at creation, so an unknown number is out of
        # range for this draft rather than a row waiting to be inserted.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This draft has no such pick number.",
        )

    if payload.player_id is not None:
        player = db.get(Player, payload.player_id)
        # The same season check create_ranking does, for the same reason: a
        # player from another season joins to no valuation and would sit on the
        # board with no value at all.
        if player is None or player.season != mock.board.league.season:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="That player is not in this league's season.",
            )

        # One player cannot go twice in one draft. Nothing in the schema says
        # so — unique(mock_draft_id, pick_number) constrains the SLOT, not the
        # player — so it is checked here. The pick_number exclusion matters:
        # without it, re-sending the same player to the same slot to correct a
        # neighbouring typo would 409 against itself.
        taken_at = db.scalar(
            select(MockPick.pick_number).where(
                MockPick.mock_draft_id == mock.id,
                MockPick.player_id == payload.player_id,
                MockPick.pick_number != payload.pick_number,
            )
        )
        if taken_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"That player was already taken at pick {taken_at}.",
            )

    pick.player_id = payload.player_id
    db.commit()
    return pick


@router.get("/mocks/{mock_id}/picks", response_model=list[PickWithPlayerOut])
def list_picks(mock_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[MockPick]:
    """The draft board, in pick order, with the player on each filled slot.

    Not in SPEC 4, which describes setting a pick and asking what is left but
    never reading the board back — so a UI could record picks and have no way
    to show them.

    The player is nested rather than left as a bare id, and that is the whole
    difference between a board and a list of UUIDs. A drafted player is by
    definition absent from best-available, so a client joining the two has
    nowhere to look the name up and can only print the id.

    selectinload, not lazy loading: this returns every slot on the board —
    130 rows for a 10-team, 13-round league — and each one touching `player`
    on access is 130 queries for one screen.
    """
    mock = _mock_for(db, mock_id, user, require_board_access)

    return list(
        db.scalars(
            select(MockPick)
            .options(selectinload(MockPick.player))
            .where(MockPick.mock_draft_id == mock.id)
            .order_by(MockPick.pick_number)
        )
    )


@router.get("/mocks/{mock_id}/best-available", response_model=Page[BestAvailableOut])
def best_available(
    mock_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    position: Position | None = None,
    limit: LimitParam = 50,
    offset: OffsetParam = 0,
) -> Page[BestAvailableOut]:
    """Everyone still on the board, best first — the "who should I take" view."""
    mock = _mock_for(db, mock_id, user, require_board_access)
    board = mock.board
    league = board.league

    # IS NOT NULL IS LOAD-BEARING, NOT TIDINESS. The pick rows are created
    # empty, so this subquery contains NULLs, and `x NOT IN (1, NULL)` is NULL
    # — never true — for every row. Without the guard best-available returns an
    # EMPTY page and still answers 200, which reads as "everyone is drafted" on
    # pick one. Measured on a 7-player pool with one player taken: 0 available
    # without it, 6 with it.
    taken = select(MockPick.player_id).where(
        MockPick.mock_draft_id == mock.id, MockPick.player_id.is_not(None)
    )

    filters = [Player.season == league.season, Player.id.not_in(taken)]
    if position is not None:
        filters.append(Player.positions.contains([position.value]))

    # Both joins carry their scope in the ON clause, never the WHERE — there,
    # either one collapses the outer join into an inner one and hides every
    # player the user has not ranked, which is most of them.
    joined = (
        select(Player, Valuation, Ranking)
        .outerjoin(
            Valuation, (Valuation.player_id == Player.id) & (Valuation.league_id == league.id)
        )
        .outerjoin(Ranking, (Ranking.player_id == Player.id) & (Ranking.board_id == board.id))
    )

    total = db.scalar(select(func.count()).select_from(Player).where(*filters))

    rows = db.execute(
        joined.where(*filters)
        # SPEC 5.4's "the human edits it", as a sort key: a manual user_rank
        # wins outright, everyone unranked falls back to computed value, and
        # espn_player_id settles the real ties at the replacement level.
        .order_by(
            Ranking.user_rank.asc().nulls_last(),
            Valuation.value.desc().nulls_last(),
            Player.espn_player_id.asc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()

    return Page[BestAvailableOut](
        items=[
            BestAvailableOut(
                player=PlayerOut.model_validate(player),
                valuation=ValuationOut.model_validate(val) if val is not None else None,
                ranking=RankingOut.model_validate(rank) if rank is not None else None,
            )
            for player, val, rank in rows
        ],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/mocks/{mock_id}/recommendation", response_model=RecommendationPage)
def recommendation(
    mock_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    position: Position | None = None,
    sort: RecommendationSort = RecommendationSort.SCORE,
    limit: LimitParam = 25,
    offset: OffsetParam = 0,
) -> RecommendationPage:
    """Who to take now, re-valued against the board as it stands.

    best-available sorts by a preseason number: the pool it was computed over
    no longer exists by the third round. This endpoint recomputes each slot's
    replacement level against the UNDRAFTED pool and the seats still unfilled
    league-wide, so a position that has been run on lifts the players left in
    it — which is the difference between a ranking and a recommendation.

    Read-only. Nothing here touches the valuations cache; see
    services/recommendation.py for why that matters.
    """
    mock = _mock_for(db, mock_id, user, require_board_access)

    try:
        results, waiting = recommend(db, mock)
    except NotAPointsLeague as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except NotValuedYet as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    # FILTER AND PAGE HERE, NEVER IN THE SERVICE. A replacement level is a
    # property of the whole league's supply and demand, so narrowing the pool
    # to one position before computing it would measure centres against a
    # league of nothing but centres. The filter is a view of the answer, not an
    # input to it.
    if position is not None:
        results = [r for r in results if position.value in r.player.positions]

    # SCORE by default: marginal value plus what you should expect next turn,
    # so a player who will still be sitting there when the wheel comes back is
    # worth less of THIS pick than one who is about to vanish.
    #
    # The MARGINAL tie-break puts filling an open seat ahead of league value.
    # Late on, every remaining player grades at or under replacement and
    # marginal collapses to 0 across the board; without it a centre you cannot
    # start outranks the guard who fills the one hole in your lineup, on a
    # league-wide number that no longer says anything about your team.
    #
    # VALUE leaves the order exactly as live_values produced it.
    sort_keys = {
        # Falls through the SAME chain as MARGINAL once score ties. In the
        # bench rounds every candidate scores 0.0, and without this the tie
        # breaks on espn_player_id — which sorts by seniority and puts a wall
        # of 34-year-olds at the top of your board.
        RecommendationSort.SCORE: lambda r: (
            -r.score,
            -r.marginal,
            not r.fills_open_seat,
            -r.live.value.value,
            r.player.espn_player_id,
        ),
        RecommendationSort.MARGINAL: lambda r: (
            -r.marginal,
            not r.fills_open_seat,
            -r.live.value.value,
            r.player.espn_player_id,
        ),
    }
    # Same fallback chain as SCORE below it. A league that estimates nothing
    # has model_sd 0 for everybody, so the primary key ties for the whole
    # board and the tie-break IS the ordering.
    sort_keys[RecommendationSort.CONFIDENT] = lambda r: (
        -(r.score - r.valuation.model_sd),
        *sort_keys[RecommendationSort.SCORE](r),
    )
    if sort in sort_keys:
        results = sorted(results, key=sort_keys[sort])

    page = results[offset : offset + limit]

    return RecommendationPage(
        picks_until_next=waiting,
        items=[
            RecommendationOut(
                player=PlayerOut.model_validate(r.player),
                valuation=ValuationOut.model_validate(r.valuation),
                ranking=RankingOut.model_validate(r.ranking) if r.ranking is not None else None,
                marginal_value=r.marginal,
                improves_lineup=r.marginal > 0.0,
                fills_open_seat=r.fills_open_seat,
                survival=r.survival,
                expected_next=r.expected_next,
                score=r.score,
                live=LiveValueOut(
                    value=r.live.value.value,
                    replacement_points=r.live.value.replacement_points,
                    assigned_slot=r.live.value.assigned_slot,
                    replacement_shift=r.live.shift,
                    value_change=r.live.value_change,
                ),
            )
            for r in page
        ],
        total=len(results),
        limit=limit,
        offset=offset,
    )


@router.get("/mocks/{mock_id}/lineup", response_model=LineupOut)
def lineup(mock_id: uuid.UUID, db: DbSession, user: CurrentUser) -> LineupOut:
    """Your own roster in this mock, auto-assigned to the league's slots.

    Board access, not edit: a shared board's owner should be able to look at
    the lineup their draft is producing without being able to change it.

    The assignment comes from valuation/draft.py rather than being recomputed
    here, so the seats shown always agree with the seats the replacement-level
    maths thinks are taken.
    """
    mock = _mock_for(db, mock_id, user, require_board_access)
    result = my_lineup(db, mock)

    return LineupOut(
        my_draft_slot=mock.my_draft_slot,
        roster_size=mock.board.league.roster_size,
        starters=[
            SeatOut(
                slot=seat.slot,
                index=seat.index,
                player=(
                    PlayerOut.model_validate(result.players_by_espn_id[seat.player.espn_player_id])
                    if seat.player is not None
                    else None
                ),
            )
            for seat in result.assignment.seats
        ],
        bench=[
            PlayerOut.model_validate(result.players_by_espn_id[p.espn_player_id])
            for p in result.assignment.bench
        ],
        bench_size=result.bench_size,
        picks_made=result.picks_made,
        picks_remaining=result.picks_remaining,
    )


@router.post("/mocks/{mock_id}/simulate", response_model=SimulateOut)
def simulate_picks(
    mock_id: uuid.UUID, payload: SimulateIn, db: DbSession, user: CurrentUser
) -> SimulateOut:
    """Auto-draft the other teams up to your next pick.

    Edit access, not board access: unlike every other mock read this writes
    picks, so a read-only share must not be able to run it.

    Already-filled slots are skipped rather than redrafted, so calling this
    twice is safe and never overwrites a pick you made yourself.
    """
    mock = _mock_for(db, mock_id, user, require_edit_access)

    try:
        result = simulate(
            db,
            mock,
            reach=payload.reach,
            seed=payload.seed,
            stop_at_my_pick=payload.stop_at_my_pick,
        )
    except (NotAPointsLeague, NotValuedYet) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    db.commit()

    return SimulateOut(
        picks=[
            MadePickOut(
                pick_number=p.pick_number,
                team_slot=p.team_slot,
                player=PlayerOut.model_validate(p.player),
            )
            for p in result.picks
        ],
        next_pick_number=result.next_pick_number,
        next_is_mine=result.next_is_mine,
        board_complete=result.board_complete,
    )
