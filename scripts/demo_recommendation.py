"""Drive GET /mocks/{id}/recommendation against a running API and show the effect.

The point of the endpoint is that the board MOVES as players come off it, and
a single response cannot show that. So this takes two snapshots — one on an
untouched board, one after a deliberate run on a single position — and prints
what changed between them.

    uvicorn warroom.main:app --reload            # terminal 1
    python scripts/seed_demo.py --email you@example.com
    python scripts/demo_recommendation.py --email you@example.com --password ...

It talks HTTP rather than importing the service, so what it exercises is the
real route, the real authorization and the real serialization — the same path
the frontend will take, not a shortcut around it.

Nothing here is a test. The suite already pins the arithmetic; this is for
looking at the numbers on a realistic pool and deciding whether they are
believable, which is the one thing a unit test cannot do for you.
"""

from __future__ import annotations

import argparse
import sys

import httpx

RUN_POSITION = "C"  # The slot to deliberately drain; centre is the scarcest.


class Api:
    """The handful of calls this demo makes, with the bearer token attached."""

    def __init__(self, base: str, token: str | None = None):
        self.client = httpx.Client(base_url=base, timeout=30.0)
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def post(self, path: str, json: dict) -> httpx.Response:
        return self.client.post(path, json=json, headers=self.headers)

    def get(self, path: str, **params) -> httpx.Response:
        return self.client.get(path, params=params, headers=self.headers)


def fail(message: str, response: httpx.Response | None = None) -> None:
    print(f"error: {message}", file=sys.stderr)
    if response is not None:
        print(f"  {response.status_code} {response.text[:300]}", file=sys.stderr)
    raise SystemExit(1)


def mint_token(email: str) -> str:
    """Sign a token for an existing account without knowing its password.

    Local convenience, and the reason it exists: seed_demo.py attaches a league
    to an account you already registered, and a league pulled from ESPN weeks
    ago belongs to an account whose password you very likely do not remember.
    Signing a token directly is the difference between testing against your
    real league and re-registering a throwaway one.

    Imports the app rather than going over HTTP, so it reads DATABASE_URL from
    the same .env the server does. It cannot reach a remote API and is not
    meant to: it is a shortcut past YOUR OWN login on YOUR OWN machine.
    """
    from sqlalchemy import select

    from warroom.db import SessionLocal
    from warroom.models import User
    from warroom.security import create_access_token

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            fail(f"no account for {email!r} in this database")
        return create_access_token(subject=user.id)


def authenticate(api: Api, email: str, password: str) -> str:
    """Log in, registering first if the account does not exist yet."""
    login = api.post("/auth/login", {"email": email, "password": password})
    if login.status_code == 401:
        created = api.post("/auth/register", {"email": email, "password": password})
        if created.status_code not in (201, 409):
            fail("could not register", created)
        login = api.post("/auth/login", {"email": email, "password": password})
    if login.status_code != 200:
        fail("could not log in", login)
    return login.json()["access_token"]


def pick_league(api: Api, league_id: str | None) -> dict:
    leagues = api.get("/leagues")
    if leagues.status_code != 200:
        fail("could not list leagues", leagues)
    rows = leagues.json()
    if not rows:
        fail("no leagues on this account — run scripts/seed_demo.py first")
    if league_id is None:
        return rows[0]
    match = next((row for row in rows if row["id"] == league_id), None)
    return match or fail(f"no league {league_id} on this account")  # type: ignore[return-value]


def setup_mock(api: Api, league: dict, slot: int, compute: bool = True) -> str:
    """Value the league, then lay out a fresh board and mock draft on it.

    `compute=False` reuses the valuations already cached. Worth knowing why you
    might want that: `players` is season-scoped reference data shared by every
    league, so recomputing values the WHOLE season's pool. If that season also
    holds a seeded or half-synced pool, a recompute quietly folds those players
    into your real league's replacement levels. The recommendation endpoint
    itself is immune — it inner-joins valuations, so it only ever sees players
    this league has actually valued — but the recompute is not.
    """
    if not compute:
        print("reusing the cached valuations (no recompute)")
    else:
        computed = api.post(f"/leagues/{league['id']}/valuations/compute", {})
        if computed.status_code != 200:
            fail("could not compute valuations", computed)
        print(f"valued {computed.json()['players_valued']} players")

    board = api.post("/boards", {"league_id": league["id"], "name": "Recommendation demo"})
    if board.status_code != 201:
        fail("could not create a board", board)

    mock = api.post(
        f"/boards/{board.json()['id']}/mocks",
        {"name": "Scarcity demo", "my_draft_slot": slot},
    )
    if mock.status_code != 201:
        fail("could not create a mock", mock)
    body = mock.json()
    print(f"mock laid out: {body['picks_total']} picks, you are slot {slot}")
    return body["id"]


