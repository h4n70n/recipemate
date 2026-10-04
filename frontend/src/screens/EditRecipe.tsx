import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  addTag,
  getRecipe,
  listTags,
  removeTag,
  updateRecipe,
  type RecipeDetail as RecipeDetailData,
  type RecipeOrigin,
} from "../api/client";
import { Button } from "../components/ui/Button";
import { EmptyState, ErrorBanner, Spinner } from "../components/ui/states";
import { TagPill } from "../components/ui/TagPill";
import { ChipInput } from "../components/ui/filters";
import { fieldErrorMessage, isNotFound, isAbort } from "../components/ui/errors";
import { useApiErrorHandler } from "../components/ui/useApiErrorHandler";
import { useToast } from "../components/ui/Toast";
import { RecipeForm } from "./RecipeForm";
import { toWriteBody, type RecipeFormValues } from "./recipeFormValues";

// NOTE ON API LIMITATIONS (docs/api.md):
// - `PATCH /recipes/{id}` only accepts recipe-level SCALAR fields (title,
//   description, prep/cook/total time, servings, origin, source_url,
//   source_citation). It does NOT touch ingredients, tools, or instructions.
// - Tags are managed via `POST /recipes/{id}/tags` and
//   `DELETE /recipes/{id}/tags/{tag_id}` — handled here.
// - There are NO write endpoints for ingredients / tools / instructions, so
//   those are rendered READ-ONLY below with an explanatory note. Structured-
//   field editing is intentionally not implemented until the API supports it.

function toFormValues(recipe: RecipeDetailData): RecipeFormValues {
  const str = (v: number | null) => (v == null ? "" : String(v));
  return {
    title: recipe.title,
    description: recipe.description ?? "",
    prep_time_min: str(recipe.prep_time_min),
    cook_time_min: str(recipe.cook_time_min),
    total_time_min: str(recipe.total_time_min),
    servings: str(recipe.servings),
    origin: (recipe.origin as RecipeOrigin | null) ?? "",
    source_url: recipe.source_url ?? "",
    source_citation: recipe.source_citation ?? "",
  };
}

/**
 * Edit recipe (web-ui-spec §5.6 edit). Pre-populated from `GET /recipes/{id}`.
 * Submitting PATCHes the scalar recipe fields. Tags are managed inline via the
 * tag endpoints. Ingredients/tools/instructions are read-only (see the API
 * limitation note above).
 */
