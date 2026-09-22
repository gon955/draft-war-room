"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

export default function Nav() {
  const path = usePathname();
  const router = useRouter();
  const { user, token, logout } = useAuth();

  if (path === "/login") return null;

  return (
    <nav className="top">
      <span className="brand">Draft War Room</span>
      <Link href="/leagues" className={path === "/leagues" ? "on" : ""}>
        Leagues
      </Link>
      <Link href="/board" className={path === "/board" ? "on" : ""}>
        Board
      </Link>
      <span className="spacer" />
      <span className="muted">{user?.email ?? (token ? "…" : "")}</span>
      {token && (
        <button
          onClick={() => {
            logout();
            router.push("/login");
          }}
        >
          log out
        </button>
      )}
    </nav>
  );
}
