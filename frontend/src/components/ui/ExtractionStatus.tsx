import type { ExtractionStatus as Status } from "../../api/client";
import { Spinner } from "./states";

const config: Record<
  Status,
  { label: string; color: string; icon: string; spin: boolean }
> = {
  pending: { label: "Queued", color: "var(--color-warning)", icon: "⏳", spin: false },
  processing: {
    label: "Reading your recipe…",
    color: "var(--color-warning)",
    icon: "",
    spin: true,
  },
  complete: { label: "Done", color: "var(--color-success)", icon: "✓", spin: false },
  failed: { label: "Extraction failed", color: "var(--color-error)", icon: "✕", spin: false },
};

/**
 * ExtractionStatus indicator (web-ui-spec §6) — a colored status chip for the
 * four async-extraction states (pending / processing / complete / failed).
 */
export function ExtractionStatusIndicator({ status }: { status: Status }) {
  const { label, color, icon, spin } = config[status];
  return (
    <span
      role="status"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--space-2)",
        padding: "var(--space-2) var(--space-3)",
        borderRadius: "var(--radius-pill)",
        background: "rgba(31, 27, 22, 0.04)",
        color,
        fontWeight: 600,
      }}
    >
      {spin ? <Spinner size={14} /> : <span aria-hidden>{icon}</span>}
      {label}
    </span>
  );
}
