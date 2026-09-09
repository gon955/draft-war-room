"""The relational schema (SPEC 3). UUID primary keys throughout.

One module rather than a package: all ten tables are densely cross-referenced
(boards -> rankings -> players, boards <-> users via board_shares), and keeping
them together avoids the circular-import dance that splitting them invites.

Tables, and the constraints that carry meaning rather than decoration:

  users          email unique, stored lowercased
  leagues        unique(user_id, espn_league_id, season) — season-scoped because
                 a league's roster shape really does change year to year: this
                 league ran F/C in 2026 and SG/SF in 2027
  players        unique(espn_player_id, season), index(season) — shared reference
                 data, cached from ESPN, owned by no user (SPEC 0.2)
  valuations     unique(league_id, player_id) — the engine's output, cached
  boards         a user may keep several per league as competing strategies
  tiers          board-scoped, ordered by sort_order
  rankings       unique(board_id, player_id), index(board_id, user_rank) — the
                 CRUD-heavy table
  mock_drafts    board-scoped
  mock_picks     unique(mock_draft_id, pick_number)
  board_shares   unique(board_id, shared_with_user_id), permission read|edit —
                 the many-to-many that makes authz interesting

Every user-owned row must trace to a user_id: that chain is what authz.py walks.

TODO (Phase 1).
"""
