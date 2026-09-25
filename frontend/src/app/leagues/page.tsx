"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";
import { EmptyState, Loading, OpHeader, Panel, Readout, pad } from "@/components/Hud";
import type { Board, League } from "@/lib/models";

export default function LeaguesPage() {
  const { ready, token, user } = useRequireAuth();
  const [leagues, setLeagues] = useState<League[]>([]);
  const [boards, setBoards] = useState<Board[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // Filters refetch on every change, so a slow earlier response can land after
  // a fast later one and overwrite it. Each call takes a ticket and discards
  // its result if a newer call has started since.
  const requestId = useRef(0);

  const [espnId, setEspnId] = useState("419087");
  const [season, setSeason] = useState("2027");
  const [name, setName] = useState("My League");
  const [cookie, setCookie] = useState("");
  const [boardName, setBoardName] = useState("Draft night");
  const [boardLeague, setBoardLeague] = useState("");

  const load = useCallback(async () => {
    if (!token) return;
    const id = ++requestId.current;
    try {
      const [ls, bs] = await Promise.all([
        api<League[]>("GET", "/leagues", { token }),
        api<Board[]>("GET", "/boards", { token }),
      ]);
      if (id !== requestId.current) return;
      setError(null);
      setLeagues(ls);
      setBoards(bs);
      setBoardLeague((cur) => cur || ls[0]?.id || "");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [token]);

  useEffect(() => {
    // Awaited inside an IIFE rather than a bare `void load()`. The React
    // Compiler lint rule cannot see through an async useCallback and treats the
    // bare call as a synchronous setState in an effect; awaiting here makes the
    // ordering explicit and satisfies it.
    void (async () => {
      await load();
    })();
  }, [load]);

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (!ready || !token) return <Loading />;

  const leagueById = new Map(leagues.map((l) => [l.id, l]));
  const shared = boards.filter((b) => user && b.user_id !== user.id).length;

  return (
    <>
      <OpHeader
        kicker={["Ops", "Command"]}
        title="Command center"
        sub="Connect an ESPN league, sync its player pool, compute values, then open a board to rank, tier and run mock drafts."
      />

      {error && <p className="error">{error}</p>}

      <div className="readouts">
        <Readout label="Leagues" value={pad(leagues.length)} tone="amber" sub="connected to ESPN" />
        <Readout
          label="Boards"
          value={pad(boards.length)}
          sub={shared > 0 ? `${shared} shared with you` : "ranks · tiers · mocks"}
        />
        <Readout
          label="Teams tracked"
          value={leagues.reduce((n, l) => n + l.num_teams, 0)}
          sub="across all leagues"
        />
        <Readout
          label="Scored stats"
          value={leagues[0] ? Object.keys(leagues[0].point_weights).length : "—"}
          sub={leagues[0] ? `in ${leagues[0].name}` : "no league yet"}
        />
      </div>

      <Panel
        code="SEC-01"
        title="Connect a league"
        meta={<span>source: ESPN fantasy API</span>}
        foot={
          <p className="muted" style={{ margin: 0 }}>
            This calls the real ESPN API. To try it without one, run{" "}
            <code>python scripts/seed_demo.py --email you@example.com</code> and reload.
          </p>
        }
      >
        <div className="fields">
          <div className="field">
            <label htmlFor="espn-id">ESPN league id</label>
            <input id="espn-id" value={espnId} onChange={(e) => setEspnId(e.target.value)} size={12} />
          </div>
          <div className="field">
            <label htmlFor="season">Season</label>
            <input id="season" value={season} onChange={(e) => setSeason(e.target.value)} size={6} />
          </div>
          <div className="field">
            <label htmlFor="lname">Name</label>
            <input id="lname" value={name} onChange={(e) => setName(e.target.value)} size={18} />
          </div>
          <div className="field" style={{ flex: 1, minWidth: 220 }}>
            <label htmlFor="s2">espn_s2 · private leagues only</label>
            {/* type="password" because espn_s2 is a live session cookie, not
                a setting: anyone who reads it over your shoulder or out of a
                screen share is signed in as you at ESPN until it expires. The
                API encrypts it at rest and never returns it; this is the same
                care at the only point where it is visible.
                autoComplete="off" keeps it out of the browser's saved
                form-fill, which is not an encrypted store. */}
            <input
              id="s2"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={cookie}
              onChange={(e) => setCookie(e.target.value)}
              placeholder="leave blank for a public league"
            />
          </div>
          <button
            className="primary big"
            disabled={busy !== null}
            onClick={() =>
              void run("create", () =>
                api<League>("POST", "/leagues", {
                  token,
                  body: {
                    espn_league_id: Number(espnId),
                    season: Number(season),
                    name,
                    // Omitted rather than sent empty: the API stores NULL for
                    // a public league, not an encrypted empty string.
                    ...(cookie.trim() ? { espn_s2: cookie.trim() } : {}),
                  },
                }),
              )
            }
          >
            {busy === "create" ? "Contacting ESPN…" : "Connect"}
          </button>
        </div>
      </Panel>

      <Panel code="SEC-02" title="Leagues" meta={<span>{leagues.length} on file</span>}>
        {leagues.length === 0 ? (
          <EmptyState title="No leagues on file">Connect one above to pull its player pool.</EmptyState>
        ) : (
          <div className="dossiers">
            {leagues.map((l) => (
              <article key={l.id} className="dossier">
                <span className="stamp">{l.scoring_format.toUpperCase()}</span>
                <div className="dossier-id">
                  ESPN {l.espn_league_id} · season {l.season}
                </div>
                <div className="dossier-title">{l.name}</div>
                <div className="dossier-stats">
                  <span>
                    <b>{l.num_teams}</b>teams
                  </span>
                  <span>
                    <b>{l.roster_size}</b>roster
                  </span>
                  <span>
                    <b>{Object.keys(l.point_weights).length}</b>scored stats
                  </span>
                </div>
                <div className="chips">
                  {Object.entries(l.roster_slots).map(([slot, n]) => (
                    <span key={slot} className="chip">
                      {slot}
                      <b>×{n}</b>
                    </span>
                  ))}
                </div>
                <div className="dossier-actions">
                  <button
                    className="sm"
                    disabled={busy !== null}
                    onClick={() => void run("sync", () => api("POST", `/leagues/${l.id}/sync`, { token }))}
                  >
                    {busy === "sync" ? "Syncing…" : "Sync pool"}
                  </button>
                  <button
                    className="sm"
                    disabled={busy !== null}
                    onClick={() =>
                      void run("compute", () =>
                        api("POST", `/leagues/${l.id}/valuations/compute`, { token }),
                      )
                    }
                  >
                    {busy === "compute" ? "Computing…" : "Compute values"}
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </Panel>

      <Panel code="SEC-03" title="Boards" meta={<span>{boards.length} active</span>} hot={boards.length > 0}>
        <div className="fields" style={{ marginBottom: 16 }}>
          <div className="field">
            <label htmlFor="board-league">League</label>
            <select id="board-league" value={boardLeague} onChange={(e) => setBoardLeague(e.target.value)}>
              {leagues.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name} · {l.season}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="board-name">Board name</label>
            <input id="board-name" value={boardName} onChange={(e) => setBoardName(e.target.value)} size={20} />
          </div>
          <button
            className="primary"
            disabled={busy !== null || !boardLeague}
            onClick={() =>
              void run("board", () =>
                api<Board>("POST", "/boards", { token, body: { league_id: boardLeague, name: boardName } }),
              )
            }
          >
            New board
          </button>
        </div>
        {boards.length === 0 ? (
          <EmptyState title="No boards yet">
            A board is where ranks, tiers and mock drafts live.
          </EmptyState>
        ) : (
          <div className="dossiers">
            {boards.map((b, i) => {
              const mine = !user || b.user_id === user.id;
              const league = leagueById.get(b.league_id);
              return (
                <article key={b.id} className="dossier">
                  <span className={mine ? "stamp hot" : "stamp cold"}>{mine ? "OWNED" : "SHARED"}</span>
                  <div className="dossier-id">
                    Board {pad(i + 1)} · opened {b.created_at.slice(0, 10)}
                  </div>
                  {/* Query string, not a [boardId] segment: a static export
                      cannot prerender a route whose id is unknown at build. */}
                  <a className="dossier-title" href={`/board?id=${b.id}`}>
                    {b.name}
                  </a>
                  <div className="muted">{league ? `${league.name} · ${league.season}` : "shared league"}</div>
                  <div className="dossier-actions">
                    <a className="btn sm primary" href={`/board?id=${b.id}`}>
                      Open board
                    </a>
                    {mine && (
                      <button
                        className="sm danger"
                        disabled={busy !== null}
                        onClick={() => void run("del", () => api("DELETE", `/boards/${b.id}`, { token }))}
                      >
                        Delete
                      </button>
                    )}
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
