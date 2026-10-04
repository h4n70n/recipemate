import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  deleteCook,
  deleteRecipe,
  getRecipe,
  listCooks,
  type CookLog,
  type IngredientDetail,
  type RecipeDetail as RecipeDetailData,
} from "../api/client";
import { Button } from "../components/ui/Button";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { EmptyState, ErrorBanner, Spinner } from "../components/ui/states";
import { SkeletonBlock } from "../components/ui/states";
import { StarRating } from "../components/ui/StarRating";
import { TagPill } from "../components/ui/TagPill";
import { TimeBadge } from "../components/ui/TimeBadge";
import { formatDate, formatMinutes, formatRating } from "../components/ui/format";
import { isNotFound, isAbort } from "../components/ui/errors";
import { useApiErrorHandler } from "../components/ui/useApiErrorHandler";
import { useToast } from "../components/ui/Toast";
import { CookLogForm } from "./CookLogForm";

function ingredientLine(ingredient: IngredientDetail): string {
  const parts: string[] = [];
  if (ingredient.quantity != null) parts.push(String(ingredient.quantity));
  if (ingredient.unit) parts.push(ingredient.unit);
  parts.push(ingredient.name);
  let line = parts.join(" ");
  if (ingredient.preparation) line += ` (${ingredient.preparation})`;
  return line;
}

/**
 * Recipe detail (web-ui-spec §5.6 + §5.7) — the full recipe view: hero, meta
 * row, tags, origin/citation, ingredients, tools, numbered instructions, cook
 * summary, and the cook log history. Actions: Edit, Delete (confirm → DELETE →
 * back to Library), and Log a cook (opens the §5.7 form).
 */
