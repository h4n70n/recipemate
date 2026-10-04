import type { RecipeOrigin, RecipeWriteBody } from "../api/client";

/** String-keyed form state for the shared recipe form (inputs are strings). */
export interface RecipeFormValues {
  title: string;
  description: string;
  prep_time_min: string;
  cook_time_min: string;
  total_time_min: string;
  servings: string;
  origin: RecipeOrigin | "";
  source_url: string;
  source_citation: string;
}

export const EMPTY_RECIPE_FORM: RecipeFormValues = {
  title: "",
  description: "",
  prep_time_min: "",
  cook_time_min: "",
  total_time_min: "",
  servings: "",
  origin: "",
  source_url: "",
  source_citation: "",
};

/** Parse a numeric form string to int|null, treating blank as null. */
function toIntOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

/**
 * Build a {@link RecipeWriteBody} from form values. Nullable fields are always
 * included so that, on a PATCH, clearing a field sends an explicit null
 * (clearing the stored value); `title` is trimmed and always present. For a
 * POST the backend treats omitted-vs-null identically.
 */
export function toWriteBody(values: RecipeFormValues): RecipeWriteBody {
  return {
    title: values.title.trim(),
    description: values.description.trim() || null,
    prep_time_min: toIntOrNull(values.prep_time_min),
    cook_time_min: toIntOrNull(values.cook_time_min),
    total_time_min: toIntOrNull(values.total_time_min),
    servings: toIntOrNull(values.servings),
    origin: values.origin || null,
    source_url: values.source_url.trim() || null,
    source_citation: values.source_citation.trim() || null,
  };
}
