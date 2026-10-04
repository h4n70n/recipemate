import { formatMinutes } from "./format";

/**
 * TimeBadge (web-ui-spec §6) — renders a total-time pill like "30 min".
 * Returns null when there is no usable time so callers can drop it cleanly.
 */
export function TimeBadge({ minutes }: { minutes: number | null | undefined }) {
  const label = formatMinutes(minutes);
  if (!label) return null;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--space-1)",
        padding: "var(--space-1) var(--space-2)",
        borderRadius: "var(--radius-pill)",
        background: "rgba(31, 27, 22, 0.06)",
        color: "var(--color-text-secondary)",
        fontSize: "0.8125rem",
        fontWeight: 600,
      }}
    >
      <span aria-hidden>⏱</span>
      {label}
    </span>
  );
}
