/**
 * ESPN's injury flag, as of the last sync.
 *
 * Deliberately separate from the valuation: ESPN's projection already prices
 * their own view of games missed, so this must not be read as a second
 * discount. It is here because it is the one thing on the row that can make
 * you skip a player the numbers like, and it was being thrown away by the
 * adapter until now.
 *
 * Nothing renders for ACTIVE, and nothing renders for a pool synced before the
 * column existed. Both are the same absence on purpose — a green "healthy"
 * badge on a null would be a claim the data does not support.
 */
export function InjuryTag({ status }: { status?: string | null }) {
  if (!status || status === "ACTIVE") return null;

  const out = status === "OUT";
  return (
    <span
      className={`tag ${out ? "inj-out" : "inj-dtd"}`}
      title={
        out
          ? "ESPN lists this player as OUT as of the last sync. Their projection does not know that."
          : `ESPN lists this player as ${status.replace(/_/g, " ").toLowerCase()} as of the last sync.`
      }
    >
      {out ? "OUT" : "DTD"}
    </span>
  );
}
