import { useId } from "react";
import type { ReactNode } from "react";

/**
 * FormField (web-ui-spec §6) — a label + control + inline error wrapper.
 * Pass the field control via render-prop so the generated id/aria wiring is
 * applied to the actual input/select/textarea.
 */
export function FormField({
  label,
  error,
  hint,
  required,
  children,
}: {
  label: string;
  error?: string | null;
  hint?: string;
  required?: boolean;
  children: (props: {
    id: string;
    "aria-invalid": boolean | undefined;
    "aria-describedby": string | undefined;
  }) => ReactNode;
}) {
  const id = useId();
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  const describedBy =
    [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ") ||
    undefined;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
      <label htmlFor={id} style={{ fontWeight: 600, fontSize: "0.875rem" }}>
        {label}
        {required && (
          <span aria-hidden style={{ color: "var(--color-error)" }}>
            {" "}
            *
          </span>
        )}
      </label>
      {hint && (
        <span id={hintId} style={{ fontSize: "0.8125rem", color: "var(--color-text-secondary)" }}>
          {hint}
        </span>
      )}
      {children({
        id,
        "aria-invalid": error ? true : undefined,
        "aria-describedby": describedBy,
      })}
      {error && (
        <span id={errorId} role="alert" style={{ color: "var(--color-error)", fontSize: "0.8125rem" }}>
          {error}
        </span>
      )}
    </div>
  );
}
