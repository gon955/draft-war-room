"""The authorization matrix (SPEC 6) — the centrepiece of the suite.

  B GETs A's board with no share            -> 404, NOT 403
  B PATCHes a ranking on A's board, no share -> 404
  B with a READ share: GET board            -> 200
  B with a READ share: POST a ranking       -> 403
  B with an EDIT share: POST a ranking      -> 200
  B with an EDIT share: DELETE the board    -> 403  (owner-only)
  a non-owner POSTing to /boards/{id}/shares -> 404/403

404-not-403 is the assertion that matters: 403 tells an attacker the board is
real. Test the status code explicitly, never merely `>= 400`.

SPEC 8: if the schedule slips, this file is the last thing to cut.

TODO (Phase 3).
"""
