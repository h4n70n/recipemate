import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  listRecipes,
  listTags,
  search,
  type RecipeCard as RecipeCardData,
  type SearchParams,
  type SearchSort,
} from "../api/client";
import { Button } from "../components/ui/Button";
import { EmptyState, ErrorBanner } from "../components/ui/states";
import { RecipeCardGrid, RecipeGridSkeleton } from "../components/ui/RecipeGrid";
import {
  ChipInput,
  MultiSelect,
  SortDropdown,
  TimeStepper,
} from "../components/ui/filters";
import { TagPill } from "../components/ui/TagPill";
import { isAbort } from "../components/ui/errors";
import { useApiErrorHandler } from "../components/ui/useApiErrorHandler";

const PAGE_SIZE = 20;

interface Filters {
  ingredients: string[];
  tags: string[];
  tools: string[];
  maxTime: number | null;
  q: string;
  sort: SearchSort;
}

const EMPTY_FILTERS: Filters = {
  ingredients: [],
  tags: [],
  tools: [],
  maxTime: null,
  q: "",
  sort: "newest",
};

/** True when any filter departs from the default (so we hit /search). */
function filtersActive(f: Filters): boolean {
  return (
    f.ingredients.length > 0 ||
    f.tags.length > 0 ||
    f.tools.length > 0 ||
    f.maxTime !== null ||
    f.q.trim() !== "" ||
    f.sort !== "newest"
  );
}

function toSearchParams(f: Filters, offset: number): SearchParams {
  return {
    ingredient: f.ingredients,
    tag: f.tags,
    tool: f.tools,
    max_time: f.maxTime ?? undefined,
    q: f.q.trim() || undefined,
    sort: f.sort,
    limit: PAGE_SIZE,
    offset,
  };
}

/**
 * Library (web-ui-spec §5.2 + §5.3) — the recipe grid with a filter sidebar.
 * With no active filters it lists the user's recipes via `GET /recipes`; once
 * any filter is set it drives `GET /search`. Handles loading (skeletons),
 * empty, and error (retry) states, with limit/offset pagination.
 */
