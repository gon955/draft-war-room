"""Tier create / patch payloads (SPEC 4).

TierOut carries no created_at on purpose: tiers is the one user-owned table
without TimestampMixin, so a copied-in audit field would raise at serialization
rather than at import.

TierCreate.sort_order is optional and means "append to the end" — the column
itself is NOT NULL, and create_tier resolves None against the board's current
highest.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class TierCreate(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    color: str | None = Field(default=None, max_length=32)
    sort_order: int | None = Field(default=None, ge=0)


class TierPatch(BaseModel):
    """Every field optional: PATCH must not blank what it does not mention.

    Same contract as RankingPatch — `color: str | None = None` cannot on its own
    tell "color omitted" from "color cleared to null", so the route applies this
    with exclude_unset=True and reads model_fields_set rather than the values.
    """

    label: str | None = Field(default=None, min_length=1, max_length=60)
    color: str | None = None
    sort_order: int | None = Field(default=None, ge=0)


class TierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    board_id: uuid.UUID
    label: str
    color: str | None
    sort_order: int


class AutoTierIn(BaseModel):
    """Knobs for the auto-tiering gap-split (SPEC 5.4).

    Both fields default, so `{}` is a valid body — the common case is "just
    tier it". pool_size caps how deep down the value list to bother grouping:
    past a certain point every player is replacement-level and a tier boundary
    between them says nothing.
    """

    n_tiers: int = Field(default=8, ge=2, le=20)
    pool_size: int = Field(default=60, ge=2, le=500)
