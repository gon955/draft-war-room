"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Crosshair } from "@/components/Crosshair";
import { Panel } from "@/components/Hud";

/** Half a court, top down, drawn as a tactical overlay behind the title. */
function CourtOverlay() {
  return (
    <svg className="court" viewBox="0 0 500 470" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <g fill="none" stroke="#465236" strokeWidth="2">
        <rect x="10" y="10" width="480" height="450" />
        <rect x="170" y="10" width="160" height="190" />
        <circle cx="250" cy="200" r="60" />
        <path d="M190 200a60 60 0 0 0 120 0" strokeDasharray="8 8" />
        <path d="M40 10v140a210 210 0 0 0 420 0V10" />
        <circle cx="250" cy="52" r="7.5" stroke="#ffb000" />
        <path d="M220 40h60" />
        <path d="M190 460a60 60 0 0 1 120 0" />
      </g>
      {/* A play drawn in: X's, O's and a cut. */}
      <g stroke="#ffb000" strokeWidth="2" fill="none" opacity=".75">
        <path d="M120 300q60 -40 110 -90" strokeDasharray="6 6" />
        <path d="M222 214l8 -4 -2 9" />
        <path d="M380 280q-40 -60 -100 -60" />
        <circle cx="120" cy="300" r="9" />
        <circle cx="380" cy="280" r="9" />
      </g>
      <g stroke="#e5484d" strokeWidth="2.4">
        <path d="M300 330l14 14M314 330l-14 14M180 380l14 14M194 380l-14 14" />
      </g>
    </svg>
  );
}

export default function LoginPage() {
  const { ready, token, login, register } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (ready && token) router.replace("/leagues");
  }, [ready, token, router]);

  async function submit(action: (e: string, p: string) => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action(email, password);
      router.push("/leagues");
    } catch (e) {
      // 409 on register and 401 on login both arrive here with the API's own
      // wording, which is deliberately vague about which half was wrong.
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <div className="gate-art">
        <CourtOverlay />
        <div className="op-kicker" style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Crosshair size={26} />
          Draft operations {"//"} restricted
        </div>
        <div>
          <h1 className="gate-title">
            Draft
            <span>War Room</span>
          </h1>
          <p className="gate-lede">
            Value over replacement on your league&rsquo;s own scoring, a board of your own, and a
            room of bots that draft the way your leaguemates will.
          </p>
        </div>
        <div className="gate-foot">
          <span>Sector: points league</span>
          <span>Source: ESPN</span>
          <span>Clearance: manager</span>
        </div>
      </div>

      <div className="gate-form">
        <Panel code="AUTH" title="Access terminal" hot>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void submit(login);
            }}
          >
            <div className="field">
              <label htmlFor="email">Operator email</label>
              <input
                id="email"
                type="email"
                placeholder="you@example.com"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="password">Passphrase</label>
              <input
                id="password"
                type="password"
                placeholder="8+ characters"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>
            {error && <p className="error">{error}</p>}
            <div className="actions">
              <button className="primary big" type="submit" disabled={busy}>
                {busy ? "Verifying…" : "Log in"}
              </button>
              <button className="big" type="button" disabled={busy} onClick={() => void submit(register)}>
                Register
              </button>
            </div>
          </form>
          <p className="gate-note">
            Registering logs you straight in — the API keeps register and login separate.
          </p>
        </Panel>
      </div>
    </div>
  );
}
