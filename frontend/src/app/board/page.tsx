"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, ApiError, qs } from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";
import { POSITIONS } from "@/lib/models";
import { Headshot } from "@/components/Headshot";
import { ConfidenceLegend } from "@/components/ConfidenceLegend";
import { InjuryTag } from "@/components/InjuryTag";
import { EmptyState, Loading, Meter, OpHeader, Panel, Readout, pad } from "@/components/Hud";
import { confidenceAria, confidenceOf, confidenceTitle } from "@/lib/confidence";
import type {
  Board,
  League,
  Mock,
  PlayerPage,
  PlayerSort,
  Position,
  Ranking,
  Tier,
} from "@/lib/models";

/** Whether a typed string is a whole number inside [lo, hi]. Empty is not. */
function inRange(raw: string, lo: number, hi?: number): boolean {
  if (!/^\d+$/.test(raw.trim())) return false;
  const n = Number(raw);
  return n >= lo && (hi === undefined || n <= hi);
}

function BoardView() {
  const { ready, token } = useRequireAuth();
  const boardId = useSearchParams().get("id");

  const [board, setBoard] = useState<Board | null>(null);
  const [page, setPage] = useState<PlayerPage | null>(null);
  const [rankings, setRankings] = useState<Ranking[]>([]);
  const [tiers, setTiers] = useState<Tier[]>([]);
  const [mocks, setMocks] = useState<Mock[]>([]);
  // The league's team count, for the draft-slot hint. Null when unknown.
  const [teams, setTeams] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Filters refetch on every change, so a slow earlier response can land after
  // a fast later one and overwrite it. Each call takes a ticket and discards
  // its result if a newer call has started since.
  const requestId = useRef(0);

  const [position, setPosition] = useState<Position | "">("");
  const [sort, setSort] = useState<PlayerSort>("value");
  const [limit, setLimit] = useState(50);
  // Held as the RAW STRING, not a number. Coercing on every keystroke with
  // `Number(v) || 6` made the field impossible to edit: clearing it yields
  // Number("") === 0, which is falsy, so the fallback fired and the box snapped
  // straight back to 6. You could never empty it to type something else.
  const [nTiers, setNTiers] = useState("6");
  const [mockSlot, setMockSlot] = useState("1");

  const load = useCallback(async () => {
    if (!token || !boardId) return;
    const id = ++requestId.current;
    try {
      const b = await api<Board>("GET", `/boards/${boardId}`, { token });
      if (id !== requestId.current) return;
      setBoard(b);
      // The pool, the overlay and the mocks in parallel — they are independent
      // reads and the board is unusable until all three land.
      const [players, ranks, tiersRes, mocksRes] = await Promise.all([
        api<PlayerPage>("GET", `/leagues/${b.league_id}/players${qs({ position, sort, limit })}`, { token }),
        api<Ranking[]>("GET", `/boards/${boardId}/rankings`, { token }),
        api<Tier[]>("GET", `/boards/${boardId}/tiers`, { token }),
        api<Mock[]>("GET", `/boards/${boardId}/mocks`, { token }),
      ]);
      if (id !== requestId.current) return;
      // GET /leagues/{id} is OWNER ONLY, so a board shared with you 403s here.
      // That must not break the board — the slot hint just goes away.
      api<League>("GET", `/leagues/${b.league_id}`, { token })
        .then((l) => {
          if (id === requestId.current) setTeams(l.num_teams);
        })
        .catch(() => {
          if (id === requestId.current) setTeams(null);
        });

      setError(null);
      setPage(players);
      setRankings(ranks);
      setTiers(tiersRes);
      setMocks(mocksRes);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [token, boardId, position, sort, limit]);

  useEffect(() => {
    // Awaited inside an IIFE rather than a bare `void load()`. The React
    // Compiler lint rule cannot see through an async useCallback and treats the
    // bare call as a synchronous setState in an effect; awaiting here makes the
    // ordering explicit and satisfies it.
    void (async () => {
      await load();
    })();
  }, [load]);

  // rankings only holds players the user has touched, so the board is the POOL
  // outer-joined to it — exactly how the API builds best-available.
  const rankingByPlayer = useMemo(
    () => new Map(rankings.map((r) => [r.player_id, r])),
    [rankings],
  );
  const tierOf = useMemo(() => new Map(tiers.map((t) => [t.id, t])), [tiers]);

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

  /** Create the ranking row on first touch, then patch it. */
  async function setField(playerId: string, patch: Record<string, unknown>) {
    const existing = rankingByPlayer.get(playerId);
    if (!existing) {
      await mutate(() =>
        api("POST", `/boards/${boardId}/rankings`, { token, body: { player_id: playerId, ...patch } }),
      );
      return;
    }
    // exclude_unset on the API side means one key changes one column.
    await mutate(() => api("PATCH", `/rankings/${existing.id}`, { token, body: patch }));
  }

  if (!ready || !token) return <Loading />;
  // The nav links here without an id, so this is a landing page, not an error.
  if (!boardId) return <BoardPicker />;

  const targets = rankings.filter((r) => r.is_target).length;
  const avoids = rankings.filter((r) => r.is_avoid).length;
  // The bar in the Value column is scaled to the best value on this page, so
  // it reads as "how close to the top of what you are looking at".
  const maxValue = Math.max(1, ...(page?.items.map((i) => i.valuation?.value ?? 0) ?? [0]));

  return (
    <>
      <OpHeader
        kicker={["Ops", "Board", teams !== null ? `${teams}-team league` : "shared"]}
        title={board ? board.name : "Board"}
        sub="Every player in the pool, valued over replacement under your league's scoring. Rank, tier, tag and annotate — it all follows you into the mock draft."
        side={
          <a className="btn" href="#mocks">
            {mocks.length === 0 ? "Start a mock" : `Mock drafts · ${mocks.length}`}
          </a>
        }
      />

      {error && <p className="error">{error}</p>}

      <div className="readouts">
        <Readout label="Player pool" value={page ? page.total : "—"} tone="amber" sub="synced from ESPN" />
        <Readout
          label="Ranked"
          value={rankings.length}
          sub="players you have touched"
          meter={page && page.total > 0 ? rankings.length / page.total : 0}
        />
        <Readout label="Tiers" value={pad(tiers.length)} sub={tiers.length ? "on this board" : "none yet — auto-tier below"} />
        <Readout label="Targets" value={pad(targets)} tone="go" sub="flagged ▲" />
        <Readout label="Avoids" value={pad(avoids)} tone={avoids ? "signal" : undefined} sub="flagged ✕" />
      </div>

      <Panel code="SEC-01" title="Filters & tiering">
        <div className="fields">
          <div className="field">
            <label htmlFor="f-pos">Position</label>
            <select id="f-pos" value={position} onChange={(e) => setPosition(e.target.value as Position | "")}>
              <option value="">any</option>
              {POSITIONS.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-sort">Sort</label>
            <select id="f-sort" value={sort} onChange={(e) => setSort(e.target.value as PlayerSort)}>
              <option value="value">value</option>
              <option value="projected_points">projected points</option>
              <option value="name">name</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-limit">Show</label>
            <select id="f-limit" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
              {[25, 50, 100, 200].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>
          <span style={{ marginLeft: "auto" }} />
          <div className="field">
            <label htmlFor="f-tiers">Tiers (2–20)</label>
            <input
              id="f-tiers"
              className="num"
              style={{ width: 70 }}
              inputMode="numeric"
              value={nTiers}
              onChange={(e) => setNTiers(e.target.value)}
              aria-label="number of tiers (2-20)"
            />
          </div>
          <button
            // Disabled rather than silently corrected, so an out-of-range
            // value is visible instead of being swapped for one you did not
            // choose. 2-20 mirrors AutoTierIn's own bounds.
            disabled={busy || !inRange(nTiers, 2, 20)}
            title={inRange(nTiers, 2, 20) ? "" : "Enter a number of tiers between 2 and 20"}
            onClick={() =>
              void mutate(() =>
                api("POST", `/boards/${boardId}/tiers/auto`, {
                  token,
                  body: { n_tiers: Number(nTiers) },
                }),
              )
            }
          >
            Auto-tier
          </button>
        </div>
      </Panel>

      <Panel
        code="SEC-02"
        title="Player pool"
        hot
        flush
        meta={
          <>
            <span>
              showing {page?.items.length ?? 0} of {page?.total ?? 0}
            </span>
            <span>
              <span style={{ color: "var(--amber)" }}>▎</span>target{" "}
              <span style={{ color: "var(--signal)" }}>▎</span>avoid
            </span>
          </>
        }
        foot={<ConfidenceLegend />}
      >
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Player</th>
                <th>Pos</th>
                <th className="num">Proj</th>
                <th className="num">Repl</th>
                <th className="num">Value</th>
                <th className="num" title="How much of a player's projection the engine had to estimate rather than read from ESPN. Lower is better.">
                  Conf
                </th>
                <th>Slot</th>
                <th>Tier</th>
                <th className="num">Rank</th>
                <th>Note</th>
                <th title="Target">▲</th>
                <th title="Avoid">✕</th>
              </tr>
            </thead>
            <tbody>
              {page?.items.map((item, i) => {
                const v = item.valuation;
                const r = rankingByPlayer.get(item.player.id);
                const tier = r?.tier_id ? tierOf.get(r.tier_id) : undefined;
                const rowClass = r?.is_avoid ? "is-avoid" : r?.is_target ? "is-target" : undefined;
                return (
                  <tr key={item.player.id} className={rowClass}>
                    <td className="idx">
                      <b>{pad(i + 1)}</b>
                    </td>
                    <td>
                      <span className="player-cell">
                        <Headshot
                          espnPlayerId={item.player.espn_player_id}
                          name={item.player.name}
                          size={32}
                        />
                        <span className="stack">
                          <span className="pname">{item.player.name}</span>
                          <InjuryTag status={item.player.injury_status} />
                        </span>
                      </span>
                    </td>
                    <td className="pos">{item.player.positions.join("/")}</td>
                    <td className="num">{v ? v.projected_points.toFixed(1) : "—"}</td>
                    <td className="num muted">{v ? v.replacement_points.toFixed(1) : "—"}</td>
                    <td className="num bar-cell">
                      {v && (
                        <i
                          className={v.value < 0 ? "bar neg" : "bar"}
                          style={{ width: `${Math.min(88, (Math.abs(v.value) / maxValue) * 88)}%` }}
                          aria-hidden
                        />
                      )}
                      <span className="score">{v ? v.value.toFixed(1) : "—"}</span>
                    </td>
                    {/* The number carries the meaning and the colour only
                        reinforces it: a band of "±184" is legible with no
                        colour at all, which is what makes this readable to a
                        screen reader and to anyone who cannot separate the
                        green from the amber. */}
                    <td className="num">
                      {v ? (
                        <span
                          className={`conf conf-${confidenceOf(v.projected_points, v.model_sd)}`}
                          title={confidenceTitle(v.projected_points, v.model_sd)}
                          aria-label={confidenceAria(v.projected_points, v.model_sd)}
                        >
                          ±{v.model_sd.toFixed(0)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="pos">{v?.assigned_slot ?? "—"}</td>
                    <td>
                      {tier ? (
                        <span
                          className="tag tier"
                          style={tier.color ? { background: tier.color, borderColor: tier.color } : undefined}
                        >
                          {tier.label}
                        </span>
                      ) : r?.tier_id ? (
                        "?"
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="num">
                      <input
                        className="num cell"
                        style={{ width: 56 }}
                        placeholder="—"
                        aria-label={`your rank for ${item.player.name}`}
                        defaultValue={r?.user_rank ?? ""}
                        onBlur={(e) => {
                          const raw = e.target.value.trim();
                          const next = raw === "" ? null : Number(raw);
                          if ((r?.user_rank ?? null) === next) return;
                          void setField(item.player.id, { user_rank: next });
                        }}
                      />
                    </td>
                    <td>
                      {/* onBlur, not onChange: patching per keystroke would
                          refetch the whole pool on every letter typed. */}
                      <input
                        className="cell"
                        style={{ width: 170 }}
                        placeholder="add note"
                        aria-label={`note on ${item.player.name}`}
                        defaultValue={r?.note ?? ""}
                        onBlur={(e) => {
                          const next = e.target.value || null;
                          if ((r?.note ?? null) === next) return;
                          void setField(item.player.id, { note: next });
                        }}
                      />
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        className="toggle target"
                        aria-label={`target ${item.player.name}`}
                        checked={r?.is_target ?? false}
                        onChange={(e) => void setField(item.player.id, { is_target: e.target.checked })}
                      />
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        className="toggle avoid"
                        aria-label={`avoid ${item.player.name}`}
                        checked={r?.is_avoid ?? false}
                        onChange={(e) => void setField(item.player.id, { is_avoid: e.target.checked })}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel code="SEC-03" title="Mock drafts" id="mocks" meta={<span>{mocks.length} on this board</span>}>
        <div className="fields" style={{ marginBottom: 16 }}>
          <div className="field">
            <label htmlFor="m-slot">My draft slot{teams !== null ? ` (1–${teams})` : ""}</label>
            <input
              id="m-slot"
              className="num"
              style={{ width: 80 }}
              inputMode="numeric"
              value={mockSlot}
              onChange={(e) => setMockSlot(e.target.value)}
              aria-label="your draft slot"
            />
          </div>
          <button
            className="primary"
            // The upper bound is the league's team count; the API rejects
            // anything past it with a message naming the range, so this only
            // guards the lower end and the empty box.
            disabled={busy || !inRange(mockSlot, 1, teams ?? undefined)}
            onClick={() =>
              // Event handler, never an effect: Strict Mode double-invokes
              // effects in dev and this would create two mocks.
              void mutate(() =>
                api("POST", `/boards/${boardId}/mocks`, {
                  token,
                  body: { name: `Mock ${mocks.length + 1}`, my_draft_slot: Number(mockSlot) },
                }),
              )
            }
          >
            New mock
          </button>
        </div>
        {mocks.length === 0 ? (
          <EmptyState title="No mocks yet">
            Pick your slot and start one — the bots fill in every other team.
          </EmptyState>
        ) : (
          <div className="dossiers">
            {mocks.map((m) => {
              const done = m.picks_total > 0 && m.picks_made >= m.picks_total;
              const pct = m.picks_total > 0 ? m.picks_made / m.picks_total : 0;
              return (
                <article key={m.id} className="dossier">
                  <span className={done ? "stamp cold" : m.picks_made > 0 ? "stamp hot" : "stamp"}>
                    {done ? "COMPLETE" : m.picks_made > 0 ? "LIVE" : "READY"}
                  </span>
                  <div className="dossier-id">Opened {m.created_at.slice(0, 10)}</div>
                  <a className="dossier-title" href={`/mock?board=${boardId}&id=${m.id}`}>
                    {m.name}
                  </a>
                  <div className="dossier-stats">
                    <span>
                      <b>{pad(m.my_draft_slot)}</b>your slot
                    </span>
                    <span>
                      <b>
                        {m.picks_made}/{m.picks_total}
                      </b>
                      picks made
                    </span>
                  </div>
                  <Meter value={pct} tone={done ? "go" : undefined} />
                  <div className="dossier-actions">
                    <a className="btn sm primary" href={`/mock?board=${boardId}&id=${m.id}`}>
                      {done ? "Review" : m.picks_made > 0 ? "Resume" : "Enter draft room"}
                    </a>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </Panel>
    </>
  );
}

/** Shown when /board is opened with no ?id — from the nav, say. */
function BoardPicker() {
  const { token } = useAuth();
  const [boards, setBoards] = useState<Board[] | null>(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    api<Board[]>("GET", "/boards", { token })
      .then((b) => {
        if (!cancelled) setBoards(b);
      })
      .catch(() => {
        if (!cancelled) setBoards([]);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (boards === null) return <Loading />;

  return (
    <>
      <OpHeader kicker={["Ops", "Board"]} title="Select a board" sub="A board is where ranks, tiers and mock drafts live." />
      <Panel code="SEC-01" title="Boards on file" meta={<span>{boards.length} available</span>}>
        {boards.length === 0 ? (
          <EmptyState title="No boards yet">
            <a href="/leagues">Create one from Command</a>.
          </EmptyState>
        ) : (
          <div className="dossiers">
            {boards.map((b, i) => (
              <article key={b.id} className="dossier">
                <div className="dossier-id">
                  Board {pad(i + 1)} · opened {b.created_at.slice(0, 10)}
                </div>
                <a className="dossier-title" href={`/board?id=${b.id}`}>
                  {b.name}
                </a>
                <div className="dossier-actions">
                  <a className="btn sm primary" href={`/board?id=${b.id}`}>
                    Open board
                  </a>
                </div>
              </article>
            ))}
          </div>
        )}
      </Panel>
    </>
  );
}

export default function BoardPage() {
  // useSearchParams suspends during prerender; without this boundary the
  // static build FAILS rather than warns.
  return (
    <Suspense fallback={<Loading />}>
      <BoardView />
    </Suspense>
  );
}
