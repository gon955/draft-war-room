/** The War Room mark: a basketball's seams inside a targeting reticle. */
export function Crosshair({ size = 28, className }: { size?: number; className?: string }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      aria-hidden
    >
      <circle cx="16" cy="16" r="9" />
      <path d="M7 16h18M16 7v18" strokeWidth="1.2" opacity=".75" />
      <path d="M9.6 9.8c3.4 3.6 3.4 8.8 0 12.4M22.4 9.8c-3.4 3.6-3.4 8.8 0 12.4" strokeWidth="1.2" opacity=".75" />
      <path d="M16 1v4M16 27v4M1 16h4M27 16h4" strokeWidth="2" />
      <path d="M3 8V3h5M29 8V3h-5M3 24v5h5M29 24v5h-5" strokeWidth="1.6" />
    </svg>
  );
}
