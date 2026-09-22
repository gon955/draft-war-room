"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

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
    <div className="card" style={{ maxWidth: 420, margin: "48px auto" }}>
      <h2>Sign in</h2>
      <div className="body">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit(login);
          }}
        >
          <div className="row">
            <input
              type="email"
              placeholder="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              style={{ flex: 1 }}
              required
            />
          </div>
          <div className="row">
            <input
              type="password"
              placeholder="password (8+ characters)"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              style={{ flex: 1 }}
              required
            />
          </div>
          <div className="row">
            <button className="primary" type="submit" disabled={busy}>
              Log in
            </button>
            <button type="button" disabled={busy} onClick={() => void submit(register)}>
              Register
            </button>
          </div>
        </form>
        {error && <p className="error">{error}</p>}
        <p className="muted" style={{ marginTop: 12 }}>
          Registering logs you straight in — the API keeps register and login separate.
        </p>
      </div>
    </div>
  );
}
