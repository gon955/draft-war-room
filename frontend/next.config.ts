import type { NextConfig } from "next";

// NEXT_PUBLIC_* values are INLINED INTO THE BUNDLE at build time, not read at
// runtime, so a production build with this unset ships a site that calls
// 127.0.0.1:8000 — every visitor's browser asking their own machine for the
// API. It fails as a network error with no mention of configuration, on a build
// that looked completely clean.
//
// So the build refuses instead. `next dev` is exempt: localhost is the right
// answer there, and requiring the variable to run the dev server would only
// train people to set it to something wrong.
//
// `next typegen` (the first half of `npm run typecheck`) is exempt too. It
// loads this file under the same production-build phase and NODE_ENV as a real
// build, so neither can tell the two apart — but it only writes route types to
// .next/types and emits no bundle, so there is nothing for the URL to be baked
// into. Without this, typecheck fails on any machine with no .env.local, CI
// included. The command name is the one signal that differs.
const generatingTypesOnly = process.argv.includes("typegen");

if (
  process.env.NODE_ENV === "production" &&
  !generatingTypesOnly &&
  !process.env.NEXT_PUBLIC_API_URL
) {
  throw new Error(
    "NEXT_PUBLIC_API_URL is not set.\n\n" +
      "It is baked into the JavaScript at build time, so an unset value cannot " +
      "be corrected after deploying — the site would ship pointing at " +
      "http://127.0.0.1:8000.\n\n" +
      "Set it to the API's public origin (e.g. https://warroom-api.fly.dev) in " +
      "the build environment: Cloudflare Pages > Settings > Environment " +
      "variables, or `NEXT_PUBLIC_API_URL=... npm run build` locally.",
  );
}

const nextConfig: NextConfig = {
  // Every screen is a client component fetching with a bearer token, so there
  // is no SSR to run. A static export deploys to Cloudflare Pages with no
  // adapter, no edge runtime and no cold starts — `next build` just writes out/.
  //
  // What this gives up, per the v16 static-export docs: rewrites, redirects,
  // headers, Route Handlers that read the request, Server Actions, and any
  // dynamic route segment without generateStaticParams(). The first of those is
  // why CORS on the API is mandatory rather than a preference — proxying the
  // API through Next.js is not available to us. The third is why the security
  // headers live in public/_headers, which Cloudflare Pages reads, instead of
  // in a headers() block here. The last is why the board and mock screens take
  // their ids from the query string instead of [boardId].
  output: "export",
};

export default nextConfig;
