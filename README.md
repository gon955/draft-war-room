# Draft War Room

A fantasy-basketball draft-prep tool for a points league. ESPN is the external
data source (player pool, projections, league scoring settings); this app owns
the persistent, authenticated data — custom rankings, tiers, notes, mock drafts
and computed player values.

## Status

| Area | State |
|------|-------|
| Valuation engine (VOR) | shipped, unit-tested |
| ESPN adapter | shipped, contract-tested without the network |
| Schema + migrations | all 10 tables, Alembic, no model drift |
| Auth (register / login / JWT) | done |
| Ownership + share authorization | done, full SPEC 6 matrix tested |
| Leagues, boards, rankings, shares | done |
| Tiers | done |
| Players, valuations, auto-tiering | done |
| Mock drafts + best-available | done — SPEC §4 complete |
| Draft-aware recommendation | live replacement level shipped; roster fit and survival next |
| Frontend | all four SPEC §8 screens built — Next.js 16 static export |

The API is 35 endpoints: all 32 in SPEC §4, plus three additions the spec
implies but does not enumerate — `POST /boards/{id}/tiers/auto` (auto-tiering,
SPEC §5.4), `GET /mocks/{id}/picks` (§4 records picks but never reads the board
back) and `GET /mocks/{id}/recommendation` (below).

## Layout

```
warroom/
  main.py          FastAPI app factory, probes, CORS
  config.py        settings from the environment
  db.py            engine, session, declarative Base
  models.py        the relational schema
  security.py      password hashing, JWT
  crypto.py        Fernet for the ESPN cookie at rest
  throttle.py      per-address auth rate limiting
  authz.py         ownership + share rules; denied reads 404, never 403
  deps.py          shared FastAPI dependencies
  schemas/         Pydantic request/response models
  api/routes/      one module per resource group
  services/        sync, valuation persistence, tiering, recommendation,
                   simulation, lineup
  valuation/       the engine — pure, framework-free, no I/O
    engine.py        value over replacement
    stats.py         league scoring vs. what ESPN projects; uncertainty bands
    availability.py  games-played model (off by default)
    draft.py         live values, survival, roster fit during a draft
    advice.py        the recommendation ranking, greedy and rollout
    data_source.py   the ESPN adapter
  tests/           app-level integration tests
alembic/           migrations
frontend/          Next.js 16 App Router; static export, types from OpenAPI
scripts/
  seed_demo.py           demo league without touching ESPN
  demo_recommendation.py show the recommendation moving as a board drains
  calibration/           fit and check the engine's constants on past seasons
```

Two seams do most of the architectural work:

- **`valuation/` knows nothing about the web or the database.** Plain
  dataclasses in, ranked values out. That is why its tests run in
  milliseconds with no fixtures.
- **`valuation/data_source.py` is the only module that speaks ESPN.** Everything
  else depends on the `PlayerDataSource` protocol, so tests inject a fake and CI
  stays green no matter what the undocumented upstream API does.

## Development

```bash
docker compose up -d                                  # Postgres on :5433
                                                      # (or: docker-compose up -d)
pip install -r requirements.lock -r requirements-dev.txt
cp .env.example .env                                  # then fill it in
alembic upgrade head
pytest -v
ruff check .
uvicorn warroom.main:app --reload

cd frontend && npm install && npm run dev             # :3000
```

That first line is the step that is easy to skip and hard to diagnose
without. It brings up Postgres 16 on **5433** — the port `.env.example` already
points at, deliberately not 5432, which is usually already taken by something
else — and creates both `warroom` and `warroom_test` (the suite refuses to run
against a database whose name does not end in `_test`).

Postgres is required rather than preferred, and the floor is **15**: the schema
is JSONB throughout, and `rankings`' foreign key uses `ON DELETE SET NULL
(tier_id)`, which is PG15+ syntax.

### When the frontend says it cannot reach the API

`Cannot reach the API at http://127.0.0.1:8000` means `fetch` itself threw, and
the browser gives JavaScript no reason why. Check, in order:

