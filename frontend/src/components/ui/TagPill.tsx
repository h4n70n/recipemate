interface TagPillProps {
  label: string;
  /** Selected (filter active) styling. */
  selected?: boolean;
  /** When provided, renders a × button and calls this on remove. */
  onRemove?: () => void;
  /** When provided (and not removable), the pill is a toggle button. */
  onClick?: () => void;
}

/**
 * TagPill (web-ui-spec §6) — a rounded label with three variants:
 *   - default: static pill.
 *   - selected: filled with the primary color (active filter).
 *   - removable: shows a × that fires `onRemove`.
 */
export function TagPill({ label, selected, onRemove, onClick }: TagPillProps) {
  const base: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: "var(--space-1)",
    padding: "var(--space-1) var(--space-3)",
    borderRadius: "var(--radius-pill)",
    fontSize: "0.8125rem",
    fontWeight: 600,
    border: "1px solid",
    borderColor: selected ? "var(--color-primary)" : "rgba(31, 27, 22, 0.14)",
    background: selected ? "var(--color-primary)" : "var(--color-surface)",
    color: selected ? "#fff" : "var(--color-text-primary)",
    cursor: onClick ? "pointer" : "default",
  };

  const content = (
    <>
      {label}
      {onRemove && (
        <button
          type="button"
          aria-label={`Remove ${label}`}
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          style={{
            border: "none",
            background: "transparent",
            color: "inherit",
            cursor: "pointer",
            padding: 0,
            marginLeft: 2,
            fontSize: "1rem",
            lineHeight: 1,
          }}
        >
          ×
        </button>
      )}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        aria-pressed={selected}
        style={{ ...base, font: "inherit" }}
      >
        {content}
      </button>
    );
  }

  return <span style={base}>{content}</span>;
}
