import { ApiError } from "../../api/client";

/**
 * Map an unknown thrown value to a user-facing message, applying the
 * cross-cutting rules from web-ui-spec §7:
 *   403 → "You don't have access to this recipe."
 *   404 → not-found message
 *   400/422 → the API's own `{"error": ...}` message (field validation /
 *             business rule), which {@link ApiError} already surfaces.
 *   500/other → generic retry message.
 *
 * 401 is handled separately (the caller bounces to sign in) — see
 * {@link isUnauthorized}.
 */
export function messageForError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 403:
        return "You don't have access to this recipe.";
      case 404:
        return "We couldn't find that recipe. It may have been deleted.";
      case 400:
      case 422:
        return err.message;
      case 500:
        return "Something went wrong on our end. Please try again.";
      default:
        return err.message;
    }
  }
  if (err instanceof Error) return err.message;
  return "An unexpected error occurred.";
}

/** True when the error is a 401 (session expired / missing). */
export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

/** True when the error is a 404. */
export function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

/** True when the error is a 403. */
export function isForbidden(err: unknown): boolean {
  return err instanceof ApiError && err.status === 403;
}

/**
 * For a 400, the API returns a single `{"error": "..."}` message rather than
 * per-field errors, so we surface it as a form-level message. This helper
 * returns that message when the error is a 400, else null.
 */
export function fieldErrorMessage(err: unknown): string | null {
  if (err instanceof ApiError && err.status === 400) return err.message;
  return null;
}

/** Ignore AbortError (from cancelled in-flight requests). */
export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}
