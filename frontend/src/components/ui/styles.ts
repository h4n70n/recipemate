import type { CSSProperties } from "react";

/** Shared inline-style fragments reused across form controls. */

/** Base styling for text inputs, selects, and textareas. */
export const inputStyle: CSSProperties = {
  padding: "var(--space-2) var(--space-3)",
  borderRadius: "var(--radius-input)",
  border: "1px solid rgba(31, 27, 22, 0.18)",
  background: "var(--color-surface)",
  font: "inherit",
  color: "inherit",
  width: "100%",
};
