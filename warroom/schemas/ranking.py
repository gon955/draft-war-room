"""Ranking create / patch and the bulk reorder payload (SPEC 4)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RankingCreate(BaseModel):
    player_id: uuid.UUID
    user_rank: int | None = Field(default=None, ge=1)
    tier_id: uuid.UUID | None = None
    note: str | None = None
    is_target: bool = False
    is_avoid: bool = False


class RankingPatch(BaseModel):
    """Every field optional: PATCH must not blank what it does not mention.

    `model_fields_set` is what distinguishes "note omitted" from "note cleared
    to null", so routes apply this with exclude_unset=True.
    """

    user_rank: int | None = Field(default=None, ge=1)
    tier_id: uuid.UUID | None = None
    note: str | None = None
    is_target: bool | None = None
    is_avoid: bool | None = None


class ReorderItem(BaseModel):
    player_id: uuid.UUID
    user_rank: int = Field(ge=1)


class ReorderIn(BaseModel):
    items: list[ReorderItem] = Field(min_length=1)


class RankingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    board_id: uuid.UUID
    player_id: uuid.UUID
    tier_id: uuid.UUID | None
    user_rank: int | None
    note: str | None
    is_target: bool
    is_avoid: bool
    created_at: datetime
    updated_at: datetime
