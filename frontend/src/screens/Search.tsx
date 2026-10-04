import { useCallback, useEffect, useState } from "react";
import {
  llmSearch,
  search,
  type LlmRecipeCard,
  type RecipeCard as RecipeCardData,
  type SearchParams,
  type SearchSort,
} from "../api/client";
import { Button } from "../components/ui/Button";
import { EmptyState, ErrorBanner } from "../components/ui/states";
import { RecipeCardGrid, RecipeGrid, RecipeGridSkeleton } from "../components/ui/RecipeGrid";
import { LlmRecipeCardView } from "../components/ui/RecipeCard";
import { ChipInput, SortDropdown, TimeStepper } from "../components/ui/filters";
import { inputStyle } from "../components/ui/styles";
import { isAbort } from "../components/ui/errors";
import { useApiErrorHandler } from "../components/ui/useApiErrorHandler";

type Mode = "filter" | "llm";

const PAGE_SIZE = 20;

/**
 * Search (web-ui-spec §5.3). One screen, two modes:
 *   - Filter search — `GET /search` with ingredient chips (AND), max_time,
 *     title `q`, and a sort dropdown. (Tag/tool chips are available too.)
 *   - LLM search — `POST /search/llm` with a free-text query; results reuse
 *     the recipe card plus a `match_explanation` line, under an "AI results"
 *     marker, noting it may fall back to filter search.
 */
export function SearchScreen() {
  const [mode, setMode] = useState<Mode>("filter");

  return (
    <section>
      <h1>Search</h1>
      <div role="tablist" aria-label="Search mode" style={{ display: "flex", gap: "var(--space-2)", marginBottom: "var(--space-6)" }}>
        <Button
          role="tab"
          aria-selected={mode === "filter"}
          variant={mode === "filter" ? "primary" : "secondary"}
          onClick={() => setMode("filter")}
        >
          Filters
        </Button>
        <Button
          role="tab"
          aria-selected={mode === "llm"}
          variant={mode === "llm" ? "primary" : "secondary"}
          onClick={() => setMode("llm")}
        >
          Ask (AI)
        </Button>
      </div>

      {mode === "filter" ? <FilterSearch /> : <LlmSearch />}
    </section>
  );
}

function FilterSearch() {
  const handleError = useApiErrorHandler();

  const [ingredients, setIngredients] = useState<string[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [maxTime, setMaxTime] = useState<number | null>(null);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SearchSort>("newest");

  const [recipes, setRecipes] = useState<RecipeCardData[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    (signal: AbortSignal) => {
      setLoading(true);
      setError(null);
      const params: SearchParams = {
        ingredient: ingredients,
        tag: tags,
        tool: tools,
        max_time: maxTime ?? undefined,
        q: q.trim() || undefined,
        sort,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      };
      search(params, signal)
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
    [ingredients, tags, tools, maxTime, q, sort, page, handleError],
  );

  useEffect(() => {
    const controller = new AbortController();
    run(controller.signal);
    return () => controller.abort();
  }, [run]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const resetPage = () => setPage(0);

  return (
    <div className="rm-library-layout">
      <aside
        aria-label="Search filters"
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
          values={ingredients}
          onChange={(v) => {
            setIngredients(v);
            resetPage();
          }}
        />
        <ChipInput
          label="Tags"
          placeholder="e.g. Italian"
          values={tags}
          onChange={(v) => {
            setTags(v);
            resetPage();
          }}
        />
        <ChipInput
          label="Tools"
          placeholder="e.g. cast iron"
          values={tools}
          onChange={(v) => {
            setTools(v);
            resetPage();
          }}
        />
        <TimeStepper
          value={maxTime}
          onChange={(v) => {
            setMaxTime(v);
            resetPage();
          }}
        />
        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)", fontWeight: 600, fontSize: "0.875rem" }}>
          Title contains
          <input
            value={q}
            placeholder="e.g. pasta"
            onChange={(e) => {
              setQ(e.target.value);
              resetPage();
            }}
            style={{ ...inputStyle, fontWeight: 400 }}
          />
        </label>
        <SortDropdown
          value={sort}
          onChange={(v) => {
            setSort(v);
            resetPage();
          }}
        />
      </aside>

      <div>
        {loading ? (
          <RecipeGridSkeleton />
        ) : error ? (
          <ErrorBanner message={error} onRetry={() => run(new AbortController().signal)} />
        ) : recipes.length === 0 ? (
          <EmptyState icon="🔍" title="No matches" message="Try fewer filters." />
        ) : (
          <>
            <RecipeCardGrid recipes={recipes} />
            {totalPages > 1 && (
              <nav
                aria-label="Pagination"
                style={{ display: "flex", justifyContent: "center", gap: "var(--space-4)", marginTop: "var(--space-6)" }}
              >
                <Button variant="secondary" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
                  Previous
                </Button>
                <span style={{ color: "var(--color-text-secondary)" }}>
                  Page {page + 1} of {totalPages}
                </span>
                <Button variant="secondary" disabled={page + 1 >= totalPages} onClick={() => setPage((p) => p + 1)}>
                  Next
                </Button>
              </nav>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function LlmSearch() {
  const handleError = useApiErrorHandler();

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<LlmRecipeCard[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runQuery = useCallback(async () => {
    const trimmed = query.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    try {
      const res = await llmSearch(trimmed);
      setResults(res.recipes);
    } catch (err) {
      setError(handleError(err));
    } finally {
      setLoading(false);
    }
  }, [query, handleError]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    void runQuery();
  };

  return (
    <div style={{ maxWidth: 820 }}>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
        <label htmlFor="llm-query" style={{ fontWeight: 600 }}>
          What do you feel like cooking?
        </label>
        <textarea
          id="llm-query"
          value={query}
          rows={3}
          placeholder="I have chicken thighs, garlic, and a lemon…"
          onChange={(e) => setQuery(e.target.value)}
          style={{ ...inputStyle, fontSize: "1.0625rem", resize: "vertical" }}
        />
        <div>
          <Button type="submit" loading={loading} disabled={!query.trim()}>
            Ask
          </Button>
        </div>
        <p style={{ color: "var(--color-text-secondary)", fontSize: "0.8125rem", margin: 0 }}>
          Results are AI-matched and may fall back to a regular filter search.
        </p>
      </form>

      <div style={{ marginTop: "var(--space-6)" }}>
        {loading ? (
          <RecipeGridSkeleton count={4} />
        ) : error ? (
          <ErrorBanner message={error} onRetry={() => void runQuery()} />
        ) : results === null ? (
          <EmptyState icon="🤖" title="Ask away" message="Describe what you have or want, and we'll find matching recipes." />
        ) : results.length === 0 ? (
          <EmptyState icon="🔍" title="No matches" message="Try describing it differently, or use the filter search." />
        ) : (
          <>
            <p
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "var(--space-2)",
                color: "var(--color-primary)",
                fontWeight: 600,
                marginTop: 0,
              }}
            >
              <span aria-hidden>✨</span> AI results
            </p>
            <RecipeGrid>
              {results.map((recipe) => (
                <LlmRecipeCardView key={recipe.id} recipe={recipe} />
              ))}
            </RecipeGrid>
          </>
        )}
      </div>
    </div>
  );
}
