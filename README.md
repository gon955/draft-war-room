# Draft War Room

A fantasy-basketball draft-prep tool for a points league. ESPN is the external
data source (player pool, projections, league scoring settings); this app owns
the persistent, authenticated data — custom rankings, tiers, notes, mock drafts
and computed player values.

Build spec: [`Draft_War_Room_SPEC.md`](Draft_War_Room_SPEC.md).

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
| Players, valuations, auto-tiering | done — 30 of 33 endpoints |
| Mock drafts + best-available | not implemented |
| Frontend | not started |

## Layout

```
warroom/
  main.py          FastAPI app factory
  config.py        settings from the environment
  db.py            engine, session, declarative Base
  models.py        the relational schema (SPEC §3)
  security.py      password hashing, JWT
  crypto.py        Fernet for the ESPN cookie at rest (SPEC §2.3)
  authz.py         ownership + share rules; denied reads 404, never 403
  deps.py          shared FastAPI dependencies
  schemas/         Pydantic request/response models
  api/routes/      one module per resource group (SPEC §4)
  services/        sync, valuation persistence, tiering
  valuation/       the engine — pure, framework-free, no I/O
  tests/           app-level integration tests (SPEC §6)
alembic/           migrations
frontend/          Next.js (not scaffolded yet)
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
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # then fill it in
pytest -v
ruff check .
uvicorn warroom.main:app --reload
```

Postgres is required rather than preferred: the schema is JSONB-heavy, so CI
runs a Postgres 16 service container rather than a throwaway SQLite file.

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
