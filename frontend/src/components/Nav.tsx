"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { Crosshair } from "@/components/Crosshair";

/** HH:MM:SS UTC, ticking. Null until mounted so the prerendered HTML and the
 *  first client render agree — the build has no "now" worth showing. */
function useUtcClock(): string | null {
  const [now, setNow] = useState<string | null>(null);
  useEffect(() => {
    const tick = () => setNow(new Date().toISOString().slice(11, 19));
    const t = setInterval(tick, 1000);
    const first = setTimeout(tick, 0);
    return () => {
      clearInterval(t);
      clearTimeout(first);
    };
  }, []);
  return now;
}

export default function Nav() {
  const path = usePathname();
  const router = useRouter();
  const { user, token, logout } = useAuth();
  const clock = useUtcClock();

  if (path === "/login") return null;

  const callsign = user?.email?.split("@")[0] ?? (token ? "…" : "—");

  return (
    <>
      <div className="strip" aria-hidden>
        <b>Restricted</b>
        <span>Draft operations</span>
        <span className="sep hide-sm">■</span>
        <span className="hide-sm">Points league · value over replacement</span>
        <span className="right hide-sm">Eyes only // front office</span>
      </div>
      <nav className="top">
        <Link href="/leagues" className="brand" aria-label="Draft War Room home">
          <Crosshair size={30} className="brand-mark" />
          <span className="brand-word">
            War Room
            <small>Draft command</small>
          </span>
        </Link>
        <Link href="/leagues" className={path === "/leagues" ? "tab on" : "tab"}>
          <span className="idx">01</span>Command
        </Link>
        <Link href="/board" className={path === "/board" ? "tab on" : "tab"}>
          <span className="idx">02</span>Board
        </Link>
        {/* A mock has no index page — it is always opened from a board — so
            this is a status light, not a link. */}
        {path === "/mock" && (
          <span className="tab on inert" aria-current="page">
            <span className="idx">03</span>Mock draft
          </span>
        )}
        <span className="spacer" />
        <div className="ops">
          <div className="ops-cell hide-sm">
            <span>Zulu</span>
            <span className="val">{clock ?? "--:--:--"}</span>
          </div>
          <div className="ops-cell hide-sm">
            <span>Link</span>
            <span className="val live">Secure</span>
          </div>
          <div className="ops-cell">
            <span>Operator</span>
            <span className="val" title={user?.email ?? undefined}>{callsign}</span>
          </div>
          {token && (
            <button
              className="sm ghost"
              onClick={() => {
                logout();
                router.push("/login");
              }}
            >
              Log out
            </button>
          )}
        </div>
      </nav>
    </>
  );
}
