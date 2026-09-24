"""Board share routes — owner-only management (SPEC 4).

    GET    /boards/{id}/shares   owner only
    POST   /boards/{id}/shares   owner only   share with a user by email
    DELETE /shares/{id}          owner only

A non-owner asking about a board's shares gets 404: the share list is exactly
the sort of thing that leaks who else exists.

Sharing by email means this is also the one endpoint that can be used to probe
the user table, so an unknown recipient returns the same 404 as a hidden board
rather than "no such user".
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from warroom.authz import require_board_owner
from warroom.deps import CurrentUser, DbSession
from warroom.models import BoardShare, User
from warroom.schemas.share import ShareCreate, ShareOut

router = APIRouter(tags=["shares"])


@router.get("/boards/{board_id}/shares", response_model=list[ShareOut])
def list_shares(board_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[BoardShare]:
    require_board_owner(db, board_id, user)
    return list(db.scalars(select(BoardShare).where(BoardShare.board_id == board_id)))


@router.post(
    "/boards/{board_id}/shares", response_model=ShareOut, status_code=status.HTTP_201_CREATED
)
def create_share(
    board_id: uuid.UUID, payload: ShareCreate, db: DbSession, user: CurrentUser
) -> BoardShare:
    require_board_owner(db, board_id, user)

    # lower(email), for the index — same reason as the lookups in auth.py.
    recipient = db.scalar(select(User).where(func.lower(User.email) == payload.email))
    if recipient is None:
        # Deliberately a 404 with the module's generic detail. Answering "no
        # such user" here would turn this endpoint into an oracle for which
        # email addresses hold accounts.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if recipient.id == user.id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="You already own this board.",
        )

    share = BoardShare(
        board_id=board_id,
        shared_with_user_id=recipient.id,
        permission=payload.permission,
    )
    db.add(share)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # unique(board_id, shared_with_user_id): one permission level per user.
        # Changing it is a DELETE then a POST, so the two rows can never coexist
        # and "does B have edit access?" stays answerable.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This board is already shared with that user.",
        ) from None

    return share


@router.delete("/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_share(share_id: uuid.UUID, db: DbSession, user: CurrentUser) -> None:
    share = db.get(BoardShare, share_id)
    if share is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # Authorize on the parent board, not the share row: only the owner revokes.
    require_board_owner(db, share.board_id, user)

    db.delete(share)
    db.commit()
