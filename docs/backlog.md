# RecipeMate Backlog

Non-blocking improvements, deferred decisions, and known limitations captured
during implementation. Items here are not part of the committed task plan in
`.kiro/specs/recipemate/tasks.md`; promote an item into the spec when it is
scheduled for work.

Each item: a short rationale, the current behavior, options considered, and
acceptance criteria so it can be picked up without re-deriving the context.

---

## BL-1 — Per-user tag namespaces

**Status:** Open · **Priority:** Medium · **Area:** Data model / Tags · **Raised:** TASK-2.4

### Context
Tags were implemented in TASK-2.4 against the merged schema, where `tags.name`
has a **global** unique constraint. As a result tags are a single shared pool
keyed by name: the label `Italian` is one row reused across every user and
recipe. `GET /tags` ("tags used by the current user") is therefore *derived*
per request by joining `tags → recipe_tags → recipes` filtered to the caller's
own, non-deleted recipes — the tags table itself is not partitioned by user.

### Current behavior / limitation
- One user creating a tag makes that exact name reusable by everyone (the row
  is shared), though associations are still per recipe, so no user sees another
  user's tag unless they independently apply the same name.
- Case is significant (`Italian` != `italian`) because the global unique key is
  the raw stripped name.
- There is no per-user customization: a user cannot have a private tag that is
  invisible as a name to others, cannot rename a tag for only themselves, and
  cannot have their own casing/spelling of a shared label.
- Tag analytics ("how many of MY recipes use this tag") already work via the
  derived listing, so the shared pool is not currently a correctness problem —
  it is a modeling/namespacing limitation.

### Why it might matter
- Product may want tags to feel personal (a user's own vocabulary), which the
  shared global pool does not provide.
- A shared pool invites accidental collisions and inconsistent casing across
  users, and makes any future "rename tag" or "tag color/metadata" feature
  ambiguous (whose rename wins?).

### Options considered
1. **Add `user_id` to `tags` with a composite unique `(user_id, name)`.**
   Each user gets their own tag rows; `recipe_tags` points at a user-scoped
   tag. Cleanest namespacing; `GET /tags` becomes a direct `WHERE user_id = ?`
   lookup instead of a join-derivation. Requires a migration and a data
   backfill (split the current shared rows per owning user, re-point
   `recipe_tags`).
2. **Keep the global pool but normalize names** (e.g. case-fold on write) to at
   least reduce collisions. Smaller change, does not deliver true per-user
   namespaces — a stopgap, not a fix.
3. **Leave as-is.** Acceptable if product confirms tags are intentionally a
   shared global vocabulary.

Recommended: option 1 if per-user namespaces are desired; otherwise explicitly
accept option 3 and close this item.

### Acceptance criteria (if option 1 is chosen)
- [ ] Decision confirmed with product: tags should be per-user, not a shared pool.
- [ ] Alembic migration adds `tags.user_id` (FK → users.id) and replaces the
      global unique on `name` with a composite unique `(user_id, name)`.
- [ ] Data backfill: existing shared tag rows are split per owning user based on
      current `recipe_tags` associations; `recipe_tags` re-points to the correct
      per-user tag; no recipe loses a tag; no orphaned tag rows remain.
- [ ] `Tag` model and the find-or-create logic in `app/routes/recipes.py`
      scope lookups/creates by `(current_user, name)`.
- [ ] `GET /tags` returns the user's own tags directly (still name-ordered);
      behavior for the caller is unchanged externally.
- [ ] Tag add/remove endpoints behave identically from the client's view;
      cross-user isolation holds (one user's tag set never leaks into another's).
- [ ] `docs/api.md` and `docs/data-model.md` updated to describe per-user tags.
- [ ] Tests: migration/backfill covered; cross-user isolation asserted;
      existing tag tests updated for the new scoping. Full suite green.

### Links
- Introduced in: TASK-2.4 (PR #10, `feat/tags`)
- Touches: `app/models/recipe.py` (Tag/RecipeTag), `app/routes/recipes.py`
  (`_find_or_create_tag`, add/remove), `app/routes/tags.py` (`GET /tags`),
  `migrations/`, `docs/api.md`, `docs/data-model.md`
