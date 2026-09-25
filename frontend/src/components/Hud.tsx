import type { ReactNode } from "react";

/**
 * The building blocks every screen is assembled from. Kept presentational —
 * no data fetching, no state — so a screen's logic stays in the screen and the
 * look stays here.
 */

/** A bracketed HUD panel with a coded header strip. */
export function Panel({
  code,
  title,
  meta,
  hot = false,
  flush = false,
  id,
  children,
  foot,
}: {
  code: string;
  title: ReactNode;
  meta?: ReactNode;
  /** Amber corner brackets — for the one panel on a screen that is the point. */
  hot?: boolean;
  /** No body padding, for tables that run edge to edge. */
  flush?: boolean;
  id?: string;
  children: ReactNode;
  foot?: ReactNode;
}) {
  return (
    <section className={hot ? "panel hot" : "panel"} id={id}>
      <header className="panel-head">
        <span className="panel-code">{code}</span>
        <h2>{title}</h2>
        {meta && <div className="panel-meta">{meta}</div>}
      </header>
      <div className={flush ? "panel-body flush" : "panel-body"}>{children}</div>
      {foot && <div className="panel-foot">{foot}</div>}
    </section>
  );
}

/** The big title block at the top of a screen. */
export function OpHeader({
  kicker,
  title,
  sub,
  side,
}: {
  kicker: string[];
  title: ReactNode;
  sub?: ReactNode;
  side?: ReactNode;
}) {
  return (
    <header className="op-head">
      <div style={{ minWidth: 0 }}>
        <div className="op-kicker">
          {kicker.map((k, i) => (
            <span key={k}>
              {i > 0 && <span className="slash">{"//"}</span>}
              {k}
            </span>
          ))}
        </div>
        <h1 className="op-title">{title}</h1>
        {sub && <p className="op-sub">{sub}</p>}
      </div>
      {side && <div className="op-side">{side}</div>}
    </header>
  );
}

export type Tone = "amber" | "go" | "signal" | "caution";

/** One stat tile. `value` is shown large; `unit` small beside it. */
export function Readout({
  label,
  value,
  unit,
  sub,
  tone,
  meter,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  sub?: ReactNode;
  tone?: Exclude<Tone, "caution">;
  /** 0..1 — draws a segmented bar under the value. */
  meter?: number;
}) {
  return (
    <div className={tone ? `readout ${tone}` : "readout"}>
      <div className="readout-label">{label}</div>
      <div className="readout-value">
        {value}
        {unit && <small>{unit}</small>}
      </div>
      {meter !== undefined && <Meter value={meter} tone={tone === "go" ? "go" : undefined} />}
      {sub && <div className="readout-sub">{sub}</div>}
    </div>
  );
}

/** A segmented 0..1 bar. Decorative: always pair it with the number. */
export function Meter({ value, tone }: { value: number; tone?: Exclude<Tone, "amber"> }) {
  const pct = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)) * 100;
  return (
    <div className={tone ? `meter ${tone}` : "meter"} aria-hidden>
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      {children}
    </div>
  );
}

export function Loading({ label = "Establishing link" }: { label?: string }) {
  return <p className="loading">{label}</p>;
}

/** Zero-padded index, the way every list on a board is numbered. */
export function pad(n: number, width = 2): string {
  return String(n).padStart(width, "0");
}
