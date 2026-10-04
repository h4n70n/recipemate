import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

/**
 * Modal (used by ConfirmDialog and the cook log form) — a centered dialog with
 * a backdrop. Closes on Escape and backdrop click, traps initial focus on the
 * panel, and restores focus to the previously focused element on close.
 */
export function Modal({
  title,
  onClose,
  children,
  labelledBy,
}: {
  title?: string;
  onClose: () => void;
  children: ReactNode;
  labelledBy?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previouslyFocused.current?.focus?.();
    };
  }, [onClose]);

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(31, 27, 22, 0.45)",
        display: "grid",
        placeItems: "center",
        padding: "var(--space-4)",
        zIndex: 100,
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={labelledBy ? undefined : title}
        aria-labelledby={labelledBy}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--color-surface)",
          borderRadius: "var(--radius-card)",
          padding: "var(--space-6)",
          maxWidth: 480,
          width: "100%",
          boxShadow: "0 12px 40px rgba(31, 27, 22, 0.25)",
          maxHeight: "90vh",
          overflowY: "auto",
        }}
      >
        {title && <h2 style={{ marginTop: 0 }}>{title}</h2>}
        {children}
      </div>
    </div>
  );
}
