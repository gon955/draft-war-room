"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, ApiError, qs } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";
import { POSITIONS } from "@/lib/models";
import type {
  Lineup,
  Mock,
  Pick,
  Position,
  RecommendationPage,
  SimulateResult,
  BotValuation,
  Tier,
} from "@/lib/models";
import { Headshot } from "@/components/Headshot";
import { ConfidenceLegend } from "@/components/ConfidenceLegend";
import { InjuryTag } from "@/components/InjuryTag";
import { Loading, Meter, OpHeader, Panel, Readout, pad } from "@/components/Hud";
import { confidenceAria, confidenceOf, confidenceTitle } from "@/lib/confidence";

function MockView() {
  const { ready, token } = useRequireAuth();
  const params = useSearchParams();
  const mockId = params.get("id");
  const boardId = params.get("board");

  // The recommendation, not plain best-available: it carries the live
  // re-valuation and what each player would add to YOUR lineup.
  const [best, setBest] = useState<RecommendationPage | null>(null);
  const [picks, setPicks] = useState<Pick[]>([]);
  const [mock, setMock] = useState<Mock | null>(null);
  // Tiers are board-scoped, so they load once alongside the mock rather than
  // per row: a ranking carries only tier_id, and the label lives here.
  const [tiers, setTiers] = useState<Tier[]>([]);
  const [lineup, setLineup] = useState<Lineup | null>(null);
  // What the last simulation did, so the board explains itself rather than
  // silently filling in nine picks while you were reading the other panel.
  const [lastSim, setLastSim] = useState<SimulateResult | null>(null);
  // Filters refetch on every change, so a slow earlier response can land after
  // a fast later one and overwrite it. Each call takes a ticket and discards
  // its result if a newer call has started since.
  const requestId = useRef(0);

  const [position, setPosition] = useState<Position | "">("");
  // Whose opinion the simulated teams draft on. Not persisted on the mock: it
  // changes who the room takes, not what the board means, so re-running with
  // the other setting is the comparison worth having.
  const [bots, setBots] = useState<BotValuation>("engine");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!token || !mockId) return;
    const id = ++requestId.current;
    try {
      const [b, p, l] = await Promise.all([
        api<RecommendationPage>(
          "GET",
          `/mocks/${mockId}/recommendation${qs({ position, limit: 50 })}`,
          { token },
        ),
        api<Pick[]>("GET", `/mocks/${mockId}/picks`, { token }),
        api<Lineup>("GET", `/mocks/${mockId}/lineup`, { token }),
      ]);
      if (id !== requestId.current) return;
      setError(null);
      setBest(b);
      setPicks(p);
      setLineup(l);
      if (boardId) {
        const [all, t] = await Promise.all([
          api<Mock[]>("GET", `/boards/${boardId}/mocks`, { token }),
          api<Tier[]>("GET", `/boards/${boardId}/tiers`, { token }),
        ]);
        if (id !== requestId.current) return;
        setMock(all.find((m) => m.id === mockId) ?? null);
        setTiers(t);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [token, mockId, boardId, position]);

  useEffect(() => {
    // Awaited inside an IIFE rather than a bare `void load()`. The React
    // Compiler lint rule cannot see through an async useCallback and treats the
    // bare call as a synchronous setState in an effect; awaiting here makes the
    // ordering explicit and satisfies it.
    void (async () => {
      await load();
    })();
  }, [load]);

  async function mutate(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  /** Draft into the first slot nobody has taken yet. */
  function draft(playerId: string) {
    const open = picks.find((p) => p.player_id === null);
    if (!open) {
      setError("Every pick is filled.");
      return;
    }
    void mutate(() =>
      api("POST", `/mocks/${mockId}/picks`, {
        token,
        body: { pick_number: open.pick_number, player_id: playerId },
      }),
    );
  }

  /** Auto-draft the other teams up to my next pick. */
  function runSim() {
    void mutate(async () => {
      setLastSim(
        await api<SimulateResult>("POST", `/mocks/${mockId}/simulate`, {
          token,
          body: { bot_valuation: bots },
        }),
      );
    });
  }

  const tierOf = new Map(tiers.map((t) => [t.id, t]));

  if (!ready || !token) return <Loading />;
  if (!mockId) return <p className="error">No mock id. Pick one from the board.</p>;

  // Who is on the clock is the first unfilled pick — the same rule `draft`
  // uses to decide where a click lands.
  const onClock = picks.find((p) => p.player_id === null);
  const myNext = picks.find((p) => p.player_id === null && p.is_mine);
  const made = picks.filter((p) => p.player_id !== null).length;
  const complete = picks.length > 0 && !onClock;
  const mineUp = onClock?.is_mine ?? false;
  const startersFilled = lineup ? lineup.starters.filter((s) => s.player).length : 0;

  return (
    <>
      <OpHeader
        kicker={["Ops", "Mock draft", mock ? `slot ${pad(mock.my_draft_slot)}` : "live"]}
        title={mock ? mock.name : "Mock draft"}
        side={
          boardId && (
            <a className="btn" href={`/board?id=${boardId}`}>
              ← Back to board
            </a>
          )
        }
      />

      {error && <p className="error">{error}</p>}

      <div className="readouts">
        <Readout
          label="Picks made"
          value={made}
          unit={`/${picks.length}`}
          meter={picks.length ? made / picks.length : 0}
          tone="amber"
        />
        <Readout
          label="Starters"
          value={startersFilled}
          unit={lineup ? `/${lineup.starters.length}` : undefined}
          meter={lineup && lineup.starters.length ? startersFilled / lineup.starters.length : 0}
          tone="go"
        />
        <Readout
          label="Your next pick"
          value={myNext ? `#${myNext.pick_number}` : "—"}
          sub={myNext ? `round ${myNext.round}` : complete ? "draft complete" : "no picks left"}
        />
        <Readout label="Pool remaining" value={best?.total ?? "—"} sub="undrafted, valued" />
        <Readout
          label="Picks left"
          value={lineup ? pad(lineup.picks_remaining) : "—"}
          sub={lineup ? `bench ${lineup.bench.length}/${lineup.bench_size}` : undefined}
        />
      </div>

      <Panel code="SEC-01" title="The clock" hot={mineUp}>
        <div className="clock">
          <div className={complete ? "radar done" : mineUp ? "radar" : "radar idle"}>
            <div className="radar-num">
              {complete ? "✓" : onClock ? onClock.pick_number : "—"}
              <small>{complete ? "final" : onClock ? `rd ${onClock.round}` : ""}</small>
            </div>
          </div>
          <div style={{ minWidth: 0 }}>
            <div className={mineUp ? "clock-status hot" : "clock-status idle"}>
              {complete ? "Board complete" : mineUp ? "You are on the clock" : "The room is picking"}
            </div>
            <div className="clock-line">
              {complete ? (
                "Every pick is in. Your lineup is below."
              ) : mineUp ? (
                <>
                  Pick <b>#{onClock?.pick_number}</b> is yours — draft from the recommendation below.
                </>
              ) : onClock ? (
                <>
                  Team <b>{onClock.team_slot}</b> is up at pick <b>#{onClock.pick_number}</b>
                  {best ? (
                    <>
                      {" "}
                      · <b>{best.picks_until_next}</b> pick{best.picks_until_next === 1 ? "" : "s"} until
                      yours
                    </>
                  ) : null}
                </>
              ) : (
                "Loading the board…"
              )}
            </div>
            <Meter value={picks.length ? made / picks.length : 0} />
            <div className="row">
              <div className="field">
                <label htmlFor="bots">Bots draft on</label>
                <select
                  id="bots"
                  value={bots}
                  onChange={(e) => setBots(e.target.value as BotValuation)}
                  title={
                    "Whose valuation the simulated teams use. Position scarcity and roster fit " +
                    "apply either way — they are computed FROM these numbers — so this changes " +
                    "who the room thinks is good, not how it drafts.\n\n" +
                    "ESPN is the more realistic rehearsal: your leaguemates are reading ESPN's " +
                    "ranking, not yours, which is what makes a player slide."
                  }
                >
                  <option value="engine">our valuation</option>
                  <option value="espn">ESPN&rsquo;s valuation</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="pos">Filter position</label>
                <select id="pos" value={position} onChange={(e) => setPosition(e.target.value as Position | "")}>
                  <option value="">any</option>
                  {POSITIONS.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </div>
              <button className="primary big" disabled={busy || complete} onClick={runSim} style={{ alignSelf: "flex-end" }}>
                {busy ? "Simulating…" : "Simulate to my pick"}
              </button>
            </div>
          </div>
        </div>

        {lastSim && (
          <div className="sitrep" role="status">
            <div className="sitrep-head">
              Sitrep · {lastSim.picks.length} pick{lastSim.picks.length === 1 ? "" : "s"} on{" "}
              {bots === "espn" ? "ESPN's" : "our"} valuation
            </div>
            {lastSim.picks.length === 0 ? (
              <div>{lastSim.board_complete ? "Board complete." : "It's your pick — nothing to simulate."}</div>
            ) : (
              <ol>
                {lastSim.picks.map((p) => (
                  <li key={p.pick_number}>
                    <span>#{pad(p.pick_number, 3)} T{pad(p.team_slot)}</span>
                    {p.player.name}
                  </li>
                ))}
              </ol>
            )}
            {lastSim.next_pick_number ? (
              <div className="sitrep-foot">▶ You&rsquo;re on the clock at #{lastSim.next_pick_number}.</div>
            ) : null}
          </div>
        )}
      </Panel>

      {lineup && (
        <Panel
          code="SEC-02"
          title="Your lineup"
          meta={
            <>
              <span>
                {lineup.picks_made} of {lineup.roster_size} rostered
              </span>
              <span>
                {startersFilled} of {lineup.starters.length} starters
              </span>
              <span>{lineup.picks_remaining} picks left</span>
            </>
          }
        >
          <div className="seats">
            {lineup.starters.map((seat, i) => (
              <div key={`${seat.slot}-${seat.index}-${i}`} className={seat.player ? "seat" : "seat empty"}>
                <span className="seat-slot">{seat.slot}</span>
                {seat.player ? (
                  <span className="player-cell">
                    <Headshot espnPlayerId={seat.player.espn_player_id} name={seat.player.name} size={32} />
                    <span className="stack">
                      <span className="seat-name">{seat.player.name}</span>
                      <span className="muted">{seat.player.positions.join("/")}</span>
                    </span>
                  </span>
                ) : (
                  <span className="seat-open">Open seat</span>
                )}
              </div>
            ))}
          </div>

          {/* bench_size is roster_size minus the starting slots, which the
              API derives — roster_slots counts starters only. */}
          <div className="subhead">
            Bench · {lineup.bench.length} of {lineup.bench_size}
          </div>
          {lineup.bench.length === 0 ? (
            <p className="muted" style={{ margin: 0 }}>
              Nobody on the bench yet.
            </p>
          ) : (
            <div className="seats">
              {lineup.bench.map((p) => (
                <div key={p.id} className="seat bench">
                  <span className="seat-slot">BE</span>
                  <span className="player-cell">
                    <Headshot espnPlayerId={p.espn_player_id} name={p.name} size={32} />
                    <span className="stack">
                      <span className="seat-name">{p.name}</span>
                      <span className="muted">{p.positions.join("/")}</span>
                    </span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      )}

      <div className="grid2">
        <Panel
          code="SEC-03"
          title="Recommendation"
          hot={mineUp}
          flush
          meta={
            <span>
              {best?.total ?? 0} left ·{" "}
              {best
                ? best.picks_until_next === 0
                  ? "you pick again immediately"
                  : `${best.picks_until_next} until your turn`
                : ""}
            </span>
          }
          foot={<ConfidenceLegend />}
        >
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Player</th>
                  <th>Pos</th>
                  <th>Tier</th>
                  {/* "My rank", not "Rank": user_rank is an override that is
                      null until you move somebody, so a blank cell here means
                      "engine order", not "missing data". */}
                  <th className="num">My rk</th>
                  {/* Value is the league-wide VOR; Adds is that value filtered
                      through your own seats. They agree until your roster
                      starts constraining you. */}
                  <th className="num">Value</th>
                  <th className="num">Adds</th>
                  {/* Survival is P(still there at your next pick); Score is
                      Adds + what you'd expect to get next turn instead. */}
                  <th className="num">Lasts</th>
                  <th className="num">Score</th>
                  {/* How much of this player's projection rests on stats we
                      inferred rather than ESPN projecting them. The part of
                      the error bar that does NOT cancel between players.
                      Graded against projected points rather than against the
                      Value column beside it: value collapses toward zero as a
                      draft runs, so a share of it would drift every candidate
                      to "low" by round 8 and say more about the round than
                      about the player. See lib/confidence.ts. */}
                  <th className="num" title="How much of a player's projection the engine had to estimate rather than read from ESPN. Lower is better.">
                    Conf
                  </th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {best?.items.map((item) => {
                  const tier = item.ranking?.tier_id ? tierOf.get(item.ranking.tier_id) : undefined;
                  const rowClass = item.ranking?.is_avoid
                    ? "is-avoid"
                    : item.ranking?.is_target
                      ? "is-target"
                      : undefined;
                  const lasts = item.survival;
                  return (
                    <tr key={item.player.id} className={rowClass}>
                      <td>
                        <span className="player-cell">
                          <Headshot espnPlayerId={item.player.espn_player_id} name={item.player.name} size={32} />
                          <span className="stack">
                            <span className="pname">{item.player.name}</span>
                            <span className="tags">
                              <InjuryTag status={item.player.injury_status} />
                              {item.ranking?.is_target && <span className="tag t">▲ TGT</span>}
                              {item.ranking?.is_avoid && <span className="tag a">✕ AVD</span>}
                            </span>
                            {item.ranking?.note && <span className="pnote">{item.ranking.note}</span>}
                          </span>
                        </span>
                      </td>
                      <td className="pos">{item.player.positions.join("/")}</td>
                      <td>
                        {tier ? (
                          <span
                            className="tag tier"
                            style={tier.color ? { background: tier.color, borderColor: tier.color } : undefined}
                          >
                            {tier.label}
                          </span>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td className="num muted">{item.ranking?.user_rank ?? "—"}</td>
                      <td className="num">{item.live.value.toFixed(1)}</td>
                      {/* "bench" used to be the whole story here, and it hid the
                          ordering: once your lineup is full every candidate adds
                          0.0, so the column read the same for all of them while
                          the list was in some order the column could not
                          explain. lineup_delta is that order — how far short of
                          the starter they would displace each one falls. */}
                      <td className={item.improves_lineup ? "num pos-delta" : "num muted"}>
                        {item.improves_lineup ? (
                          `+${item.marginal_value.toFixed(1)}`
                        ) : item.fills_open_seat ? (
                          "fills seat"
                        ) : (
                          <span
                            title={
                              `${Math.abs(item.lineup_delta).toFixed(0)} short of the starter ` +
                              `they would have to displace. Bench depth, but this is how close ` +
                              `they come — and it is what orders the board once your lineup is full.`
                            }
                          >
                            {item.lineup_delta.toFixed(0)}
                          </span>
                        )}
                      </td>
                      <td className="num">
                        <span className="meter-cell">
                          <Meter value={lasts} tone={lasts >= 0.7 ? "go" : lasts >= 0.35 ? "caution" : "signal"} />
                          <span className="muted">{`${Math.round(lasts * 100)}%`}</span>
                        </span>
                      </td>
                      <td className="num score">{item.score.toFixed(1)}</td>
                      <td className="num">
                        {item.valuation ? (
                          <span
                            className={`conf conf-${confidenceOf(item.valuation.projected_points, item.valuation.model_sd)}`}
                            title={confidenceTitle(item.valuation.projected_points, item.valuation.model_sd)}
                            aria-label={confidenceAria(item.valuation.projected_points, item.valuation.model_sd)}
                          >
                            ±{item.valuation.model_sd.toFixed(0)}
                          </span>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td>
                        <button className="sm primary" disabled={busy} onClick={() => draft(item.player.id)}>
                          Draft
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          code="SEC-04"
          title="Draft board"
          flush
          meta={
            <span>
              <span style={{ color: "var(--amber)" }}>▎</span>yours{" "}
              <span style={{ color: "var(--go)" }}>▎</span>on the clock
            </span>
          }
        >
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th className="num">Rd</th>
                  <th className="num">Team</th>
                  <th>Player</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {picks.map((p) => (
                  <tr
                    key={p.id}
                    className={p.is_mine ? "mine" : p.id === onClock?.id ? "clock" : undefined}
                  >
                    <td className="num idx">
                      <b>{pad(p.pick_number, 3)}</b>
                    </td>
                    <td className="num muted">{p.round}</td>
                    <td className="num muted">T{pad(p.team_slot)}</td>
                    <td>
                      {p.player ? (
                        <span className="player-cell">
                          <Headshot espnPlayerId={p.player.espn_player_id} name={p.player.name} size={24} />
                          <span className="stack">
                            <span className="pname">{p.player.name}</span>
                            <span className="muted"> {p.player.positions.join("/")}</span>
                          </span>
                        </span>
                      ) : p.id === onClock?.id ? (
                        <span className="seat-open" style={{ color: "var(--go)" }}>
                          ▶ On the clock
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      {p.player_id && (
                        <button
                          className="sm danger"
                          disabled={busy}
                          aria-label={`undo pick ${p.pick_number}`}
                          onClick={() =>
                            // null clears the slot and returns the player to
                            // best-available.
                            void mutate(() =>
                              api("POST", `/mocks/${mockId}/picks`, {
                                token,
                                body: { pick_number: p.pick_number, player_id: null },
                              }),
                            )
                          }
                        >
                          ×
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </>
  );
}

export default function MockPage() {
  return (
    <Suspense fallback={<Loading />}>
      <MockView />
    </Suspense>
  );
}