export function EditRecipeScreen() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const handleError = useApiErrorHandler();

  const [recipe, setRecipe] = useState<RecipeDetailData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Tags: the recipe carries tag NAMES, but DELETE needs the tag id, so we
  // resolve names → ids from GET /tags. New tags get their id from the POST
  // response.
  const [tagIds, setTagIds] = useState<Record<string, string>>({});
  const [tagBusy, setTagBusy] = useState(false);

  const load = useCallback(
    (signal: AbortSignal) => {
      setLoading(true);
      setError(null);
      setNotFound(false);
      Promise.all([getRecipe(id, signal), listTags(signal)])
        .then(([recipeRes, tagsRes]) => {
          setRecipe(recipeRes);
          const map: Record<string, string> = {};
          for (const tag of tagsRes.tags) map[tag.name] = tag.id;
          setTagIds(map);
        })
        .catch((err) => {
          if (isAbort(err)) return;
          if (isNotFound(err)) setNotFound(true);
          else setError(handleError(err));
        })
        .finally(() => {
          if (!signal.aborted) setLoading(false);
        });
    },
    [id, handleError],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const handleSubmit = async (values: RecipeFormValues) => {
    setSaving(true);
    setFormError(null);
    try {
      await updateRecipe(id, toWriteBody(values));
      toast.show("Recipe updated.", "success");
      navigate(`/recipes/${id}`);
    } catch (err) {
      const fieldMsg = fieldErrorMessage(err);
      setFormError(fieldMsg ?? handleError(err));
      setSaving(false);
    }
  };

  const handleAddTag = async (next: string[]) => {
    if (!recipe) return;
    // The ChipInput below is used as an add-only box (its own chip list is
    // kept empty), so `next` is just the single new name wrapped in an array.
    const added = next.find((name) => !recipe.tags.includes(name));
    if (!added) return;
    setTagBusy(true);
    try {
      const tag = await addTag(id, added);
      setTagIds((prev) => ({ ...prev, [tag.name]: tag.id }));
      setRecipe((prev) =>
        prev && !prev.tags.includes(tag.name)
          ? { ...prev, tags: [...prev.tags, tag.name] }
          : prev,
      );
    } catch (err) {
      toast.show(handleError(err), "error");
    } finally {
      setTagBusy(false);
    }
  };

  const handleRemoveTag = async (name: string) => {
    if (!recipe) return;
    const tagId = tagIds[name];
    if (!tagId) {
      toast.show("Couldn't resolve that tag to remove.", "error");
      return;
    }
    setTagBusy(true);
    try {
      await removeTag(id, tagId);
      setRecipe((prev) =>
        prev ? { ...prev, tags: prev.tags.filter((t) => t !== name) } : prev,
      );
    } catch (err) {
      toast.show(handleError(err), "error");
    } finally {
      setTagBusy(false);
    }
  };

  if (loading) {
    return (
      <div style={{ padding: "var(--space-8)" }}>
        <Spinner />
      </div>
    );
  }

  if (notFound) {
    return (
      <EmptyState
        icon="🤷"
        title="Recipe not found"
        message="This recipe may have been deleted."
        action={
          <Link to="/" style={{ textDecoration: "none" }}>
            <Button variant="secondary">Back to Library</Button>
          </Link>
        }
      />
    );
  }

  if (error) {
    return (
      <ErrorBanner
        message={error}
        onRetry={() => {
          const controller = new AbortController();
          load(controller.signal);
        }}
      />
    );
  }

  if (!recipe) return null;

  return (
    <section style={{ maxWidth: 720, margin: "0 auto" }}>
      <h1>Edit recipe</h1>

      <RecipeForm
        initial={toFormValues(recipe)}
        submitLabel="Save changes"
        submitting={saving}
        formError={formError}
        onSubmit={handleSubmit}
        secondaryAction={
          <Link to={`/recipes/${id}`} style={{ textDecoration: "none" }}>
            <Button variant="secondary" type="button">
              Cancel
            </Button>
          </Link>
        }
      />

      {/* Tags — managed via POST/DELETE /recipes/{id}/tags */}
      <section style={{ marginTop: "var(--space-8)", maxWidth: 640 }}>
        <h2>Tags</h2>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-1)", marginBottom: "var(--space-3)" }}>
          {recipe.tags.length === 0 ? (
            <span style={{ color: "var(--color-text-secondary)" }}>No tags yet.</span>
          ) : (
            recipe.tags.map((tag) => (
              <TagPill key={tag} label={tag} selected onRemove={() => handleRemoveTag(tag)} />
            ))
          )}
        </div>
        <ChipInput
          label="Add a tag"
          placeholder="e.g. Italian"
          values={[]}
          onChange={handleAddTag}
        />
        {tagBusy && (
          <p style={{ color: "var(--color-text-secondary)", fontSize: "0.8125rem", marginTop: "var(--space-2)" }}>
            Updating tags…
          </p>
        )}
      </section>

      {/* Read-only structured fields — see API limitation note at top of file. */}
      <section style={{ marginTop: "var(--space-8)", maxWidth: 640 }}>
        <h2>Ingredients, tools &amp; instructions</h2>
        <p style={{ color: "var(--color-text-secondary)" }}>
          These structured fields are shown read-only. The current API has no
          write endpoints for ingredients, tools, or instructions, so editing
          them isn't supported yet.
        </p>

        {recipe.ingredients.length > 0 && (
          <>
            <h3>Ingredients</h3>
            <ul>
              {[...recipe.ingredients]
                .sort((a, b) => a.sort_order - b.sort_order)
                .map((ing) => (
                  <li key={ing.id}>
                    {[ing.quantity, ing.unit, ing.name].filter(Boolean).join(" ")}
                    {ing.preparation ? ` (${ing.preparation})` : ""}
                  </li>
                ))}
            </ul>
          </>
        )}

        {recipe.tools.length > 0 && (
          <>
            <h3>Tools</h3>
            <ul>
              {[...recipe.tools]
                .sort((a, b) => a.sort_order - b.sort_order)
                .map((tool) => (
                  <li key={tool.id}>{tool.name}</li>
                ))}
            </ul>
          </>
        )}

        {recipe.instructions.length > 0 && (
          <>
            <h3>Instructions</h3>
            <ol>
              {[...recipe.instructions]
                .sort((a, b) => a.step_number - b.step_number)
                .map((step) => (
                  <li key={step.id}>{step.body}</li>
                ))}
            </ol>
          </>
        )}
      </section>
    </section>
  );
}