1. **`curl localhost:8000/ready`.** `{"database":"unreachable"}` means the API
   is fine and Postgres is not — usually `docker compose up -d` was skipped, or
   `DATABASE_URL` names the wrong port. This is the common one, and it used to
   be indistinguishable from a CORS problem: a 500 raised inside a route was
   answered by Starlette from outside the CORS middleware, so it reached the
   browser with no `Access-Control-Allow-Origin` and was rejected before any
   JavaScript could read the status. `main.py` now catches those inside the
   CORS layer, so a server error arrives as a server error.
2. **Is the API running at all?** `curl localhost:8000/health`.
3. **Origin mismatch.** `http://localhost:3000` and `http://127.0.0.1:3000` are
   different origins to a browser. `CORS_ORIGINS` defaults to both; if you set
   it by hand, set both.

## Deploying

Two artefacts, deployed separately: a container for the API and a static export
for the frontend. Neither is wired to a CD pipeline — CI builds and tests both,
but the deploys below are run by hand.

### Requirements

**Postgres 15 or newer.** Not a preference: `rankings`' foreign key uses
`ON DELETE SET NULL (tier_id)`, and the column list in that clause is PG15+
syntax. The schema is also JSONB throughout, so SQLite cannot run this app at
all. CI runs Postgres 16.

### API

```bash
fly secrets set \
  DATABASE_URL="postgresql+psycopg://..." \
  JWT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  CORS_ORIGINS="https://your-site.pages.dev"
fly deploy
```

`fly.toml`'s `release_command` runs `alembic upgrade head` in a one-off machine
and only promotes the release if it exits 0.

| Variable | | |
|---|---|---|
| `DATABASE_URL` | **required** | Postgres 15+, `postgresql+psycopg://` |
| `JWT_SECRET` | **required** | ≥32 bytes |
| `CORS_ORIGINS` | **required in production** | the frontend's origin. Defaults to localhost only, so an unset value blocks the deployed site — and the browser reports it as a generic network error that never says CORS |
| `FERNET_KEY` | optional | without it, connecting a *private* league returns 503; public leagues are unaffected |
| `ESPN_S2` / `ESPN_SWID` | optional | fallback cookies for leagues with none of their own |
| `CORS_ORIGIN_REGEX` | optional | for Pages preview hosts, which an exact list cannot match |
| `DOCS_ENABLED` | optional | `false` in production — `/docs` publishes every route and schema |

Everything else is in `.env.example`.

**On Railway or Render** the Dockerfile is the whole story: both inject `PORT`,
which the CMD reads. Set the same variables, and run `alembic upgrade head` as
a release/pre-deploy command — not at boot, or replicas race for the version
row.

### Frontend

```bash
cd frontend
NEXT_PUBLIC_API_URL=https://warroom-api.fly.dev npm run build   # writes out/
```

`NEXT_PUBLIC_*` is **inlined into the JavaScript at build time**, not read when
the page runs. An unset value would ship a site whose every visitor's browser
calls `127.0.0.1:8000`, so `next.config.ts` refuses the build outright. Set it
in Cloudflare Pages → Settings → Environment variables; changing it means
rebuilding, not restarting.

The build also writes `out/_headers` (CSP, HSTS, frame-ancestors) with the API
origin substituted into `connect-src`. A static export cannot send headers on
its own — `output: "export"` disables `next.config`'s `headers()` — so this
file is the only thing that sends them, and only Cloudflare Pages reads it.

Whatever `NEXT_PUBLIC_API_URL` is set to must also appear in the API's
`CORS_ORIGINS`, from the other side.

### Probes

| | |
|---|---|
| `GET /health` | liveness. Touches nothing else. A failure means the process is wedged, so this is what the platform's restart check watches. |
| `GET /ready` | readiness. `SELECT 1` through the request pool; 503 when the database is unreachable. |

`fly.toml` points its check at `/health` deliberately. Restarting an app
machine does not fix Postgres, and with every machine sharing one database a
readiness check would make them all restart together — a crash loop on top of
an outage. `/ready` is for **alerting**: it is what tells "the database is
gone" apart from "the app is gone".

