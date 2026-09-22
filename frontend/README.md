# Frontend — Draft War Room

Next.js 16.3.5 (App Router) + React 19.2 + TypeScript, per SPEC 1.

> SPEC 1 says "Next.js 15". `create-next-app@latest` now scaffolds 16, and every
> breaking change in the v16 upgrade guide applies to *migrating* an existing
> app rather than starting one — no sync request APIs, no middleware, no
> `next/image`, no caching APIs here. Amend the spec line rather than carry a
> superseded major.

## Running it

```bash
# terminal 1 — the API (the frontend talks to it cross-origin)
cd .. && uvicorn warroom.main:app --reload

# terminal 2
npm run dev          # Turbopack by default in 16; no --turbopack flag
npm run types        # regenerate src/lib/types.ts from the live OpenAPI
npm run build        # writes out/ — a static export, no server
```

`NEXT_PUBLIC_API_URL` lives in `.env.local` (git-ignored). The `NEXT_PUBLIC_`
prefix is required: without it the value is server-only and reads as `undefined`
in the browser.

## Why a static export

Every screen is a client component holding a bearer token, so there is no SSR to
run. `output: "export"` therefore costs nothing and buys a deploy with no
adapter, no edge runtime and no cold starts.

Two consequences worth knowing before you add a screen:

- **No rewrites.** Proxying the API through Next.js is not available, which is
  why the API carries CORS middleware rather than this app carrying a proxy.
- **No dynamic segments without `generateStaticParams()`.** Board and mock ids
  do not exist at build time, so those screens take them from the query string
  (`/board?id=…`) and read them with `useSearchParams()` inside a `<Suspense>`
  boundary.

## Screens, in the order SPEC 8 wants them

1. Login / register.
2. Connect a league (ESPN league id + season), then its player pool.
3. Board view — the real work. Player list with computed value and tier, inline
   edit of rank, tier and note, target / avoid flags.
4. Mock draft view with best-available.

## dev.html

Not part of this app. A throwaway console for exercising the API by hand — one
static file, no build step, served by the API itself at `/dev` because it is
same-origin there and needs no CORS entry. It covers the 34 endpoints that
existed when it was written, not `GET /mocks/{id}/recommendation`. Delete it
once the screens above work.
