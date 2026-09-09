"""Run the valuation engine and cache its output (SPEC 4, 5).

Loads the league's settings and cached player pool, maps rows to the engine's
domain objects, calls value_over_replacement, and upserts the valuations table
(projected_points, replacement_points, value, assigned_slot, computed_at).

The engine itself stays pure and framework-free — this is the only place that
knows both it and the DB.

TODO (Phase 4).
"""
