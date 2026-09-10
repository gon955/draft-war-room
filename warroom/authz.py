"""Ownership and share authorization — the spine of the app (SPEC 0.3, 4).

Two rules, and they are the whole point of the exercise:

  access to a board  = owner OR any board_share row
  edit access        = owner OR a board_share with permission='edit'

**A denied read returns 404, not 403.** 403 confirms the resource exists, which
leaks the existence of other users' boards to anyone willing to enumerate ids.
403 is correct only once access is already established and the *operation* is
too privileged — a read-share holder trying to write, or a non-owner trying an
owner-only action such as deleting a board or managing its shares.

So each helper answers with one of three outcomes, and the distinction between
the last two is the part worth getting right:

  granted          -> returns the row
  not visible      -> 404, identical to the response for an id that never existed
  visible, denied  -> 403, and only reachable by someone who already has a share

The SPEC 6 matrix in tests/test_authz.py is the executable version of this
docstring; treat a change here as a change to that test.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from warroom.models import Board, BoardShare, League, SharePermission, User

# One message for every 404 this module raises. A distinct detail per case
# ("no such board" vs "not shared with you") would re-leak exactly what the
# status code is chosen to hide.
NOT_FOUND_DETAIL = "Not found"


def _not_found() -> HTTPException:
    """Returned, never raised internally, so each `raise` is visible at its call
    site — the same reason deps.get_current_user builds its 401 this way."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _share_for(db: Session, board_id: uuid.UUID, user_id: uuid.UUID) -> BoardShare | None:
    """The one share row for this board and user, or None.

    At most one can exist: unique(board_id, shared_with_user_id). Without that
    constraint this would return an arbitrary row and "does B have edit access?"
    would depend on row order.
    """
    return db.scalar(
        select(BoardShare).where(
            BoardShare.board_id == board_id,
            BoardShare.shared_with_user_id == user_id,
        )
    )


def require_board_access(db: Session, board_id: uuid.UUID, user: User) -> Board:
    """Read access: owner or any share. Anything else is 404."""
    board = db.get(Board, board_id)
    if board is None:
        raise _not_found()

    if board.user_id == user.id:
        return board

    if _share_for(db, board_id, user.id) is not None:
        return board

    raise _not_found()


def require_edit_access(db: Session, board_id: uuid.UUID, user: User) -> Board:
    """Write access: owner or an edit share.

    A read-share holder gets 403, not 404: they can already see the board, so
    there is nothing left to conceal — and 404 here would be actively confusing,
    telling them a board they just read does not exist.
    """
    board = db.get(Board, board_id)
    if board is None:
        raise _not_found()

    if board.user_id == user.id:
        return board

    share = _share_for(db, board_id, user.id)
    if share is None:
        raise _not_found()

    if share.permission is not SharePermission.EDIT:
        raise _forbidden("This board is shared with you for reading only.")

    return board


def require_board_owner(db: Session, board_id: uuid.UUID, user: User) -> Board:
    """Owner-only operations: deleting a board, managing its shares.

    An edit share is not ownership. Someone you gave edit rights to can change
    what is on the board; they cannot delete it out from under you, and they
    cannot hand access to a third party.
    """
    board = db.get(Board, board_id)
    if board is None:
        raise _not_found()

    if board.user_id == user.id:
        return board

    if _share_for(db, board_id, user.id) is None:
        raise _not_found()

    raise _forbidden("Only the board's owner can do this.")


def require_league_access(db: Session, league_id: uuid.UUID, user: User) -> League:
    """Read access to a league's settings and cached player pool.

    Granted to the owner, and to anyone holding a share on any board built on
    that league — otherwise a read-share holder could open the board but not see
    a single player on it, which makes the share worthless.
    """
    league = db.get(League, league_id)
    if league is None:
        raise _not_found()

    if league.user_id == user.id:
        return league

    shared = db.scalar(
        select(BoardShare.id)
        .join(Board, Board.id == BoardShare.board_id)
        .where(
            Board.league_id == league_id,
            BoardShare.shared_with_user_id == user.id,
        )
        .limit(1)
    )
    if shared is not None:
        return league

    raise _not_found()


def require_league_owner(db: Session, league_id: uuid.UUID, user: User) -> League:
    """Owner-only league operations: sync, delete, recompute valuations.

    Syncing hits ESPN with the owner's own session cookie and overwrites shared
    reference data, so it stays with the person who connected the league.
    """
    league = db.get(League, league_id)
    if league is None:
        raise _not_found()

    if league.user_id == user.id:
        return league

    # Same three-way split as require_board_owner. A share-holder can already
    # establish this league exists — require_league_access shows them its player
    # pool — so hiding it from them here would only be inconsistent, not private.
    shared = db.scalar(
        select(BoardShare.id)
        .join(Board, Board.id == BoardShare.board_id)
        .where(
            Board.league_id == league_id,
            BoardShare.shared_with_user_id == user.id,
        )
        .limit(1)
    )
    if shared is None:
        raise _not_found()

    raise _forbidden("Only the league's owner can do this.")
