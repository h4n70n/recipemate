import type { ReactNode } from "react";
import type { RecipeCard as RecipeCardData } from "../../api/client";
import { RecipeCard, RecipeCardSkeleton } from "./RecipeCard";

/** Responsive recipe grid wrapper (4/2/1-up via the `.rm-recipe-grid` class). */
export function RecipeGrid({ children }: { children: ReactNode }) {
  return <div className="rm-recipe-grid">{children}</div>;
}

/** A grid of standard recipe cards. */
export function RecipeCardGrid({ recipes }: { recipes: RecipeCardData[] }) {
  return (
    <RecipeGrid>
      {recipes.map((recipe) => (
        <RecipeCard key={recipe.id} recipe={recipe} />
      ))}
    </RecipeGrid>
  );
}

/** A grid of skeleton cards for the loading state. */
export function RecipeGridSkeleton({ count = 8 }: { count?: number }) {
  return (
    <RecipeGrid>
      {Array.from({ length: count }, (_, i) => (
        <RecipeCardSkeleton key={i} />
      ))}
    </RecipeGrid>
  );
}