## Simulated teams: whose valuation the bots use

`POST /mocks/{id}/simulate` takes `bot_valuation`:

| | |
|---|---|
| `engine` (default) | this app's projections, reconciled against the league's scoring by `stats.py` |
| `espn` | ESPN's own projected fantasy total, already scored under this league's settings |

ESPN's number arrives in the same payload as the raw stat line and used to be
discarded; it is now kept on `players.espn_projected_points`. It is **not** an
independent opinion. It is the same projected stat line scored with this
league's weights, with every stat ESPN does not project — offensive and
defensive rebounds, double-doubles, triple-doubles — scored as zero: exactly
the gap `stats.py` exists to fill. Checked to 0.1% for every projected player
in 2024–2026 (`python -m scripts.calibration.baseline`). In this league that
takes Jokić from 4,262 to 2,466 and Gobert from 2,257 to 990, and guards
barely at all.

That makes it the wrong number for valuing a player and the right one for
predicting the room: it is what ESPN shows managers while they draft. A room
drafting off it lets rebounders slide, which is the effect both this mode and
the survival model below are meant to capture.

**Only the per-player strength input changes.** Live replacement levels, the
marginal basis and roster fit are all derived *from* that mapping, so the bots
still re-price every slot after every pick and still fill their lineup before
taking depth — they just disagree with you about who is good. On the real
league, first ten picks:

```
#   bots on OUR value        bots on ESPN value       straight down ESPN's list
1   Nikola Jokic             Nikola Jokic             Shai Gilgeous-Alexander
2   Victor Wembanyama        Shai Gilgeous-Alexander  Nikola Jokic
...
8   Jalen Duren              Kawhi Leonard            Jalen Brunson
9   Karl-Anthony Towns       Tyrese Maxey             Donovan Mitchell
10  Anthony Davis            Jalen Brunson            Jamal Murray
```

The ESPN column is neither of the other two: it takes Jokić over SGA because
centre is the scarcer slot, which ESPN's flat list does not know, and then
diverges from our board toward the wings and guards ESPN rates highly. That is
the point — a room of bots using your own numbers agrees with you by
construction and can never show you the player your league will let slide.

`espn` needs no valuations computed, but it does need a pool synced since the
column was added; until then it answers 422 naming `/sync`.

## Recommendation order

`GET /mocks/{id}/recommendation` takes two knobs:

| `sort` | |
|---|---|
| `score` (default) | what your next two picks are worth together — a player likely to be there when the wheel comes back is worth less of *this* pick |
| `marginal` | best for your roster now, ignoring timing |
| `value` | the league-wide order, ignoring your roster too |
| `confident` | `score` less half a band of the player's comparative uncertainty (`stats.comparative_sd`) |

| `depth` | |
|---|---|
| `greedy` (default) | each candidate priced against your roster as it stands |
| `rollout` | the top candidates also played forward: the field drafts to your next turn on ESPN's numbers, you take your best response, and candidates are ranked by the lineup the two picks leave. Sees what greedy cannot — taking a big now can make next turn's big a bench piece — for a few hundred milliseconds |

The ranking itself lives in `valuation/advice.py`, framework-free, so the
strategy harness in `scripts/calibration` measures exactly what the API serves.

## Predicting what the room will take

`GET /mocks/{id}/recommendation` reports `survival` — P(this player is still
there at your next pick) — and `score` is docked by it. That estimate used to
decay each opponent's interest by a player's rank on *our* board, which assumes
the other managers share our valuation. They do not; that is the premise of the
app, and the error was largest for exactly the players the recommendation tells
you to target.

Survival is now read off the market's board (`players.espn_projected_points`),
blended by `DRAFT_MARKET_WEIGHT` — 1.0 full market, 0.0 the old behaviour.
Which seats each opponent still needs is read off our own eligibility rules
either way: that is roster shape, not an opinion about value.

Calibrated against 200 simulated continuations of a real league, from a
12-pick-deep board with 16 opponent picks before the next turn:

| survival predicted from | MAE vs realised |
|---|---|
| our own board | 0.232 |
| the market's board | **0.150** |