export function LibraryScreen() {
  const handleError = useApiErrorHandler();

  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState(0);
  const [recipes, setRecipes] = useState<RecipeCardData[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tagSuggestions, setTagSuggestions] = useState<string[]>([]);

  const active = useMemo(() => filtersActive(filters), [filters]);

  // Load the user's known tags once, for the sidebar multi-select + chip hints.
  useEffect(() => {
    const controller = new AbortController();
    listTags(controller.signal)
      .then((res) => setTagSuggestions(res.tags.map((t) => t.name)))
      .catch((err) => {
        if (!isAbort(err)) {
          // Tag suggestions are non-critical; swallow quietly.
        }
      });
    return () => controller.abort();
  }, []);

  const load = useCallback(
    (signal: AbortSignal) => {
      setLoading(true);
      setError(null);
      const offset = page * PAGE_SIZE;
      const request = active
        ? search(toSearchParams(filters, offset), signal)
        : listRecipes({ limit: PAGE_SIZE, offset }, signal);
      request
        .then((res) => {
          setRecipes(res.recipes);
          setTotal(res.total);
        })
        .catch((err) => {
          if (isAbort(err)) return;
          setError(handleError(err));
        })
        .finally(() => {
          if (!signal.aborted) setLoading(false);
        });
    },
    [active, filters, page, handleError],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // Any filter change resets to the first page.
  const updateFilters = (patch: Partial<Filters>) => {
    setFilters((prev) => ({ ...prev, ...patch }));
    setPage(0);
  };

  const clearFilters = () => {
    setFilters(EMPTY_FILTERS);
    setPage(0);
  };

  const retry = () => {
    const controller = new AbortController();
    load(controller.signal);
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <section>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--space-4)",
          marginBottom: "var(--space-6)",
          flexWrap: "wrap",
        }}
      >
        <h1 style={{ margin: 0 }}>Library</h1>
        <Link to="/add" style={{ textDecoration: "none" }}>
          <Button>Add recipe +</Button>
        </Link>
      </header>

      <div className="rm-library-layout">
        <aside
          aria-label="Filters"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-4)",
            background: "var(--color-surface)",
            borderRadius: "var(--radius-card)",
            padding: "var(--space-4)",
          }}
        >
          <ChipInput
            label="Ingredients"
            placeholder="e.g. chicken"
            values={filters.ingredients}
            onChange={(ingredients) => updateFilters({ ingredients })}
          />
          <ChipInput
            label="Title contains"
            placeholder="e.g. pasta"
            values={filters.q ? [filters.q] : []}
            onChange={(vals) => updateFilters({ q: vals[vals.length - 1] ?? "" })}
          />
          <TimeStepper
            value={filters.maxTime}
            onChange={(maxTime) => updateFilters({ maxTime })}
          />
          <MultiSelect
            label="Tags"
            options={tagSuggestions}
            selected={filters.tags}
            onChange={(tags) => updateFilters({ tags })}
          />
          <ChipInput
            label="Tools"
            placeholder="e.g. cast iron"
            values={filters.tools}
            onChange={(tools) => updateFilters({ tools })}
          />
          <SortDropdown value={filters.sort} onChange={(sort) => updateFilters({ sort })} />
          {active && (
            <Button variant="ghost" onClick={clearFilters}>
              Clear filters
            </Button>
          )}
        </aside>

        <div>
          {active && (
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: "var(--space-1)",
                marginBottom: "var(--space-4)",
              }}
            >
              {filters.ingredients.map((v) => (
                <TagPill
                  key={`ing-${v}`}
                  label={`Ingredient: ${v}`}
                  selected
                  onRemove={() =>
                    updateFilters({ ingredients: filters.ingredients.filter((x) => x !== v) })
                  }
                />
              ))}
              {filters.tags.map((v) => (
                <TagPill
                  key={`tag-${v}`}
                  label={`Tag: ${v}`}
                  selected
                  onRemove={() => updateFilters({ tags: filters.tags.filter((x) => x !== v) })}
                />
              ))}
              {filters.tools.map((v) => (
                <TagPill
                  key={`tool-${v}`}
                  label={`Tool: ${v}`}
                  selected
                  onRemove={() => updateFilters({ tools: filters.tools.filter((x) => x !== v) })}
                />
              ))}
              {filters.maxTime !== null && (
                <TagPill
                  label={`≤ ${filters.maxTime} min`}
                  selected
                  onRemove={() => updateFilters({ maxTime: null })}
                />
              )}
              {filters.q.trim() && (
                <TagPill
                  label={`"${filters.q.trim()}"`}
                  selected
                  onRemove={() => updateFilters({ q: "" })}
                />
              )}
            </div>
          )}

          {loading ? (
            <RecipeGridSkeleton />
          ) : error ? (
            <ErrorBanner message={error} onRetry={retry} />
          ) : recipes.length === 0 ? (
            active ? (
              <EmptyState
                icon="🔍"
                title="No matches"
                message="Try fewer filters, or clear them to see everything."
                action={
                  <Button variant="secondary" onClick={clearFilters}>
                    Clear filters
                  </Button>
                }
              />
            ) : (
              <EmptyState
                title="Your cookbook is empty"
                message="Add your first recipe to get started."
                action={
                  <Link to="/add" style={{ textDecoration: "none" }}>
                    <Button>Add your first recipe</Button>
                  </Link>
                }
              />
            )
          ) : (
            <>
              <RecipeCardGrid recipes={recipes} />
              {totalPages > 1 && (
                <nav
                  aria-label="Pagination"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "var(--space-4)",
                    marginTop: "var(--space-6)",
                  }}
                >
                  <Button
                    variant="secondary"
                    disabled={page === 0}
                    onClick={() => setPage((p) => Math.max(0, p - 1))}
                  >
                    Previous
                  </Button>
                  <span style={{ color: "var(--color-text-secondary)" }}>
                    Page {page + 1} of {totalPages}
                  </span>
                  <Button
                    variant="secondary"
                    disabled={page + 1 >= totalPages}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    Next
                  </Button>
                </nav>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
