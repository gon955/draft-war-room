"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, ApiError, qs } from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";
import { POSITIONS } from "@/lib/models";
import { Headshot } from "@/components/Headshot";
import { ConfidenceLegend } from "@/components/ConfidenceLegend";
import { InjuryTag } from "@/components/InjuryTag";
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
  const tierLabel = useMemo(() => new Map(tiers.map((t) => [t.id, t.label])), [tiers]);

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

  if (!ready || !token) return <p className="muted">Loading…</p>;
  // The nav links here without an id, so this is a landing page, not an error.
  if (!boardId) return <BoardPicker />;

  return (
    <>
      {error && <p className="error">{error}</p>}

      <div className="card">
        <h2>{board ? board.name : "Board"}</h2>
        <div className="body">
          <div className="row">
            <label>Position</label>
            <select value={position} onChange={(e) => setPosition(e.target.value as Position | "")}>
              <option value="">any</option>
              {POSITIONS.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
            <label>Sort</label>
            <select value={sort} onChange={(e) => setSort(e.target.value as PlayerSort)}>
              <option value="value">value</option>
              <option value="projected_points">projected points</option>
              <option value="name">name</option>
            </select>
            <label>Show</label>
            <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
              {[25, 50, 100, 200].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
            <span className="spacer" style={{ marginLeft: "auto" }} />
            <label>Tiers</label>
            <input
              className="num"
              style={{ width: 52 }}
              inputMode="numeric"
              value={nTiers}
              onChange={(e) => setNTiers(e.target.value)}
              aria-label="number of tiers (2-20)"
            />
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
              auto-tier
            </button>
          </div>
          <p className="muted">
            {page ? `${page.total} players in the pool` : "…"}
            {tiers.length > 0 && ` · ${tiers.length} tiers`}
            {rankings.length > 0 && ` · ${rankings.length} ranked`}
            {" · "}
            <a href="#mocks">
              {mocks.length === 0 ? "start a mock draft" : `${mocks.length} mock draft(s)`}
            </a>
          </p>
        </div>
      </div>

      <div className="card">
        <h2>Players</h2>
        <div className="body scroll">
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
                <th>T</th>
                <th>A</th>
              </tr>
            </thead>
            <tbody>
              {page?.items.map((item, i) => {
                const v = item.valuation;
                const r = rankingByPlayer.get(item.player.id);
                return (
                  <tr key={item.player.id}>
                    <td className="num">{i + 1}</td>
                    <td>
                      <span className="player-cell">
                        <Headshot
                          espnPlayerId={item.player.espn_player_id}
                          name={item.player.name}
                        />
                        <span className="stack">{item.player.name}</span>
                        <InjuryTag status={item.player.injury_status} />
                      </span>
                    </td>
                    <td className="muted">{item.player.positions.join("/")}</td>
                    <td className="num">{v ? v.projected_points.toFixed(1) : "—"}</td>
                    <td className="num">{v ? v.replacement_points.toFixed(1) : "—"}</td>
                    <td className="num">{v ? v.value.toFixed(1) : "—"}</td>
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
                    <td className="muted">{v?.assigned_slot ?? "—"}</td>
                    <td className="muted">{r?.tier_id ? (tierLabel.get(r.tier_id) ?? "?") : "—"}</td>
                    <td className="num">
                      <input
                        className="num"
                        style={{ width: 48 }}
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
                        style={{ width: 160 }}
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
                        checked={r?.is_target ?? false}
                        onChange={(e) => void setField(item.player.id, { is_target: e.target.checked })}
                      />
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        checked={r?.is_avoid ?? false}
                        onChange={(e) => void setField(item.player.id, { is_avoid: e.target.checked })}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <ConfidenceLegend />
        </div>
      </div>

      <div className="card">
        <h2 id="mocks">Mock drafts</h2>
        <div className="body">
          <div className="row">
            <label>My slot</label>
            <input
              className="num"
              style={{ width: 52 }}
              inputMode="numeric"
              value={mockSlot}
              onChange={(e) => setMockSlot(e.target.value)}
              aria-label="your draft slot"
            />
            {teams !== null && <span className="muted">of {teams}</span>}
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
            <p className="muted">No mocks yet.</p>
          ) : (
            <table>
              <tbody>
                {mocks.map((m) => (
                  <tr key={m.id}>
                    <td>
                      <a href={`/mock?board=${boardId}&id=${m.id}`}>{m.name}</a>
                    </td>
                    <td className="muted">slot {m.my_draft_slot}</td>
                    <td className="num muted">
                      {m.picks_made} / {m.picks_total}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
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

  if (boards === null) return <p className="muted">Loading…</p>;

  return (
    <div className="card">
      <h2>Pick a board</h2>
      <div className="body">
        {boards.length === 0 ? (
          <p className="muted">
            No boards yet — <a href="/leagues">create one on the Leagues page</a>. A board is
            where ranks, tiers and mock drafts live.
          </p>
        ) : (
          <table>
            <tbody>
              {boards.map((b) => (
                <tr key={b.id}>
                  <td>
                    <a href={`/board?id=${b.id}`}>{b.name}</a>
                  </td>
                  <td className="muted">open board</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export default function BoardPage() {
  // useSearchParams suspends during prerender; without this boundary the
  // static build FAILS rather than warns.
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <BoardView />
    </Suspense>
  );
}