def board_of(api: Api, mock_id: str, limit: int = 200) -> list[dict]:
    page = api.get(f"/mocks/{mock_id}/recommendation", limit=limit)
    if page.status_code != 200:
        fail("recommendation failed", page)
    return page.json()["items"]


def run_on_position(api: Api, mock_id: str, board: list[dict], count: int) -> list[str]:
    """Draft the top `count` players at RUN_POSITION, one per consecutive pick.

    Consecutive picks means consecutive TEAMS in a snake, which is what makes
    this a run rather than one team hoarding a position — the seats drain
    across the league, which is what moves the replacement level.
    """
    targets = [r for r in board if RUN_POSITION in r["player"]["positions"]][:count]
    taken = []
    for number, row in enumerate(targets, start=1):
        response = api.post(
            f"/mocks/{mock_id}/picks",
            {"pick_number": number, "player_id": row["player"]["id"]},
        )
        if response.status_code != 200:
            fail(f"could not record pick {number}", response)
        taken.append(row["player"]["name"])
    return taken


def show(title: str, rows: list[dict], limit: int = 12) -> None:
    print(f"\n{title}")
    print(f"  {'player':<22} {'pos':<8} {'slot':<6} {'proj':>8} {'repl':>8} {'value':>8} {'Δ':>8}")
    for row in rows[:limit]:
        player, live = row["player"], row["live"]
        print(
            f"  {player['name']:<22} {'/'.join(player['positions']):<8} "
            f"{live['assigned_slot']:<6} {row['valuation']['projected_points']:>8.1f} "
            f"{live['replacement_points']:>8.1f} {live['value']:>8.1f} "
            f"{live['value_change']:>+8.1f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", default="demo-password-123")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--league-id", default=None, help="defaults to the first league")
    parser.add_argument("--slot", type=int, default=1, help="your draft slot")
    parser.add_argument("--run", type=int, default=8, help=f"how many {RUN_POSITION}s go early")
    parser.add_argument(
        "--mint-token",
        action="store_true",
        help="sign a token for --email directly instead of logging in (local only)",
    )
    parser.add_argument(
        "--skip-compute",
        action="store_true",
        help="reuse cached valuations; see setup_mock for when that matters",
    )
    args = parser.parse_args()

    api = Api(args.base_url)
    api.token = (
        mint_token(args.email) if args.mint_token else authenticate(api, args.email, args.password)
    )

    league = pick_league(api, args.league_id)
    print(f"league {league['name']!r}: {league['num_teams']} teams, roster {league['roster_size']}")

    mock_id = setup_mock(api, league, args.slot, compute=not args.skip_compute)

    before = board_of(api, mock_id)
    show("BEFORE — untouched board (live == preseason, every Δ is zero)", before)

    taken = run_on_position(api, mock_id, before, args.run)
    print(f"\ndrafted {len(taken)} {RUN_POSITION}s off the top: {', '.join(taken)}")

    after = board_of(api, mock_id)
    show(f"AFTER — the run on {RUN_POSITION}", after)

    # The headline: who gained most from the run. These are the players the
    # static best-available list cannot promote, because its number was fixed
    # before the draft started.
    movers = sorted(after, key=lambda r: r["live"]["value_change"], reverse=True)[:5]
    print(f"\nBiggest gainers — what the run on {RUN_POSITION} was worth to them:")
    for row in movers:
        live = row["live"]
        print(
            f"  {row['player']['name']:<22} {'/'.join(row['player']['positions']):<8} "
            f"value {row['valuation']['value']:>7.1f} -> {live['value']:>7.1f} "
            f"({live['value_change']:+.1f}, replacement moved {live['replacement_shift']:+.1f})"
        )

    before_order = [r["player"]["name"] for r in before if r["player"]["name"] not in taken][:10]
    after_order = [r["player"]["name"] for r in after][:10]
    print("\nTop 10, before (drafted removed) vs after:")
    for rank, (was, now) in enumerate(zip(before_order, after_order, strict=False), start=1):
        marker = "  " if was == now else "->"
        print(f"  {rank:>2}. {was:<24} {marker} {now}")

    if before_order == after_order:
        print(
            f"\nThe order did not change. Try a bigger --run: the level only moves "
            f"once the {RUN_POSITION} seats run out faster than the {RUN_POSITION} talent does."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
