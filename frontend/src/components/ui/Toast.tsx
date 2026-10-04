import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

type ToastKind = "success" | "error" | "info";

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastContextValue {
  /** Show a transient toast. Defaults to a 4s auto-dismiss. */
  show: (message: string, kind?: ToastKind, durationMs?: number) => void;
}

const ToastContext = createContext<ToastContextValue | undefined>(undefined);

const kindColor: Record<ToastKind, string> = {
  success: "var(--color-success)",
  error: "var(--color-error)",
  info: "var(--color-text-primary)",
};

/**
 * Toast (web-ui-spec §6) — a stacked, auto-dismissing notification surface.
 * Wrap the app once; call `useToast().show(...)` from anywhere beneath it.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (message: string, kind: ToastKind = "info", durationMs = 4000) => {
      const id = nextId.current++;
      setToasts((current) => [...current, { id, kind, message }]);
      window.setTimeout(() => dismiss(id), durationMs);
    },
    [dismiss],
  );

  const value = useMemo<ToastContextValue>(() => ({ show }), [show]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        style={{
          position: "fixed",
          bottom: "var(--space-6)",
          right: "var(--space-6)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-2)",
          zIndex: 200,
        }}
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role="status"
            onClick={() => dismiss(toast.id)}
            style={{
              background: "var(--color-surface)",
              borderLeft: `4px solid ${kindColor[toast.kind]}`,
              borderRadius: "var(--radius-input)",
              padding: "var(--space-3) var(--space-4)",
              boxShadow: "0 6px 20px rgba(31, 27, 22, 0.18)",
              maxWidth: 360,
              cursor: "pointer",
            }}
          >
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (ctx === undefined) {
    throw new Error("useToast must be used within a <ToastProvider>");
  }
  return ctx;
}
