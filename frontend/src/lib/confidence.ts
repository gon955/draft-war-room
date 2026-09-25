// How much of a valuation is this app's own guesswork.
//
// Every valuation carries two error bars (see warroom/valuation/stats.py):
//
//   value_sd  the whole one-standard-deviation band on `value`
//   model_sd  the part of that band contributed by THIS app's estimators
//             rather than by ESPN's projections
//
// model_sd is the one this column shows. It is not the only uncertainty that
// survives a comparison — a player on few projected minutes, or coming off a
// short season, is less settled than a heavy-minutes starter, and value_sd is
// wider for them — but it is the one that is this app's own doing, which is
// what "Conf" claims to measure. The API's `sort=confident` recommendation
// order accounts for both (warroom/valuation/stats.py, comparative_sd); no
// screen requests it yet. model_sd comes from
// stats ESPN never projects and the engine has to model. In a league scoring
// oreb/dreb/dd/td that is a sixth of all scoring, and it lands very unevenly:
// two players on the same value are not equally knowable, because a rebounding
// centre's number leans hard on a rebound-split model and a guard's barely does.
//
// MEASURED AGAINST PROJECTED POINTS, not against value, and that is the whole
// design. Value is the obvious denominator and it does not work:
//
//   * it goes to zero and then negative as a draft runs, because replacement
//     level rises under everybody. Graded against live value, every candidate
//     was "low" by round 8 and "unknown" once the board filled — the column
//     stopped saying who to trust and started saying how late it was.
//   * graded against PRESEASON value it fails the same way for exactly the
//     players you are choosing between late on, who are all below replacement
//     and so all undefined.
//
// Against projected points the share is defined for anyone ESPN projects at
// all, stays put as the draft runs, and is a property of the PLAYER rather than
// of the board — so the same player reads the same on every screen, in every
// round. On this league it spreads from 1.0% (Jordan Poole, Jalen Brunson —
// guards, almost entirely projected) to 8.8% (Mitchell Robinson, Steven Adams —
// rebounding bigs, heavily modelled), which is the distinction worth drawing.

export type Confidence = "high" | "medium" | "low" | "unknown";

// Cut from the real distribution over 325 valued players rather than picked for
// roundness: the median is 2.4% and the 90th percentile 5.7%, so these put
// roughly the cleanest third in "high" and flag the model-heaviest sixth as
// "low". They are absolute on purpose. A league scoring nothing the engine has
// to estimate lands everyone at 0% and reads all-high, correctly; a league
// scoring mostly estimated stats reads mostly low, also correctly.
const HIGH = 0.02;
const MEDIUM = 0.05;

/**
 * The share of a player's projected production that the engine had to model.
 *
 * Null when ESPN projects nothing for them at all — the pool carries a few
 * dozen such players, and 0/0 would read as perfect confidence when the truth
 * is that there is no information either way.
 */
export function estimatedShare(projectedPoints: number, modelSd: number): number | null {
  if (!Number.isFinite(projectedPoints) || !Number.isFinite(modelSd)) return null;
  if (projectedPoints <= 0) return null;
  return modelSd / projectedPoints;
}

export function confidenceOf(projectedPoints: number, modelSd: number): Confidence {
  const share = estimatedShare(projectedPoints, modelSd);
  if (share === null) return "unknown";
  if (share < HIGH) return "high";
  if (share < MEDIUM) return "medium";
  return "low";
}

/**
 * The tooltip. Long on purpose: the column is four characters wide and the
 * number in it is the least self-explanatory thing on the board.
 */
export function confidenceTitle(projectedPoints: number, modelSd: number): string {
  const share = estimatedShare(projectedPoints, modelSd);
  const band =
    `±${modelSd.toFixed(0)} pts of this player's projection comes from stats the ` +
    `engine estimated rather than ones ESPN projects — rebound splits, double-doubles.`;

  if (share === null) {
    return `${band}\n\nESPN projects nothing for this player, so there is no way to tell either way.`;
  }

  return (
    `${band}\n\nThat is ${(share * 100).toFixed(1)}% of their ${projectedPoints.toFixed(0)} ` +
    `projected points. It is not the whole story: how settled a player's role is — projected ` +
    `minutes, games missed last season — is uncertainty too, and this number leaves it out.`
  );
}

/** Spoken by a screen reader, where the colour says nothing at all. */
export function confidenceAria(projectedPoints: number, modelSd: number): string {
  const share = estimatedShare(projectedPoints, modelSd);
  const pct = share === null ? "not applicable" : `${(share * 100).toFixed(1)} percent of projection`;
  return `Confidence ${confidenceOf(projectedPoints, modelSd)}: ${modelSd.toFixed(0)} points estimated, ${pct}`;
}
