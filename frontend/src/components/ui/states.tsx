import type { ReactNode } from "react";

/**
 * EmptyState (web-ui-spec §6) — a centered friendly empty message with an
 * optional call-to-action.
 */
export function EmptyState({
  title,
  message,
  action,
  icon = "🍳",
}: {
  title: string;
  message?: string;
  action?: ReactNode;
  icon?: string;
}) {
  return (
    <div
      style={{
        textAlign: "center",
        padding: "var(--space-12) var(--space-6)",
        color: "var(--color-text-secondary)",
      }}
    >
      <div aria-hidden style={{ fontSize: "2.5rem", marginBottom: "var(--space-3)" }}>
        {icon}
      </div>
      <h2 style={{ margin: "0 0 var(--space-2)", color: "var(--color-text-primary)" }}>
        {title}
      </h2>
      {message && <p style={{ margin: "0 0 var(--space-4)" }}>{message}</p>}
      {action}
    </div>
  );
}

/**
 * ErrorBanner (web-ui-spec §6) — an inline error with an optional Retry
 * button. Uses `role="alert"` so it is announced to assistive tech.
 */
export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--space-3)",
        padding: "var(--space-3) var(--space-4)",
        borderRadius: "var(--radius-input)",
        background: "rgba(198, 40, 40, 0.08)",
        border: "1px solid rgba(198, 40, 40, 0.25)",
        color: "var(--color-error)",
      }}
    >
      <span style={{ flex: 1 }}>{message}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          style={{
            border: "1px solid currentColor",
            background: "transparent",
            color: "inherit",
            borderRadius: "var(--radius-input)",
            padding: "var(--space-1) var(--space-3)",
            cursor: "pointer",
            fontWeight: 600,
          }}
        >
          Retry
        </button>
      )}
    </div>
  );
}

/** Spinner (web-ui-spec §7) — a lightweight inline spinner for action states. */
export function Spinner({ size = 18 }: { size?: number }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      style={{
        display: "inline-block",
        width: size,
        height: size,
        border: "2px solid rgba(31, 27, 22, 0.2)",
        borderTopColor: "var(--color-primary)",
        borderRadius: "50%",
        animation: "rm-spin 0.7s linear infinite",
      }}
    />
  );
}

/** A single skeleton block used by loading placeholders. */
export function SkeletonBlock({
  height = 16,
  width = "100%",
  radius = "var(--radius-input)",
}: {
  height?: number | string;
  width?: number | string;
  radius?: string;
}) {
  return (
    <span
      aria-hidden
      style={{
        display: "block",
        height,
        width,
        borderRadius: radius,
        background:
          "linear-gradient(90deg, rgba(31,27,22,0.06) 25%, rgba(31,27,22,0.12) 37%, rgba(31,27,22,0.06) 63%)",
        backgroundSize: "400% 100%",
        animation: "rm-shimmer 1.4s ease infinite",
      }}
    />
  );
}
