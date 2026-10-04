import { Link } from "react-router-dom";
import type { LlmRecipeCard, RecipeCard as RecipeCardData } from "../../api/client";
import { formatRating } from "./format";
import { TimeBadge } from "./TimeBadge";
import { TagPill } from "./TagPill";
import { SkeletonBlock } from "./states";

const MAX_TAGS = 3;

const cardStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  background: "var(--color-surface)",
  borderRadius: "var(--radius-card)",
  overflow: "hidden",
  boxShadow: "0 1px 3px rgba(31, 27, 22, 0.08)",
  textDecoration: "none",
  color: "inherit",
  height: "100%",
};

const thumbStyle: React.CSSProperties = {
  aspectRatio: "4 / 3",
  width: "100%",
  objectFit: "cover",
  display: "block",
  background: "rgba(31, 27, 22, 0.06)",
};

function Meta({ recipe }: { recipe: RecipeCardData }) {
  const rating = formatRating(recipe.avg_rating);
  const cookCount = recipe.cook_count ?? 0;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--space-3)",
        color: "var(--color-text-secondary)",
        fontSize: "0.8125rem",
      }}
    >
      <TimeBadge minutes={recipe.total_time_min} />
      {rating && (
        <span>
          <span aria-hidden style={{ color: "var(--color-rating)" }}>
            ★
          </span>{" "}
          {rating}
        </span>
      )}
      <span>
        cooked {cookCount}
        {"×"}
      </span>
    </div>
  );
}

function Tags({ tags }: { tags: string[] }) {
  if (tags.length === 0) return null;
  const shown = tags.slice(0, MAX_TAGS);
  const extra = tags.length - shown.length;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-1)" }}>
      {shown.map((tag) => (
        <TagPill key={tag} label={tag} />
      ))}
      {extra > 0 && <TagPill label={`+${extra}`} />}
    </div>
  );
}

/**
 * RecipeCard (web-ui-spec §6) — a grid item linking to the recipe detail.
 * Shows thumbnail, title, time badge, rating + cook count, and up to three
 * tag pills with a "+N" overflow.
 */
export function RecipeCard({ recipe }: { recipe: RecipeCardData }) {
  return (
    <Link to={`/recipes/${recipe.id}`} style={cardStyle}>
      {recipe.thumbnail_url ? (
        <img src={recipe.thumbnail_url} alt={recipe.title} style={thumbStyle} />
      ) : (
        <div style={{ ...thumbStyle, display: "grid", placeItems: "center" }} aria-hidden>
          <span style={{ fontSize: "2rem", opacity: 0.5 }}>🍲</span>
        </div>
      )}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-2)",
          padding: "var(--space-3)",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "1.0625rem" }}>{recipe.title}</h3>
        <Meta recipe={recipe} />
        <Tags tags={recipe.tags} />
      </div>
    </Link>
  );
}

/**
 * LLM-result variant of the recipe card (web-ui-spec §6) — adds the
 * `match_explanation` line beneath the standard card content.
 */
export function LlmRecipeCardView({ recipe }: { recipe: LlmRecipeCard }) {
  return (
    <Link to={`/recipes/${recipe.id}`} style={cardStyle}>
      {recipe.thumbnail_url ? (
        <img src={recipe.thumbnail_url} alt={recipe.title} style={thumbStyle} />
      ) : (
        <div style={{ ...thumbStyle, display: "grid", placeItems: "center" }} aria-hidden>
          <span style={{ fontSize: "2rem", opacity: 0.5 }}>🍲</span>
        </div>
      )}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-2)",
          padding: "var(--space-3)",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "1.0625rem" }}>{recipe.title}</h3>
        {recipe.match_explanation && (
          <p
            style={{
              margin: 0,
              fontSize: "0.875rem",
              color: "var(--color-primary)",
              fontStyle: "italic",
            }}
          >
            {recipe.match_explanation}
          </p>
        )}
        <Meta recipe={recipe} />
        <Tags tags={recipe.tags} />
      </div>
    </Link>
  );
}

/** Loading skeleton variant of the recipe card. */
export function RecipeCardSkeleton() {
  return (
    <div style={cardStyle} aria-hidden>
      <SkeletonBlock height={160} radius="0" />
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-2)",
          padding: "var(--space-3)",
        }}
      >
        <SkeletonBlock height={20} width="70%" />
        <SkeletonBlock height={14} width="50%" />
        <SkeletonBlock height={18} width="60%" radius="var(--radius-pill)" />
      </div>
    </div>
  );
}
