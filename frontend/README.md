# Frontend — Draft War Room

Next.js 15 (App Router) + React + TypeScript, per SPEC 1. Not scaffolded yet;
SPEC 8 puts it after the API is real, which is deliberate — a UI over endpoints
that do not exist yet is the fastest way to build the wrong ones.

Scaffold with:

    npx create-next-app@latest . --typescript --app

Screens, in the order SPEC 8 wants them:

1. Login / register.
2. Connect a league (ESPN league id + season), then its player pool.
3. Board view — the real work. Player list with computed value and tier, inline
   edit of rank, tier and note, target / avoid flags.
4. Mock draft view with best-available.

`node_modules/` and `.next/` are already git-ignored at the repo root.
