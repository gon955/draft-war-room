"""Board routes (SPEC 4).

    POST   /boards        authed        must reference a league the caller can
                                        access; owner = caller
    GET    /boards        authed        owned by OR shared with the caller
    GET    /boards/{id}   board access
    PATCH  /boards/{id}   edit access
    DELETE /boards/{id}   owner only    a share, even an edit share, is not
                                        permission to delete someone's board

Every handler asks warroom.authz rather than filtering by user_id inline: the
404-not-403 rule is one decision, made in one place, and a route that forgets to
ask is visible as a missing call rather than as a subtly wrong WHERE clause.
"""

import uuid

from fastapi import APIRouter, status
from sqlalchemy import or_, select

from warroom.authz import (
    require_board_access,
    require_board_owner,
    require_edit_access,
    require_league_access,
)
from warroom.deps import CurrentUser, DbSession
from warroom.models import Board, BoardShare
from warroom.schemas.board import BoardCreate, BoardOut, BoardPatch

router = APIRouter(prefix="/boards", tags=["boards"])


@router.post("", response_model=BoardOut, status_code=status.HTTP_201_CREATED)
def create_board(payload: BoardCreate, db: DbSession, user: CurrentUser) -> Board:
    # Access, not ownership: a league shared with you through a board is a
    # league you may build your own board on.
    require_league_access(db, payload.league_id, user)

    board = Board(user_id=user.id, league_id=payload.league_id, name=payload.name)
    db.add(board)
    db.commit()
    return board


@router.get("", response_model=list[BoardOut])
def list_boards(db: DbSession, user: CurrentUser) -> list[Board]:
    """Boards the caller owns, plus boards shared with them."""
    return list(
        db.scalars(
            select(Board)
            .outerjoin(
                BoardShare,
                (BoardShare.board_id == Board.id) & (BoardShare.shared_with_user_id == user.id),
            )
            .where(or_(Board.user_id == user.id, BoardShare.id.is_not(None)))
            .order_by(Board.created_at)
            .distinct()
        )
    )


@router.get("/{board_id}", response_model=BoardOut)
def get_board(board_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Board:
    return require_board_access(db, board_id, user)


@router.patch("/{board_id}", response_model=BoardOut)
def patch_board(
    board_id: uuid.UUID, payload: BoardPatch, db: DbSession, user: CurrentUser
) -> Board:
    board = require_edit_access(db, board_id, user)

    # exclude_unset, so omitting a field leaves it alone rather than nulling it.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(board, field, value)

    db.commit()
    return board


@router.delete("/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_board(board_id: uuid.UUID, db: DbSession, user: CurrentUser) -> None:
    board = require_board_owner(db, board_id, user)
    db.delete(board)
    db.commit()
