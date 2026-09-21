// Re-exports of the generated OpenAPI schemas, so screens import a short name
// instead of components["schemas"]["..."] everywhere.
//
// Never hand-edit src/lib/types.ts — `npm run types` regenerates it from the
// live API, and CI fails if the committed copy has drifted. That is the whole
// point: a Pydantic field rename becomes a TypeScript compile error here rather
// than `undefined` on a page.
import type { components } from "./types";

type S = components["schemas"];

export type User = S["UserOut"];
export type Token = S["TokenOut"];
export type League = S["LeagueOut"];
export type SyncResult = S["SyncResult"];
export type ComputeResult = S["ComputeResult"];
export type Player = S["PlayerOut"];
export type Valuation = S["ValuationOut"];
export type PlayerWithValuation = S["PlayerWithValuationOut"];
export type Board = S["BoardOut"];
export type Tier = S["TierOut"];
export type Ranking = S["RankingOut"];
export type Share = S["ShareOut"];
export type Mock = S["MockWithProgressOut"];
// The write returns PickOut (id in, id out); the board read returns the
// player nested, which is the only place a drafted player's name exists.
export type Pick = S["PickWithPlayerOut"];
export type PickWrite = S["PickOut"];
export type BestAvailable = S["BestAvailableOut"];
export type Lineup = S["LineupOut"];
export type SimulateResult = S["SimulateOut"];
export type Recommendation = S["RecommendationOut"];
export type RecommendationPage = S["RecommendationPage"];
export type Seat = S["SeatOut"];
export type Position = S["Position"];
export type PlayerSort = S["PlayerSort"];

export type PlayerPage = S["Page_PlayerWithValuationOut_"];
export type ValuationPage = S["Page_ValuationOut_"];
export type BestPage = S["Page_BestAvailableOut_"];

export const POSITIONS: Position[] = ["PG", "SG", "SF", "PF", "C"];
