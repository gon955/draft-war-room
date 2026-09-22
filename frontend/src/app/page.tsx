"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

export default function Home() {
  const { ready, token } = useAuth();
  const router = useRouter();

  // Redirect in an effect, not during render: under `output: "export"` this
  // page is prerendered to HTML at build time, where there is no token to read.
  useEffect(() => {
    if (!ready) return;
    router.replace(token ? "/leagues" : "/login");
  }, [ready, token, router]);

  return <p className="muted">Loading…</p>;
}
