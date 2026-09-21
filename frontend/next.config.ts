import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Every screen is a client component fetching with a bearer token, so there
  // is no SSR to run. A static export deploys to Cloudflare Pages with no
  // adapter, no edge runtime and no cold starts — `next build` just writes out/.
  //
  // What this gives up, per the v16 static-export docs: rewrites, redirects,
  // headers, Route Handlers that read the request, Server Actions, and any
  // dynamic route segment without generateStaticParams(). The first of those is
  // why CORS on the API is mandatory rather than a preference — proxying the
  // API through Next.js is not available to us. The last is why the board and
  // mock screens take their ids from the query string instead of [boardId].
  output: "export",
};

export default nextConfig;
