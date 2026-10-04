/**
 * Small presentation helpers shared across the component library. Kept in a
 * non-component module so eslint's `react-refresh/only-export-components` rule
 * stays happy (component files export only components).
 */

/** Format a minute count as a short badge label, e.g. `30 min`, `1 h 5 min`. */
export function formatMinutes(min: number | null | undefined): string | null {
  if (min == null || min <= 0) return null;
  if (min < 60) return `${min} min`;
  const hours = Math.floor(min / 60);
  const rest = min % 60;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}

/** Format an average rating to one decimal, or null when there is none. */
export function formatRating(avg: number | null | undefined): string | null {
  if (avg == null) return null;
  return avg.toFixed(1);
}

/** Format an ISO date (`YYYY-MM-DD` or full ISO) as a readable local date. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Today as a `YYYY-MM-DD` string, for date-input defaults. */
export function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}
