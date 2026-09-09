"""Ownership and share authorization — the spine of the app (SPEC 0.3, 4).

Two rules, and they are the whole point of the exercise:

  access to a board  = owner OR any board_share row
  edit access        = owner OR a board_share with permission='edit'

**A denied read returns 404, not 403.** 403 confirms the resource exists, which
leaks the existence of other users' boards to anyone willing to enumerate ids.
403 is correct only once access is already established and the *operation* is
too privileged — a read-share holder trying to write, or a non-owner trying an
owner-only action such as deleting a board or managing its shares.

TODO (Phase 3), written before the routes that depend on it:
  * `require_board_access(db, board_id, user) -> Board`   (404 when denied)
  * `require_edit_access(db, board_id, user) -> Board`    (404 unseen / 403 read-only)
  * `require_board_owner(db, board_id, user) -> Board`    (404 unseen / 403 non-owner)
  * `require_league_access(db, league_id, user) -> League`

The SPEC 6 matrix in tests/test_authz.py is the executable version of this
docstring; treat a change here as a change to that test.
"""
