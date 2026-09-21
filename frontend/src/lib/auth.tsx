"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";
import { useRouter } from "next/navigation";
import { api, setUnauthorizedHandler } from "./api";
import type { Token, User } from "./models";

const KEY = "warroom.token";

// --------------------------------------------------------------------------
// localStorage as an external store.
//
// Reading it with useEffect + setState works but trips
// react-hooks/set-state-in-effect, and the rule has a point: that is a render,
// then an effect, then a second render on every mount. useSyncExternalStore is
// the API built for exactly this — an outside source of truth React must stay
// subscribed to — and its getServerSnapshot is what makes the value safe under
// `output: "export"`, where these components are prerendered to HTML at build
// time and localStorage does not exist at all.
// --------------------------------------------------------------------------

const listeners = new Set<() => void>();

function subscribe(onChange: () => void) {
  listeners.add(onChange);
  // `storage` fires in OTHER tabs, so logging out in one logs out the rest.
  window.addEventListener("storage", onChange);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onChange);
  };
}

function readToken(): string | null {
  // A private window or blocked site data makes this throw rather than return
  // null, and an exception here would blank the app on first paint.
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function writeToken(value: string | null) {
  try {
    if (value === null) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, value);
  } catch {
    /* storage unavailable: the session just will not survive a reload */
  }
  listeners.forEach((l) => l());
}

/** null during prerender — there is no browser storage at build time. */
const serverToken = () => null;
/** false during prerender, true once hydrated, with no setState anywhere. */
const serverFalse = () => false;
const clientTrue = () => true;

type AuthValue = {
  token: string | null;
  user: User | null;
  /** False until the browser has taken over and storage has been read. */
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const token = useSyncExternalStore(subscribe, readToken, serverToken);
  const ready = useSyncExternalStore(subscribe, clientTrue, serverFalse);
  const [user, setUser] = useState<User | null>(null);

  const logout = useCallback(() => {
    writeToken(null);
    setUser(null);
  }, []);

  // Any 401 from anywhere clears the session once, centrally.
  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    api<User>("GET", "/auth/me", { token })
      .then((me) => {
        if (!cancelled) setUser(me);
      })
      .catch(() => {
        /* a 401 already triggered logout through the handler above */
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const login = useCallback(async (email: string, password: string) => {
    const res = await api<Token>("POST", "/auth/login", { body: { email, password } });
    writeToken(res.access_token);
  }, []);

  const register = useCallback(
    async (email: string, password: string) => {
      // POST /auth/register returns 201 with a UserOut and NO token — SPEC 4
      // keeps them separate — so log in afterwards rather than assume a session.
      await api<User>("POST", "/auth/register", { body: { email, password } });
      await login(email, password);
    },
    [login],
  );

  return (
    <AuthContext.Provider
      value={{ token, user: token ? user : null, ready, login, register, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth used outside AuthProvider");
  return ctx;
}

/** Send the browser to /login once auth state is known and there is no token. */
export function useRequireAuth() {
  const auth = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (auth.ready && !auth.token) router.replace("/login");
  }, [auth.ready, auth.token, router]);
  return auth;
}
