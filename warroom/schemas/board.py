"""Board create / read / patch payloads (SPEC 4)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BoardCreate(BaseModel):
    league_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)


class BoardPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


class BoardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    league_id: uuid.UUID
    name: str
    created_at: datetime