export function RecipeDetailScreen() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const handleError = useApiErrorHandler();

  const [recipe, setRecipe] = useState<RecipeDetailData | null>(null);
  const [cooks, setCooks] = useState<CookLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const [cookFormOpen, setCookFormOpen] = useState(false);
  const [editingCook, setEditingCook] = useState<CookLog | null>(null);
  const [cookToDelete, setCookToDelete] = useState<CookLog | null>(null);
  const [deletingCook, setDeletingCook] = useState(false);

  const loadRecipe = useCallback(
    (signal: AbortSignal) => {
      setLoading(true);
      setError(null);
      setNotFound(false);
      Promise.all([getRecipe(id, signal), listCooks(id, signal)])
        .then(([recipeRes, cooksRes]) => {
          setRecipe(recipeRes);
          setCooks(cooksRes.cooks);
        })
        .catch((err) => {
          if (isAbort(err)) return;
          if (isNotFound(err)) {
            setNotFound(true);
          } else {
            setError(handleError(err));
          }
        })
        .finally(() => {
          if (!signal.aborted) setLoading(false);
        });
    },
    [id, handleError],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadRecipe(controller.signal);
    return () => controller.abort();
  }, [loadRecipe]);

  // Refetch recipe + cooks after a cook log changes so cook_count / avg_rating
  // reflect the new values.
  const refetch = useCallback(() => {
    const controller = new AbortController();
    loadRecipe(controller.signal);
  }, [loadRecipe]);

  const handleDeleteRecipe = async () => {
    setDeleting(true);
    try {
      await deleteRecipe(id);
      toast.show("Recipe deleted.", "success");
      navigate("/", { replace: true });
    } catch (err) {
      toast.show(handleError(err), "error");
      setDeleting(false);
      setConfirmDelete(false);
    }
  };

  const handleDeleteCook = async () => {
    if (!cookToDelete) return;
    setDeletingCook(true);
    try {
      await deleteCook(id, cookToDelete.id);
      toast.show("Cook log deleted.", "success");
      setCookToDelete(null);
      setDeletingCook(false);
      refetch();
    } catch (err) {
      toast.show(handleError(err), "error");
      setDeletingCook(false);
    }
  };

  if (loading) return <DetailSkeleton />;

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
    return <ErrorBanner message={error} onRetry={refetch} />;
  }

  if (!recipe) return null;

  const totalTime = recipe.total_time_min;
  const avg = formatRating(recipe.cook_summary.avg_rating);

  return (
    <article style={{ maxWidth: 820, margin: "0 auto" }}>
      {/* Hero */}
      <header style={{ marginBottom: "var(--space-6)" }}>
        {recipe.image_url && (
          <img
            src={recipe.image_url}
            alt={recipe.title}
            style={{
              width: "100%",
              maxHeight: 360,
              objectFit: "cover",
              borderRadius: "var(--radius-card)",
              marginBottom: "var(--space-4)",
            }}
          />
        )}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: "var(--space-4)",
            flexWrap: "wrap",
          }}
        >
          <h1 style={{ margin: 0 }}>{recipe.title}</h1>
          <div style={{ display: "flex", gap: "var(--space-2)" }}>
            <Link to={`/recipes/${recipe.id}/edit`} style={{ textDecoration: "none" }}>
              <Button variant="secondary">Edit</Button>
            </Link>
            <Button variant="danger" onClick={() => setConfirmDelete(true)}>
              Delete
            </Button>
          </div>
        </div>

        {recipe.description && (
          <p style={{ color: "var(--color-text-secondary)" }}>{recipe.description}</p>
        )}

        {/* Meta row */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "var(--space-4)",
            color: "var(--color-text-secondary)",
            fontSize: "0.9375rem",
            margin: "var(--space-3) 0",
          }}
        >
          {formatMinutes(recipe.prep_time_min) && (
            <span>Prep: {formatMinutes(recipe.prep_time_min)}</span>
          )}
          {formatMinutes(recipe.cook_time_min) && (
            <span>Cook: {formatMinutes(recipe.cook_time_min)}</span>
          )}
          <TimeBadge minutes={totalTime} />
          {recipe.servings != null && <span>Serves {recipe.servings}</span>}
        </div>

        {recipe.tags.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-1)" }}>
            {recipe.tags.map((tag) => (
              <TagPill key={tag} label={tag} />
            ))}
          </div>
        )}

        {(recipe.origin || recipe.source_citation || recipe.source_url) && (
          <p style={{ color: "var(--color-text-secondary)", fontSize: "0.875rem", marginTop: "var(--space-3)" }}>
            {recipe.origin && <span>Source: {recipe.origin}</span>}
            {recipe.source_citation && <span> · {recipe.source_citation}</span>}
            {recipe.source_url && (
              <>
                {" · "}
                <a href={recipe.source_url} target="_blank" rel="noopener noreferrer">
                  original
                </a>
              </>
            )}
          </p>
        )}

        {/* Cook summary */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "var(--space-3)",
            marginTop: "var(--space-4)",
          }}
        >
          <StarRating readOnly value={recipe.cook_summary.avg_rating} size={22} />
          <span style={{ color: "var(--color-text-secondary)" }}>
            {avg ? `${avg} avg` : "No ratings yet"} · cooked{" "}
            {recipe.cook_summary.cook_count}
            {"×"}
          </span>
        </div>
      </header>

      <div
        style={{
          display: "grid",
          gap: "var(--space-8)",
          gridTemplateColumns: "1fr",
        }}
      >
        {/* Ingredients */}
        <section>
          <h2>Ingredients</h2>
          {recipe.ingredients.length === 0 ? (
            <p style={{ color: "var(--color-text-secondary)" }}>No ingredients listed.</p>
          ) : (
            <ul style={{ paddingLeft: "var(--space-6)", lineHeight: 1.9 }}>
              {[...recipe.ingredients]
                .sort((a, b) => a.sort_order - b.sort_order)
                .map((ingredient) => (
                  <li key={ingredient.id}>{ingredientLine(ingredient)}</li>
                ))}
            </ul>
          )}
        </section>

        {/* Tools */}
        {recipe.tools.length > 0 && (
          <section>
            <h2>Tools</h2>
            <ul style={{ paddingLeft: "var(--space-6)", lineHeight: 1.9 }}>
              {[...recipe.tools]
                .sort((a, b) => a.sort_order - b.sort_order)
                .map((tool) => (
                  <li key={tool.id}>{tool.name}</li>
                ))}
            </ul>
          </section>
        )}

        {/* Instructions */}
        <section>
          <h2>Instructions</h2>
          {recipe.instructions.length === 0 ? (
            <p style={{ color: "var(--color-text-secondary)" }}>No instructions listed.</p>
          ) : (
            <ol style={{ paddingLeft: "var(--space-6)", lineHeight: 1.7, display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
              {[...recipe.instructions]
                .sort((a, b) => a.step_number - b.step_number)
                .map((step) => (
                  <li key={step.id}>{step.body}</li>
                ))}
            </ol>
          )}
        </section>

        {/* Cook log */}
        <section>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "var(--space-4)",
            }}
          >
            <h2 style={{ margin: 0 }}>Cook log</h2>
            <Button
              onClick={() => {
                setEditingCook(null);
                setCookFormOpen(true);
              }}
            >
              Log a cook
            </Button>
          </div>

          {cooks.length === 0 ? (
            <EmptyState
              icon="🍽"
              title="You haven't cooked this yet"
              message="Log your first cook to track how it went."
            />
          ) : (
            <ul style={{ listStyle: "none", padding: 0, display: "flex", flexDirection: "column", gap: "var(--space-3)", marginTop: "var(--space-4)" }}>
              {cooks.map((cook) => (
                <li
                  key={cook.id}
                  style={{
                    background: "var(--color-surface)",
                    borderRadius: "var(--radius-card)",
                    padding: "var(--space-4)",
                    display: "flex",
                    gap: "var(--space-4)",
                  }}
                >
                  {cook.photo_url && (
                    <img
                      src={cook.photo_url}
                      alt={`Cook from ${formatDate(cook.cooked_at)}`}
                      style={{ width: 96, height: 96, objectFit: "cover", borderRadius: "var(--radius-input)" }}
                    />
                  )}
                  <div style={{ flex: 1 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
                      <strong>{formatDate(cook.cooked_at)}</strong>
                      {cook.rating != null && <StarRating readOnly value={cook.rating} />}
                    </div>
                    {cook.notes && (
                      <p style={{ margin: "var(--space-2) 0 0", whiteSpace: "pre-wrap" }}>{cook.notes}</p>
                    )}
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setEditingCook(cook);
                        setCookFormOpen(true);
                      }}
                    >
                      Edit
                    </Button>
                    <Button variant="ghost" onClick={() => setCookToDelete(cook)}>
                      Delete
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {confirmDelete && (
        <ConfirmDialog
          title="Delete this recipe?"
          message="This removes the recipe from your library. This can't be undone here."
          confirmLabel="Delete"
          danger
          loading={deleting}
          onConfirm={handleDeleteRecipe}
          onCancel={() => setConfirmDelete(false)}
        />
      )}

      {cookFormOpen && (
        <CookLogForm
          recipeId={id}
          existing={editingCook ?? undefined}
          onSaved={() => {
            setCookFormOpen(false);
            setEditingCook(null);
            toast.show("Cook log saved.", "success");
            refetch();
          }}
          onClose={() => {
            setCookFormOpen(false);
            setEditingCook(null);
          }}
        />
      )}

      {cookToDelete && (
        <ConfirmDialog
          title="Delete this cook log?"
          message="This removes the entry and updates the recipe's rating."
          confirmLabel="Delete"
          danger
          loading={deletingCook}
          onConfirm={handleDeleteCook}
          onCancel={() => setCookToDelete(null)}
        />
      )}
    </article>
  );
}

function DetailSkeleton() {
  return (
    <div style={{ maxWidth: 820, margin: "0 auto" }} aria-hidden>
      <SkeletonBlock height={280} radius="var(--radius-card)" />
      <div style={{ marginTop: "var(--space-4)", display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
        <SkeletonBlock height={32} width="60%" />
        <SkeletonBlock height={16} width="90%" />
        <SkeletonBlock height={16} width="40%" />
        <div style={{ marginTop: "var(--space-6)" }}>
          <Spinner />
        </div>
      </div>
    </div>
  );
}
