"""Shared FastAPI dependencies (SPEC 2.2, 4).

TODO (Phase 2-3):
  * `get_current_user` — decodes the bearer token, 401 when absent or invalid.
  * `get_data_source` — yields the PlayerDataSource implementation. Production
    wires EspnDataSource; tests override THIS dependency with
    FakePlayerDataSource, which is what keeps the suite off the network.

Routes depend on the interface, never on EspnDataSource directly — otherwise the
override has nothing to grab and the adapter seam stops being a seam.
"""
