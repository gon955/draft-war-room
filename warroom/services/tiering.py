"""Group a valued pool into tiers (SPEC 5.4).

Split the ranked list on the largest gaps in `value` (or 1-D k-means), write the
tiers rows, and set rankings.tier_id. Tiers are what turn a ranked list into a
draft-day decision: within a tier the choice is a coin flip, between tiers it is
not.

The engine seeds the board; the human edits it. A manual rankings.user_rank
always overrides the computed order.

TODO (Phase 4).
"""
