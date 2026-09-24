/**
 * Explains the Conf column. Shared, so the board and the mock draft cannot
 * drift into describing the same number two different ways — they show the
 * identical figure for a player, in every round.
 */
export function ConfidenceLegend() {
  return (
    <p className="muted legend">
      <b>Conf</b> is how many of a player&rsquo;s projected points rest on stats the engine
      estimated rather than ones ESPN projects — rebound splits, double-doubles. Compare players
      on this rather than on the full error bar: the rest of the uncertainty is common to the pool
      and cancels.{" "}
      <span className="conf conf-high">under 2% of projection</span>,{" "}
      <span className="conf conf-medium">2–5%</span>,{" "}
      <span className="conf conf-low">over 5%</span>,{" "}
      <span className="conf conf-unknown">not projected at all</span>.
    </p>
  );
}
