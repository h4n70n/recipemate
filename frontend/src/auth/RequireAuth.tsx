import { Navigate, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "./AuthProvider";

/**
 * Route guard: renders children only for an authenticated session. While the
 * initial session check runs it shows a lightweight loading state; otherwise
 * it bounces to the Sign in screen, preserving the attempted location so we
 * can return there after login.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div role="status" style={{ padding: "var(--space-8)" }}>
        Loading…
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/signin" replace state={{ from: location }} />;
  }

  return <>{children}</>;
}
