# Draft War Room — Build Spec (points league)

A personal fantasy-basketball draft-prep tool. ESPN is the **external data source** (player pool, projections, your league's scoring settings); your app owns the **persistent, relational, authenticated** data — custom rankings, tiers, notes, mock drafts, and computed player values.

The point of this project (for your resume): a real relational schema, ownership-based authorization, an algorithmic core (player valuation), and a second tested + deployed repo — the exact gaps left after the last review. Build it tested and in CI from commit one.

> **Your league is a points league**, so the valuation engine is value-over-replacement (§5). The category (z-score) path is out of scope. The engine is already written, tested, and lint-clean — it ships alongside this spec in `warroom_valuation/` (drop it in as `warroom/valuation/`).

---

## 0. Design principles

1. **ESPN behind an adapter.** All ESPN access goes through one interface (`PlayerDataSource`). One concrete implementation (`EspnDataSource`) wraps the `espn-api` library; tests inject a fake. Your suite never touches the network, and if ESPN changes their undocumented API, one file changes.
2. **Separate reference data from user data.** The ESPN player pool is cached, season-scoped, shared reference data owned by no user. Everything a user creates (boards, rankings, mocks) is owned and access-controlled.
3. **Ownership is the authz spine.** Every user-owned row traces to a `user_id`. A request is authorized if the user owns the parent resource *or* holds a `board_share`. Denied reads return **404, not 403**, so you don't leak which resources exist.
4. **MVP first, then the one deep feature.** Ship auth + CRUD + a live board before wiring the valuation engine into the UI. Don't gold-plate.

---

## 1. Tech stack

- **Backend:** FastAPI, SQLAlchemy 2.x, Alembic (migrations), Pydantic v2.
- **DB:** PostgreSQL (use JSONB — SQLite won't match it, so run Postgres in CI too).
- **Auth:** JWT bearer, passwords hashed with Argon2 or bcrypt (`passlib`).
- **ESPN:** `espn-api` (`from espn_api.basketball import League`).
- **Frontend:** Next.js 15 (App Router) + React + TypeScript (reuse your NBA-project patterns).
- **Tests/CI:** pytest + ruff, GitHub Actions with a Postgres service container.
- **Deploy:** Postgres on Neon or Supabase; API on Railway / Render / Fly; frontend on Cloudflare Pages or Vercel.

---

## 2. ESPN integration

### 2.1 Access facts (verify against your league before building)
- Basketball is supported: `from espn_api.basketball import League`.
- **Public league:** `League(league_id, year)` — no auth.
- **Private league:** `League(league_id, year, espn_s2=..., swid=...)`. Username/password no longer works; grab the `SWID` and `ESPN_S2` cookies from your browser (DevTools → Application → Cookies → fantasy.espn.com). They persist across sessions but can't be fetched programmatically.
- Basketball game code is `fba`; the raw read endpoint is `lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{year}/segments/0/leagues/{league_id}`.
- **Read data refreshes ~once daily (~6am ET), not live.** Do **not** build live pick-by-pick draft syncing — it would depend on undocumented latency. Everything here runs off pre-fetched projections.

### 2.2 The adapter interface
The interface and the test fake are in `data_source.py`:
```python
class PlayerDataSource(Protocol):
    def get_league_settings(self, espn_league_id: int, season: int) -> LeagueSettings: ...
    def get_player_pool(self, espn_league_id: int, season: int) -> list[PlayerProjection]: ...
```
`EspnDataSource` implements this over `espn-api`. **Two points-league specifics it must get right:**
- **Read the league's own point weights, don't hardcode them.** ESPN points leagues let the commissioner set custom per-stat values. Pull them from `league.settings` and store them on the `leagues` row (`point_weights`), so valuation is reproducible and correct *for your league*.
- **Confirm whether FG%/FT% are scored.** Points leagues usually score made/missed shots (FGM/FTM/3PM), not percentages. If yours doesn't weight percentages, there's no volume-weighting to do — value is a plain linear function of counting stats.

In FastAPI, provide the data source via `Depends`; override that dependency with `FakePlayerDataSource` in tests.

### 2.3 Security for private-league cookies
`ESPN_S2` is a live session credential. Either **don't persist it** (accept it per-request / per-session), or encrypt at rest (Fernet with an app key from env). Never log it, never commit it. Handle it like a secret, not a column.

---

## 3. Relational schema

UUID primary keys throughout. `created_at` / `updated_at` on mutable tables.

**users**
- `id` PK · `email` unique (store lowercased) · `password_hash` · `created_at`

**leagues** — a saved ESPN league connection + cached settings, owned by the user who added it
- `id` PK · `user_id` FK→users · `espn_league_id` int · `season` int · `name`
- `scoring_format` enum(points, categories) · `num_teams` int · `roster_size` int
- `roster_slots` jsonb · `point_weights` jsonb · `espn_s2_encrypted` null · `created_at`
- unique(`user_id`, `espn_league_id`, `season`)

**players** — cached ESPN reference data, season-scoped, no owner
- `id` PK · `espn_player_id` int · `season` int · `name` · `pro_team`
- `positions` jsonb · `projections` jsonb · `updated_at`
- unique(`espn_player_id`, `season`) · index(`season`)

**valuations** — computed value per player per league (cache of the engine's output)
- `id` PK · `league_id` FK→leagues · `player_id` FK→players
- `projected_points` float · `replacement_points` float · `value` float · `assigned_slot` text · `computed_at`
- unique(`league_id`, `player_id`)

**boards** — a big board for a league+season (a user can keep several as strategies)
- `id` PK · `user_id` FK→users · `league_id` FK→leagues · `name` · `created_at`

**tiers**
- `id` PK · `board_id` FK→boards · `label` · `color` null · `sort_order` int

**rankings** — the CRUD-heavy table: one row per player per board
- `id` PK · `board_id` FK→boards · `player_id` FK→players
- `user_rank` int null (manual override) · `tier_id` FK→tiers null · `note` text null
- `is_target` bool · `is_avoid` bool · `created_at` · `updated_at`
- unique(`board_id`, `player_id`) · index(`board_id`, `user_rank`)

**mock_drafts**
- `id` PK · `board_id` FK→boards · `name` · `my_draft_slot` int · `created_at`

**mock_picks**
- `id` PK · `mock_draft_id` FK→mock_drafts · `pick_number` int · `round` int
- `team_slot` int · `player_id` FK→players null · `is_mine` bool
- unique(`mock_draft_id`, `pick_number`)

**board_shares** — many-to-many; the interesting authz surface
- `id` PK · `board_id` FK→boards · `shared_with_user_id` FK→users
- `permission` enum(read, edit) · `created_at`
- unique(`board_id`, `shared_with_user_id`)

Relationship graph: users → boards → rankings → players; boards → tiers; boards → mock_drafts → mock_picks → players; boards ↔ users via board_shares (M2M); leagues → players (via season) → valuations.

---

## 4. API endpoints + authorization rule per route

Auth: `Authorization: Bearer <jwt>`. Unauthenticated protected route → **401**.
"Access to board" = owner OR a `board_share` row. "Edit access" = owner OR share with `permission='edit'`. Denied access to a specific resource → **404** (don't leak existence).

**Auth**
- `POST /auth/register` — public
- `POST /auth/login` — public → `{access_token}`
- `GET  /auth/me` — authed

**Leagues**
- `POST   /leagues` — authed; owner = caller; fetches settings + player pool from ESPN
- `GET    /leagues` — authed; only caller's leagues
- `GET    /leagues/{id}` — owner only
- `POST   /leagues/{id}/sync` — owner only; refresh players/projections
- `DELETE /leagues/{id}` — owner only

**Players + valuations**
- `GET  /leagues/{id}/players` — league access; cached pool + valuations; filter by position, sort by value/rank, paginated
- `POST /leagues/{id}/valuations/compute` — owner only; runs the engine (§5), stores `valuations`
- `GET  /leagues/{id}/valuations` — league access

**Boards**
- `POST   /boards` — authed; must reference a league the caller can access; owner = caller
- `GET    /boards` — authed; boards owned by OR shared with caller
- `GET    /boards/{id}` — board access
- `PATCH  /boards/{id}` — edit access
- `DELETE /boards/{id}` — owner only

**Rankings (scoped to a board)**
- `GET   /boards/{id}/rankings` — board access
- `POST  /boards/{id}/rankings` — edit access; create/override a player's ranking
- `PATCH /rankings/{id}` — edit access to parent board
- `DELETE /rankings/{id}` — edit access to parent board
- `PATCH /boards/{id}/rankings/reorder` — edit access; bulk reorder (list of {player_id, user_rank})

**Tiers** — `GET/POST /boards/{id}/tiers`, `PATCH/DELETE /tiers/{id}` (board / edit access)

**Mock drafts**
- `POST /boards/{id}/mocks` · `GET /boards/{id}/mocks` — board / edit access
- `POST /mocks/{id}/picks` — edit access; record a pick (sets player on a pick_number)
- `GET  /mocks/{id}/best-available` — board access; players not yet picked, ordered by this board's rank/value → the "who should I take" view

**Shares (owner-only management)**
- `GET    /boards/{id}/shares` — owner only
- `POST   /boards/{id}/shares` — owner only; share with a user by email + permission
- `DELETE /shares/{id}` — owner only

---

## 5. The valuation engine — value over replacement (SHIPPED)

Ranking by raw projected points ignores position scarcity. Value over replacement (VOR) fixes that by measuring each player against the *last startable* player at their position across the whole league. A scarce position (steep drop-off) has a low replacement level, so its top players gain value — which is what makes the tool smarter than ESPN's default rank. **If you skip position scarcity and just rank by projected points, the valuation feature isn't worth claiming.**

Files (already written, 12 unit tests passing, ruff-clean): `domain.py`, `engine.py`, `data_source.py`, `tests/test_engine.py`.

### 5.1 Definitions (stated so they're defensible in review)
- **projected_points(p)** = Σ (stat × league weight) — a linear dot product of the player's projected stats and the league's own point weights.
- **replacement level at a slot** = projected points of the **last starter** at that slot league-wide, i.e. the `(num_teams × slot_count)`-th best eligible player. So the marginal starter has value 0; players above are positive, below negative.
- **multi-eligible players** are credited at their **scarcest** eligible slot — the one with the lowest replacement level — which maximizes their value over replacement (you'd play them where they help most).
- **UTIL/G/F** slots use eligibility predicates: UTIL = anyone, G = PG/SG, F = SF/PF. Bench/IR slots create no starter demand.

### 5.2 The engine (`engine.py`)
```python
NON_STARTING_SLOTS = {"BE", "BN", "BENCH", "IR", "IL", "NA"}


def default_slot_eligibility(slot, positions):
    slot = slot.upper()
    pos = {p.upper() for p in positions}
    if slot in ("UTIL", "UT"):
        return True
    if slot == "G":
        return bool(pos & {"PG", "SG", "G"})
    if slot == "F":
        return bool(pos & {"SF", "PF", "F"})
    return slot in pos


def project_points(player, weights):
    return sum(player.stats.get(s, 0.0) * w for s, w in weights.items())


def compute_replacement_levels(players, settings, points, eligibility=default_slot_eligibility):
    replacement = {}
    for slot, count in settings.roster_slots.items():
        if slot.upper() in NON_STARTING_SLOTS or count <= 0:
            continue
        eligible = [p for p in players if eligibility(slot, p.positions)]
        if not eligible:
            replacement[slot] = 0.0
            continue
        eligible.sort(key=lambda p: points[p.espn_player_id], reverse=True)
        n_starters = settings.num_teams * count
        idx = min(n_starters - 1, len(eligible) - 1)  # last starter, clamped
        replacement[slot] = points[eligible[idx].espn_player_id]
    return replacement


def value_over_replacement(players, settings, eligibility=default_slot_eligibility):
    if settings.scoring_format != "points":
        raise ValueError(f"points leagues only, got {settings.scoring_format!r}")
    points = {p.espn_player_id: project_points(p, settings.point_weights) for p in players}
    replacement = compute_replacement_levels(players, settings, points, eligibility)
    results = []
    for p in players:
        slots = [s for s in replacement if eligibility(s, p.positions)]
        best = min(slots, key=lambda s: replacement[s]) if slots else "NONE"
        repl = replacement.get(best, 0.0)
        pts = points[p.espn_player_id]
        results.append(PlayerValue(p, pts, repl, pts - repl, best))
    results.sort(key=lambda v: v.value, reverse=True)
    return results
```

### 5.3 Worked example (this is the main unit test)
`num_teams=2`, slots `PG:1 / C:1 / UTIL:1`, weight `pts:1` (so points == pts):

| pool | sorted | last starter (2nd) → replacement |
|------|--------|----------------------------------|
| PG   | 50, 40, 30 | **40** |
| C    | 45, 20, 10 | **20** |
| UTIL | 50,45,40,30,25,20,10 | **45** |

Values = points − min(replacement over eligible slots): p4 C = 45−20 = **25** (top, beats the 50-pt PG because C is scarcer) · p1 PG = 50−40 = 10 · p2 = 0 · p5 = 0 · p3 = −10 · p6 = −10 · p7 SF (UTIL only) = 25−45 = −20. Ranked ids `[4,1,2,5,3,6,7]`.

### 5.4 Tiering + overrides
Group ranked players into tiers by gaps in `value` (largest-gap splits or 1-D k-means), write `tiers` rows, set `rankings.tier_id`. The user's manual `user_rank` overrides the computed order when set — the engine seeds the board; the human edits it.

### 5.5 Extensions after MVP (optional)
Fold UTIL demand into an overall replacement level; support flex `G`/`F` scarcity explicitly; expose a "what-if" that re-values after a stat-weight tweak.

---

## 6. Test plan

Postgres in CI (service container); transactional fixture that rolls back per test. **ESPN is always faked** — inject `FakePlayerDataSource` via dependency override; no test hits the network.

**Fixtures:** `user_a`, `user_b` with tokens; a `league` owned by A with a small fixed `players` pool; a `board` owned by A; a `board_share(read)` and `board_share(edit)` to B for the authz cases.

**Auth**
- register → login → `/auth/me` returns the user
- protected route without token → 401
- duplicate email register → 409

**Authorization matrix (the ones hiring managers look for)**
- B `GET /boards/{A's board}` with no share → **404** (not 403)
- B `PATCH /rankings/{A's}` with no share → 404
- B with **read** share: `GET` board → 200; `POST /rankings` → 403
- B with **edit** share: `POST /rankings` → 200; `DELETE /boards/{id}` (owner-only) → 403
- non-owner `POST /boards/{id}/shares` → 404/403

**CRUD integration**
- create board → add ranking → reorder → patch note/tier → delete; assert `unique(board_id, player_id)` rejects a duplicate ranking
- `best-available` excludes already-picked players and orders by this board's ranks

**Valuation engine (unit — DONE, in `tests/test_engine.py`, all hand-computed):**
- `project_points` is the linear dot product; negative turnover weight lowers a score; unweighted stats are ignored
- replacement level is the last starter per slot; a scarcer position has a lower replacement level
- full ranking matches the §5.3 worked example (`[4,1,2,5,3,6,7]`)
- the scarce-C player (45 pts) outranks the deep-PG player (50 pts)
- a UTIL-only player is valued against the UTIL replacement
- a multi-eligible player is credited at their scarcest slot
- an under-filled position (fewer players than starting slots) clamps instead of raising
- a categories league raises `ValueError`
- `FakePlayerDataSource` round-trips and feeds the engine

**ESPN adapter (contract test, no network)**
- `FakePlayerDataSource` returns fixtures; assert the sync endpoint writes the expected `players` rows and settings

---

## 7. CI (`.github/workflows/ci.yml`, sketch)
```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env: { POSTGRES_PASSWORD: postgres, POSTGRES_DB: warroom_test }
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 10s
          --health-timeout 5s --health-retries 5
    env:
      DATABASE_URL: postgresql://postgres:postgres@localhost:5432/warroom_test
      JWT_SECRET: test-secret
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: ruff check .
      - run: alembic upgrade head
      - run: pytest -v
```

---

## 8. 40-day timeline (finish ~1 week before your draft)

The valuation engine and its tests are already done, so days 15–21 are lighter — spend the slack on the authz test matrix and the UI.

- **Days 1–7 — Foundation.** Postgres + Alembic, FastAPI skeleton, auth (register/login/JWT), pytest + ruff + GitHub Actions from commit one. `PlayerDataSource` interface (shipped) + `EspnDataSource` pulling settings, **point weights**, and the player pool for your league/season.
- **Days 8–14 — CRUD + authz.** Boards, rankings, tiers with ownership + share authorization; the full authz test matrix. Basic Next.js board UI (list players, set rank/tier/note).
- **Days 15–21 — Wire in valuation.** Drop in `warroom/valuation/`, add the `compute` + `GET valuations` endpoints, persist results to the `valuations` table, surface value + tier in the board UI. Auto-tiering.
- **Days 22–28 — Mocks + deploy.** Mock drafts + picks + `best-available`; deploy DB, API, frontend; smoke-test end to end.
- **Days 29–33 — Real run.** Load your actual league, dry-run a mock, fix rough edges, write the README (architecture, the adapter decision, the VOR method).
- **Days 34–40 — Buffer + use it on draft day.**

If you slip, cut mock drafts before you cut tests or the authz matrix — a smaller tested app beats a bigger untested one.

---

## 9. Resume bullets this earns (only once true)

- Built a full-stack fantasy-basketball draft tool (FastAPI · PostgreSQL · Next.js) with JWT auth and ownership/share-based authorization — enforced and tested (unauthorized access returns 404, edit vs. read shares verified across the endpoint matrix).
- Designed a normalized Postgres schema (users, leagues, boards, rankings, mock drafts, many-to-many board sharing) with Alembic migrations and indexed, paginated player queries.
- Implemented a value-over-replacement player-valuation engine that ranks against the league's own configured scoring weights and per-position replacement levels (multi-position and UTIL eligibility handled), unit-tested against hand-computed values.
- Integrated ESPN's undocumented Fantasy API behind a swappable data-source adapter, mocked in tests so CI stays green independent of the upstream API; handled private-league session credentials as encrypted secrets.
- Shipped tested (pytest) and CI-gated (GitHub Actions, Postgres service) with a live deployment.

---

*Package layout for the shipped engine: copy `warroom_valuation/` into your repo as `warroom/valuation/`. Run `pytest warroom/valuation/tests/ -v` to confirm 12 green before you build anything around it.*
