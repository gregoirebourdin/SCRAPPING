/* Research brand mark. Single path, `currentColor` fill: white on the dark UI, black on light. */

export const BRAND_NAME = "Research";

const MARK_PATH = "M0 0H104.9C133.2 0 157.6 22.5 157.6 48.6V108.7H102.9V50.8H0ZM0 80.7C87.8 80.7 157.6 134.4 157.6 201.9H99.9C99.9 163.3 59.3 131.9 9.3 131.9H0Z";

export function LogoMark({ size = 20, className, title }: { size?: number; className?: string; title?: string }) {
  return (
    <svg
      width={(size * 157.6) / 201.9}
      height={size}
      viewBox="0 0 157.6 201.9"
      fill="currentColor"
      className={className}
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      <path d={MARK_PATH} />
    </svg>
  );
}

/** Mark + wordmark, e.g. in the sidebar and on the sign-in page. */
export function Logo({ size = 18, wordmark = true, className }: { size?: number; wordmark?: boolean; className?: string }) {
  return (
    <span className={["inline-flex items-center gap-2 text-fg", className].filter(Boolean).join(" ")}>
      <LogoMark size={size} />
      {wordmark && <span className="font-display text-[15px] font-bold tracking-[-0.015em]">{BRAND_NAME}</span>}
    </span>
  );
}
