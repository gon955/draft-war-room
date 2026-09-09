"""Mock draft routes (SPEC 4).

    POST /boards/{id}/mocks       edit access
    GET  /boards/{id}/mocks       board access
    POST /mocks/{id}/picks        edit access   set a player on a pick_number
    GET  /mocks/{id}/best-available  board access

best-available is the payoff view — everyone not yet picked, ordered by this
board's user_rank where set and by computed value where not.

If the schedule slips, SPEC 8 says cut this before cutting tests or the authz
matrix.

TODO (Phase 5).
"""

from fastapi import APIRouter

router = APIRouter(tags=["mocks"])
