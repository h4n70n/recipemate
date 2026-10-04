import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Spinner } from "./states";

type Variant = "primary" | "secondary" | "danger" | "ghost";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  loading?: boolean;
  children: ReactNode;
}

const variantStyles: Record<Variant, React.CSSProperties> = {
  primary: { background: "var(--color-primary)", color: "#fff", border: "1px solid var(--color-primary)" },
  secondary: {
    background: "var(--color-surface)",
    color: "var(--color-text-primary)",
    border: "1px solid rgba(31, 27, 22, 0.18)",
  },
  danger: { background: "var(--color-error)", color: "#fff", border: "1px solid var(--color-error)" },
  ghost: { background: "transparent", color: "var(--color-text-primary)", border: "1px solid transparent" },
};

/** Button (shared) — consistent CTA styling with a loading spinner state. */
export function Button({
  variant = "primary",
  loading = false,
  disabled,
  children,
  style,
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || loading;
  return (
    <button
      {...rest}
      disabled={isDisabled}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        gap: "var(--space-2)",
        padding: "var(--space-2) var(--space-4)",
        borderRadius: "var(--radius-input)",
        font: "inherit",
        fontWeight: 600,
        cursor: isDisabled ? "not-allowed" : "pointer",
        opacity: isDisabled ? 0.65 : 1,
        ...variantStyles[variant],
        ...style,
      }}
    >
      {loading && <Spinner size={14} />}
      {children}
    </button>
  );
}
