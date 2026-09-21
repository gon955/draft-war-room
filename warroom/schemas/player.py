"""Player and valuation read payloads, filtered and paginated (SPEC 4).

Paginated reads get an envelope (`Page`); unpaginated ones stay bare lists, so
GET /boards/{id}/rankings keeps its current shape. The difference is deliberate:
a pager needs a total, and a list that is never sliced does not.
"""

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict

from warroom.models import ReplacementBasis


class Position(str, Enum):
    """The five real positions — the only values a SQL filter can match.

    ESPN's flex slots (G, F) and combo slots (PF/C) are ENGINE vocabulary:
    data_source.positions_of keeps only these five on the players row, so
    filtering the column for "G" finds nobody. Flex filtering would have to
    expand to a set of these before it reached SQL.
    """

    PG = "PG"
    SG = "SG"
    SF = "SF"
    PF = "PF"
    C = "C"


class PlayerSort(str, Enum):
    """SPEC 4 says "sort by value or rank", but there is no league-scoped rank:
    rankings.user_rank is board-scoped and this endpoint knows only a league,
    and ESPN's own rank is not stored. `value` IS that ordinal."""

    VALUE = "value"
    PROJECTED_POINTS = "projected_points"
    NAME = "name"


class Page[ItemT](BaseModel):
    """One page of results, plus what a client needs to draw a pager."""

    items: list[ItemT]
    total: int
    limit: int
    offset: int


class PlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    espn_player_id: int
    season: int
    name: str
    pro_team: str
    positions: list[str]
    # dict[str, float] rather than dict[str, Any]: projected_stats_of already
    # drops every non-numeric value, so the column only ever holds numbers.
    projections: dict[str, float]


class ValuationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    league_id: uuid.UUID
    player_id: uuid.UUID
    projected_points: float
    replacement_points: float
    value: float
    assigned_slot: str
    # One standard deviation on `value`, in points, and the share of it
    # coming from this app's own estimators rather than ESPN. Read the
    # second one when comparing two players: the first is largely common
    # to the whole pool and cancels, the second does not.
    value_sd: float
    model_sd: float
    computed_at: datetime


class PlayerWithValuationOut(BaseModel):
    """A pool player plus this league's valuation, which may be absent.

    `valuation` is None until POST /valuations/compute has run. The pool is
    synced from ESPN and valued separately, so "synced but not yet valued" is a
    normal state the UI has to render rather than an error.

    No from_attributes here: the route builds this from a (Player, Valuation |
    None) row pair, and from_attributes on the two inner models is what lets
    the ORM objects validate into these fields.
    """

    player: PlayerOut
    valuation: ValuationOut | None


class StatCoverageOut(BaseModel):
    """How one scored stat was obtained, and how much of the scoring it carries."""

    stat: str
    weight: float
    provenance: str
    detail: str
    point_share: float


class ComputeIn(BaseModel):
    """Optional knobs for a recompute. `{}` reuses the league's own settings.

    Setting `replacement_basis` PERSISTS it on the league rather than applying
    it for one call. That is deliberate: it changes what every row in
    `valuations` means, and a cache whose rows cannot say which question they
    answer is worse than no cache.
    """

    replacement_basis: ReplacementBasis | None = None


class ComputeResult(BaseModel):
    """What the write did, not what it wrote — same shape as league.SyncResult.

    `coverage` and `estimated_share` are the honesty of the number. ESPN projects
    only 31 of its 46 scoreable stat codes, so a league can score things that are
    never projected; without this the valuation would look equally confident
    whether every stat was real or a third of the scoring was invented.
    """

    players_valued: int
    computed_at: datetime
    # What these numbers are measured against, echoed so a client never
    # has to assume which basis produced them.
    replacement_basis: ReplacementBasis
    coverage: list[StatCoverageOut]
    # Fraction of scoring that is estimated or missing. Read this before the
    # rankings: a large number means the board is a model, not a measurement.
    estimated_share: float
