"use client";

import { useState } from "react";
import { headshotUrl } from "@/lib/espn";

/**
 * A player's ESPN headshot, or their initials when there isn't one.
 *
 * The fallback is not optional. ESPN has no headshot for every id it serves —
 * rookies and two-way players especially — and an unknown id returns 404, so
 * without this the table fills with broken-image glyphs exactly where the
 * player pool is least familiar and a face would help most.
 *
 * A plain <img>, deliberately. next/image needs a loader and an optimizer that
 * a static export (`output: "export"`) does not have, and pointing it at an
 * external CDN with `unoptimized` leaves nothing but a <img> with extra steps.
 */
export function Headshot({
  espnPlayerId,
  name,
  size = 28,
}: {
  espnPlayerId: number;
  name: string;
  size?: number;
}) {
  const [failed, setFailed] = useState(false);

  const initials = name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0] ?? "")
    .join("")
    .toUpperCase();

  if (failed) {
    return (
      <span className="shot shot-fallback" style={{ width: size, height: size }} aria-hidden>
        {initials}
      </span>
    );
  }

  return (
    // A static export has no image optimizer for next/image to call, so the
    // rule's suggestion is not available here — see the docstring above.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      className="shot"
      src={headshotUrl(espnPlayerId, size)}
      alt=""
      width={size}
      height={size}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}
