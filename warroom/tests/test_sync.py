"""ESPN adapter contract at the HTTP boundary (SPEC 6).

FakePlayerDataSource returns fixtures; assert the sync endpoint writes the
expected players rows, settings and point weights, and that re-syncing updates
rather than duplicating (unique(espn_player_id, season)).

The adapter's own translation is already covered without a network call in
warroom/valuation/tests/test_data_source.py.

TODO (Phase 3).
"""
