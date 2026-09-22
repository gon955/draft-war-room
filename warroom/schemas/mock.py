"""Mock draft, pick and best-available payloads (SPEC 4).

PickOut carries no created_at: mock_picks is the second table without
TimestampMixin (tiers is the other), so a copied-in audit field raises at
serialization rather than at import.
"""

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from warroom.schemas.player import Page, PlayerOut, PlayerWithValuationOut
from warroom.schemas.ranking import RankingOut


class MockCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # ge=1 only. The real upper bound is the league's num_teams, which a schema
    # cannot see, so create_mock checks that end against the league row.
    my_draft_slot: int = Field(ge=1)


class MockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    board_id: uuid.UUID
    name: str
    my_draft_slot: int
    created_at: datetime


class MockWithProgressOut(MockOut):
    """MockOut plus how far the draft has got.

    Both counts come from one grouped query over mock_picks, so listing a
    board's mocks stays a single round trip rather than one per mock.
    """

    picks_total: int
    picks_made: int


class PickSet(BaseModel):
    """Record one pick, or clear it by sending player_id null.

    Clearing is not in SPEC 4, which only describes setting a pick; undoing a
    misclick mid-mock is the obvious need and the column is already nullable.
    """

    pick_number: int = Field(ge=1)
    player_id: uuid.UUID | None = None


class PickOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mock_draft_id: uuid.UUID
    pick_number: int
    round: int
    team_slot: int
    is_mine: bool
    player_id: uuid.UUID | None


class PickWithPlayerOut(PickOut):
    """A pick with the player on it, for reading the board back.

    PickOut carries player_id and nothing else, which is right for the write
    (you send an id, you get the id back) and useless for the read: a drafted
    player is by definition absent from best-available, so a client has no
    other source for the name and can only render the raw UUID.

    None when the slot is empty, which is most of the board before the draft
    starts — mock_picks rows are created at mock creation with player_id NULL.
    """

    player: PlayerOut | None


class BestAvailableOut(PlayerWithValuationOut):
    """A pool player, their valuation, and what this board says about them.

    Inherits player + valuation so the "who should I take" view and
    GET /leagues/{id}/players stay one shape plus a field, rather than two the
    frontend has to special-case.
    """

    ranking: RankingOut | None


class LiveValueOut(BaseModel):
    """A player's value against the draft board as it stands, not the preseason.

    Separate from `valuation` rather than replacing it: the preseason number is
    the baseline the shift is measured against, so a UI that wants to say "up
    90 since pick 1" needs both in the same payload.
    """

    value: float
    replacement_points: float
    assigned_slot: str
    # Movement in the replacement level this player is credited against.
    # Negative means their slot has drained, which is what lifts their value.
    replacement_shift: float
    # The resulting change in the player's own value. Positive = gained.
    value_change: float


class RecommendationOut(BestAvailableOut):
    """A best-available row plus its live, draft-aware re-valuation.

    Inherits rather than replaces so the mock screen can render one shape:
    best-available and the recommendation differ by a field, not a schema.
    """

    live: LiveValueOut
    # What this player adds to YOUR starting lineup, same units as live.value.
    # Equal to live.value while your roster is empty and diverging as it
    # fills; 0.0 means every seat they fit is held by someone better, so they
    # would be bench depth rather than an upgrade.
    marginal_value: float
    # False once no seat they fit is open or winnable — the fast read of the
    # number above.
    improves_lineup: bool
    # Whether a seat you have not filled accepts them at all. The last
    # useful signal once marginal_value has collapsed to 0 league-wide.
    fills_open_seat: bool
    # P(still on the board when you pick again), given who picks in
    # between and what those teams still need.
    survival: float
    # The best you should expect next turn if you take this player now —
    # the number that justifies taking him rather than waiting.
    expected_next: float
    # marginal_value + expected_next: what your next two picks are worth
    # together. The default sort, because a draft pick is a choice about
    # timing, not just about players.
    score: float


class SeatOut(BaseModel):
    """One starting slot, filled or empty.

    An empty seat is a row in the response rather than an omission: "you still
    need a centre" is the most useful thing this endpoint says, and a client
    cannot infer it from a list of who you have.
    """

    slot: str
    index: int
    player: PlayerOut | None


class LineupOut(BaseModel):
    """Your roster in a mock, laid out over the league's slots.

    `bench` holds players who fit no open starting slot — a roster spot with no
    lineup spot. `bench_size` is roster_size minus the starting slots, which is
    derived rather than stored because roster_slots counts starters only.
    """

    my_draft_slot: int
    roster_size: int
    starters: list[SeatOut]
    bench: list[PlayerOut]
    bench_size: int
    picks_made: int
    picks_remaining: int


class RecommendationPage(Page[RecommendationOut]):
    """A page of recommendations plus the draft context they assume.

    `picks_until_next` is what every survival number is conditioned on, so it
    travels with them: the same board reads completely differently at the turn
    (nobody picks in between) and in the middle of a round (nine do).
    """

    picks_until_next: int


class SimulateIn(BaseModel):
    """Knobs for auto-drafting the other teams. `{}` is the common case.

    `reach` is how far down its shortlist a simulated team will go: 1 makes
    every bot a pure value-maximizer, which produces a defensible board and
    the identical draft every time. The default of 3 keeps the best player the
    most likely pick while letting the board differ run to run, which is the
    only way repeated mocks tell you anything.
    """

    reach: int = Field(default=3, ge=1, le=10)
    # Pin the dice to replay one board against a different strategy of your
    # own, and see whether the difference was your pick or the randomness.
    seed: int | None = None
    # False auto-drafts YOUR picks too, i.e. runs the rest of the board out.
    stop_at_my_pick: bool = True


class MadePickOut(BaseModel):
    pick_number: int
    team_slot: int
    player: PlayerOut


class SimulateOut(BaseModel):
    """What the simulation did, and whose turn it is now."""

    picks: list[MadePickOut]
    next_pick_number: int | None
    next_is_mine: bool
    board_complete: bool


class RecommendationSort(str, Enum):
    """How to order the recommendation.

    SCORE is the default and the most complete answer: what your next two
    picks are worth together, so a player who will still be there when the
    wheel comes back is worth less of THIS pick. MARGINAL ignores timing and
    asks only "best for my roster now"; VALUE is the league-wide order with
    your roster ignored too. All three are kept because the later ones lean
    on a model of the field, and being able to drop back to the plainer
    number is how you tell whether that model is helping.
    """

    SCORE = "score"
    MARGINAL = "marginal"
    VALUE = "value"
    # score minus one standard deviation of the MODEL component of the
    # band. Ranks by what you would get in a poor outcome for the parts of
    # the projection this app inferred rather than measured, so a player
    # whose score leans on estimated stats has to be clearly ahead, not
    # marginally ahead, to outrank one whose score is measured.
    #
    # Only the model component, deliberately. The baseline band is common
    # to the whole pool, so subtracting it would shift every candidate by
    # roughly the same amount and change nothing but the axis.
    CONFIDENT = "confident"
