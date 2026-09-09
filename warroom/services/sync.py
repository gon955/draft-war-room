"""ESPN -> database sync (SPEC 2, 4).

Takes a PlayerDataSource (injected, never constructed here) and upserts what it
returns into the leagues and players tables: settings, point weights, roster
slots, and the season-scoped player pool.

TODO (Phase 3):
  * `sync_league_settings(db, league, source)` — refresh settings + point weights
  * `sync_player_pool(db, league, source)` — upsert on (espn_player_id, season)

Two things this must handle, both observed against the live API:
  * ProjectionsUnavailable — ESPN publishes nothing for a season until close to
    opening night. Surface it as a clear 4xx, do not write an empty pool over a
    good one.
  * Partial projections are normal: ESPN projects a few hundred players, and the
    rest are genuinely replacement-level. Do not discard them.
"""