The players it moves are the ones you would expect:

```
player             our #   mkt #   realised   old pred   new pred
Rudy Gobert           11     105       1.00       0.80       1.00
Ivica Zubac           16     102       1.00       0.94       1.00
Donovan Clingan       20     125       1.00       0.98       1.00
```

Gobert was being docked for a 20% chance of being taken that did not happen
once in 200 simulations. Both variants still over-predict survival by about
+0.13 on average — that is the independence approximation in the model and is
unaffected by this change; `DEFAULT_FIELD_SPREAD` is the knob for it.

The calibration assumes a room that drafts on ESPN. If yours drafts on
reputation or a different ranking, lower `DRAFT_MARKET_WEIGHT`.

## Injury status

The adapter now keeps ESPN's `injuryStatus` (`ACTIVE` / `DAY_TO_DAY` / `OUT`)
on `players.injury_status`, and the board and mock draft show a tag for
anything other than ACTIVE. It arrives in the payload we already fetch and was
being discarded.

**It does not feed the valuation**, and that is deliberate — though the reason
has changed. It used to be that ESPN's projection already prices games missed,
so discounting again would double-count; measured against the full projected
line, players still play only ~88% of the games ESPN projects, so that was
wrong. The real obstacle is that ESPN only serves a player's *current* flag:
there is no record of what it said before any past season, so there is nothing
to fit a discount against. It is displayed because it is the one thing on the
row that can make you skip a player the numbers like. Nothing renders for a
healthy player or for a pool synced before the column existed — a green
"healthy" badge on a null would claim more than the data supports.

## Availability

`AVAILABILITY_MODEL=true` scales each player's projected totals to the share of
their projected games players like them actually played — by projected minutes
and a 3-season weighted share of games played — before values are computed
(`warroom/valuation/availability.py`, fitted by
`python -m scripts.calibration.availability`). **It is off**, on held-out
evidence: this app's projections run 20–25% high and the model fixes that, but
so does a flat haircut, and a flat haircut changes no ranking. What only a
per-player model could add is better *order*, and on each of 2024–2026 held out
from its fit it ranked the 130 players a draft takes no better than the flat
haircut (rank correlation +0.007 on average; worse in 2024). Season-wide swings
in games played dwarfed the differences between kinds of player. Turn it on for
honest point totals, not better picks, and re-fit when a season closes.

## Calibration

The constants in `valuation/stats.py` and `valuation/availability.py` are
measured, not chosen, against past seasons of a real league:

```bash
python -m scripts.calibration.fetch          # pull 2024-2026 once (needs ESPN_S2 / ESPN_SWID)
python -m scripts.calibration.baseline       # reproduce the current constants first
python -m scripts.calibration.oreb_share     # offensive rebound share
python -m scripts.calibration.uncertainty    # per-player uncertainty buckets
python -m scripts.calibration.availability   # games-played model
python -m scripts.calibration.strategy       # whole drafts: value vs greedy vs rollout
```

`fetch` is the only one that touches the network; the rest read the cache in
`scripts/calibration/data/` (gitignored). `strategy` reads the league's pool
and valuations from the database. Each script fits on some seasons and reports
on the held-out ones.

## Operational limits

The defaults in `warroom/config.py` are sized for one 512 MB / 1 shared-CPU
container. Three of them were set from measurement rather than taste, and each
is a way the app falls over if it is changed without the others.

**Password hashing.** passlib's argon2 defaults are `m=65536 KiB` (64 MiB per
hash) with nothing bounding concurrency. 20 concurrent unauthenticated
`POST /auth/login` measured at **899 MiB RSS** — an OOM kill available to
anyone with curl and no account. The app now sets OWASP's `m=19456, t=2, p=1`
and caps concurrent hashes at 4, so the peak is bounded by

    PASSWORD_HASH_CONCURRENCY x ARGON2_MEMORY_KIB

