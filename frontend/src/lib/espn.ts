// ESPN player headshots.
//
// There is no API call here and no new column on the players table: the
// headshot is a static CDN path keyed by the ESPN player id we already store,
// so the URL is derivable from data every screen already has.
//
//     https://a.espncdn.com/i/headshots/nba/players/full/3112335.png
//
// The raw file is ~250KB, which is fine once and ruinous fifty times down a
// table, so everything goes through ESPN's `combiner` endpoint instead. It
// resizes and re-encodes server side: the same headshot at 48x48 and quality
// 40 is 3.8KB, measured, which is a ~66x saving per row.
//
// An unknown id 404s (verified), so every caller needs a fallback — see the
// Headshot component, which is why this module exports the URL and not an
// <img>.

const CDN = "https://a.espncdn.com";

/** A square, cropped headshot for a player, sized for a table row. */
export function headshotUrl(espnPlayerId: number, size = 48): string {
  const path = `/i/headshots/nba/players/full/${espnPlayerId}.png`;
  const params = new URLSearchParams({
    img: path,
    w: String(size * 2), // 2x for retina; still only a few KB at this quality.
    h: String(size * 2),
    scale: "crop",
    cquality: "40",
  });
  return `${CDN}/combiner/i?${params}`;
}
