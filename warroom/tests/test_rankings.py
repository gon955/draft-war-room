"""Ranking CRUD — the heaviest write path (SPEC 6).

  create board -> add ranking -> reorder -> patch note/tier -> delete
  a duplicate ranking is rejected by unique(board_id, player_id)
  reorder is atomic: a bad player_id in the batch changes nothing

TODO (Phase 3).
"""
