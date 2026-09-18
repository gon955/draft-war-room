"""Mock draft routes (SPEC 4).

    POST /boards/{id}/mocks       edit access
    GET  /boards/{id}/mocks       board access
    POST /mocks/{id}/picks        edit access   set a player on a pick_number
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

TODO (Phase 5).
"""

from fastapi import APIRouter

router = APIRouter(tags=["mocks"])