That was not the whole story. With the cap in place the same 120 logins still
settled at **455 MiB and stayed there** — glibc gives every thread its own
malloc arena and never returns what is freed inside one, so each of the 32
worker threads ends up holding a 19 MiB hole it will not reuse. `Dockerfile`
sets `MALLOC_ARENA_MAX=2`, which takes the identical run to **94 MiB**. The
cap bounds what is *in use*; the arena limit bounds what is *kept*, and only
both together fit in 512 MB.

| | peak RSS, 120 logins |
|---|---|
| passlib defaults, unbounded | 899 MiB |
| + OWASP cost, concurrency capped at 4 | 455 MiB (retained) |
| + `MALLOC_ARENA_MAX=2` | 94 MiB |

Existing hashes keep verifying at whatever cost they were written with — argon2
stores its parameters in the hash — and are rewritten to the current cost on
the owner's next successful login. `AUTH_MAX_ATTEMPTS` sits in front of all of
this, per address per window.

**Request concurrency.** Every route is a sync `def`, so each in-flight request
holds one AnyIO worker thread *and* one database connection. The invariant:

    DB_POOL_SIZE + DB_MAX_OVERFLOW  >=  REQUEST_THREAD_LIMIT  >=  fly.toml hard_limit

AnyIO defaults to 40 threads and SQLAlchemy to 15 connections, so out of the
box the 16th concurrent request waited 30s for a connection and then failed
with what looked like a database outage.

**ESPN timeouts.** `espn-api` issues bare `requests.get()` with no timeout on
any path and no way to pass one, so a connection ESPN accepts and never answers
held a worker thread for the life of the process. `ESPN_TIMEOUT` is enforced by
a shim over the `requests` module in `valuation/data_source.py`; a timeout
surfaces as **504**, not a 500.

**CPU.** `POST /mocks/{id}/simulate` with `stop_at_my_pick=false` runs a whole
draft in pure Python. It used to be the most expensive thing the app could be
asked to do; profiling found the cost was almost entirely repeated work rather
than the algorithm.

| 12 teams x 13 rounds, 400 players, `marginal` basis | |
|---|---|
| `autodraft`, before | 4.28s (27.4 ms/pick) |
| `autodraft`, after | 0.23s (1.5 ms/pick) |
| end to end over HTTP | 5.34s -> 0.43s |
| 3 concurrent drafts, worst `/health` | 1.27s -> 0.07s |

Three fixes in `valuation/engine.py`, none of which changes an answer — the
simulated draft is identical pick for pick under the same seed:

1. `default_slot_eligibility` is `lru_cache`d. It is pure, over a domain of
   under a hundred distinct answers, and was being recomputed from string
   `.upper()`/`.split()` 1.4 million times per draft.
2. `_seat_player` no longer re-sorts the chair list on every level of its
   recursion — the ordering cannot change during a seating, so it is computed
   once in `optimal_seating`. That alone was 2.09M sorts and 15.8M key calls.
3. `optimal_seating` stops once every chair is taken. `_seat_player` can only
   succeed by finding a free chair somewhere along the augmenting path, so
   every player after that point was being walked through the full search only
   to be rejected.

`_seat_player` call count for one draft went from 2,086,429 to 23,491. The
remaining cost is `live_values` rebuilding the whole ranked board after each
pick when `_bot_pick` consumes only its top few entries; at 1.5 ms/pick that is
no longer worth restructuring for.

## The valuation method

Ranking by projected points ignores position scarcity. Value over replacement
measures each player against the last *startable* player at their position
across the whole league, so a scarce position with a steep drop-off lifts its
top players. Multi-eligible players are credited at their scarcest slot.

On this league's real 2026 projections the difference is visible immediately:

```
Nikola Jokic      proj 2466.3   replacement 1339.1 (PF/C)   value 1127.2
Shai Gilgeous-A.  proj 2664.9   replacement 1615.8 (UT)     value 1049.1
```

Jokić ranks first despite roughly 200 fewer projected points, because center is
the scarcer slot. Weights are read from the league rather than hardcoded — this
league scores made and missed shots (`fgm 2.0 / fgmi -0.5`, `3pm 3.0 / 3pmi
-1.3`) and no raw points at all, so any hardcoded `pts: 1.0` would be wrong for
every player.
