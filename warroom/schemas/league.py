"""League create / read payloads, incl. settings and point weights (SPEC 4).

LeagueOut's omission of espn_s2_encrypted is load-bearing, not an oversight:
ESPN_S2 is a live session credential and no endpoint returns it (SPEC 2.3).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from warroom.models import ReplacementBasis, ScoringFormat


class LeagueCreate(BaseModel):
    espn_league_id: int = Field(gt=0)
    season: int = Field(ge=2000, le=2100)
    name: str = Field(min_length=1, max_length=120)
    espn_s2: str | None = Field(default=None, repr=False)


class LeagueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    espn_league_id: int
    season: int
    name: str
    scoring_format: ScoringFormat
    num_teams: int
    roster_size: int
    roster_slots: dict[str, int]
    point_weights: dict[str, float]
    replacement_basis: ReplacementBasis
    created_at: datetime


class SyncResult(BaseModel):
    players_synced: int
    scoring_format: ScoringFormat
    synced_at: datetime
