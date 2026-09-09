"""The engine wired to HTTP (SPEC 6).

  POST /leagues/{id}/valuations/compute persists one row per player
  recomputing updates rather than duplicating (unique(league_id, player_id))
  GET /leagues/{id}/players sorts by value, filters by position, paginates
  a categories league returns a clean 4xx, not a 500 from the engine's ValueError

TODO (Phase 4).
"""
