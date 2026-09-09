"""Pydantic v2 request and response models (SPEC 1, 4).

Separate from warroom.models: those are the database's shape, these are the
API's. Keeping them apart is what stops a column rename becoming a breaking API
change, and what keeps password_hash and espn_s2_encrypted from ever being
serialized to a client.
"""
