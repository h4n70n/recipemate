import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { isUnauthorized, messageForError } from "./errors";

/**
 * Shared API-error handling (web-ui-spec §7). Returns a `handle` function that:
 *   - on 401, bounces to the Sign in screen (preserving the attempted
 *     location so the user returns here after re-authenticating), and
 *   - otherwise resolves the value to a user-facing message string.
 *
 * Screens use the returned string to populate inline/banner error states, and
 * rely on the redirect side effect for 401.
 */
export function useApiErrorHandler() {
  const navigate = useNavigate();
  const location = useLocation();

  return useCallback(
    (err: unknown): string => {
      if (isUnauthorized(err)) {
        navigate("/signin", { replace: true, state: { from: location } });
        return "Your session has expired. Please sign in again.";
      }
      return messageForError(err);
    },
    [navigate, location],
  );
}
