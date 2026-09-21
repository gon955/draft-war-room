"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";
import type { Board, League } from "@/lib/models";

export default function LeaguesPage() {
  const { ready, token } = useRequireAuth();
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

  if (!ready || !token) return <p className="muted">Loading…</p>;

  return (
    <>
      {error && <p className="error">{error}</p>}

      <div className="card">
        <h2>Connect a league</h2>
        <div className="body">
          <div className="row">
            <input value={espnId} onChange={(e) => setEspnId(e.target.value)} size={12} placeholder="espn league id" />
            <input value={season} onChange={(e) => setSeason(e.target.value)} size={6} placeholder="season" />
            <input value={name} onChange={(e) => setName(e.target.value)} size={16} placeholder="name" />
            <input
              value={cookie}
              onChange={(e) => setCookie(e.target.value)}
              size={22}
              placeholder="espn_s2 (private leagues only)"
            />
            <button
              className="primary"
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
          <p className="muted">
            This calls the real ESPN API. To try it without one, run{" "}
            <code>python scripts/seed_demo.py --email you@example.com</code> and reload.
          </p>
        </div>
      </div>

      <div className="card">
        <h2>Leagues</h2>
        <div className="body">
          {leagues.length === 0 ? (
            <p className="muted">None yet.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th className="num">Teams</th>
                  <th className="num">Roster</th>
                  <th>Format</th>
                  <th>Slots</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {leagues.map((l) => (
                  <tr key={l.id}>
                    <td>
                      {l.name} <span className="muted">· {l.season}</span>
                    </td>
                    <td className="num">{l.num_teams}</td>
                    <td className="num">{l.roster_size}</td>
                    <td>{l.scoring_format}</td>
                    <td className="muted">{Object.keys(l.roster_slots).join(" ")}</td>
                    <td>
                      <button
                        disabled={busy !== null}
                        onClick={() => void run("sync", () => api("POST", `/leagues/${l.id}/sync`, { token }))}
                      >
                        sync
                      </button>{" "}
                      <button
                        disabled={busy !== null}
                        onClick={() =>
                          void run("compute", () =>
                            api("POST", `/leagues/${l.id}/valuations/compute`, { token }),
                          )
                        }
                      >
                        compute values
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="card">
        <h2>Boards</h2>
        <div className="body">
          <div className="row">
            <select value={boardLeague} onChange={(e) => setBoardLeague(e.target.value)}>
              {leagues.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name} · {l.season}
                </option>
              ))}
            </select>
            <input value={boardName} onChange={(e) => setBoardName(e.target.value)} size={18} />
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
            <p className="muted">No boards yet.</p>
          ) : (
            <table>
              <tbody>
                {boards.map((b) => (
                  <tr key={b.id}>
                    <td>
                      {/* Query string, not a [boardId] segment: a static export
                          cannot prerender a route whose id is unknown at build. */}
                      <a href={`/board?id=${b.id}`}>{b.name}</a>
                    </td>
                    <td className="num">
                      <button
                        className="danger"
                        disabled={busy !== null}
                        onClick={() => void run("del", () => api("DELETE", `/boards/${b.id}`, { token }))}
                      >
                        delete
                      </button>
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
