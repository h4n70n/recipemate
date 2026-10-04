# RecipeMate Web UI Spec

A design brief for the RecipeMate web client. Hand this to Figma (or a designer / Figma AI) as the starting point for screens, components, and flows. It is grounded in the live REST API (`docs/api.md`) and data model (`docs/data-model.md`).

## 1. Product in one line

A searchable digital cookbook built from recipe photos. Users upload an image, GPT-4o extracts structured recipe data, and they search, browse, and keep a personal cook log of every dish they make.

## 2. Target user & tone

Home cooks managing a personal recipe collection. The UI should feel warm, uncluttered, and photo-forward — recipes are visual. Prioritize fast search and quick capture (snap a photo, done) over dense data entry.

## 3. Design tokens (starting point)

Figma should turn these into styles/variables. Adjust during design.

| Token | Suggested value | Notes |
| --- | --- | --- |
| Primary | `#E8562A` (warm terracotta) | CTAs, active states |
| Surface | `#FFFFFF` / `#FAF7F2` (cream) | Cards on cream background |
| Text primary | `#1F1B16` | |
| Text secondary | `#6B6258` | Metadata, captions |
| Success | `#2E7D32` | Extraction complete |
| Warning | `#C77700` | Extraction processing |
| Error | `#C62828` | Extraction failed, validation |
| Radius | 12px cards, 8px inputs, pill for tags | |
| Font | Display serif for titles, sans for body | e.g. Fraunces + Inter |
| Rating | 5-star, filled primary | 1–5 integer |

Define spacing on a 4px grid (4/8/12/16/24/32/48). Build responsive layouts for mobile (375), tablet (768), and desktop (1280).

## 4. Information architecture

```
Auth (Cognito hosted / redirect)
└── App shell (top nav: Library · Search · Add +, user menu)
    ├── Library            (GET /recipes)
    ├── Search             (GET /search, POST /search/llm)
    ├── Add Recipe
    │   ├── Upload photo   (POST /recipes/upload → POST /recipes/extract → poll)
    │   └── Manual entry   (POST /recipes)
    ├── Recipe Detail      (GET /recipes/{id})
    │   ├── Edit           (PATCH /recipes/{id})
    │   └── Cook Log       (GET/POST/PATCH/DELETE /recipes/{id}/cooks)
    └── User menu          (sign out)
```

## 5. Screens

### 5.1 Sign in
- Single branded screen with a "Sign in" button that hands off to Amazon Cognito (hosted UI / OAuth redirect). No local password form.
- States: default, redirecting (spinner), auth error.

### 5.2 Library (recipe grid)
Source: `GET /recipes` → `{recipes[], total}`. Card fields: `thumbnail_url`, `title`, `total_time_min`, `cook_count`, `avg_rating`, `tags`.
- Responsive card grid (4-up desktop, 2-up tablet, 1-up mobile).
- Each card: photo (16:9 or 4:3), title, time badge ("30 min"), star rating + cook count ("★ 4.5 · cooked 3×"), up to 2–3 tag pills with "+N".
- Pagination or infinite scroll (`limit`/`offset`, default 20, max 100).
- Prominent "Add recipe +" entry point.
- Empty state: friendly illustration + "Add your first recipe" CTA.
- Loading: skeleton cards. Error: retry banner.

### 5.3 Search
Two modes in one screen, toggle or unified bar:
- **Filter search** — `GET /search`. Controls:
  - Natural-language-ish toggle to the LLM mode.
  - Ingredient chips (repeatable, AND logic): "chicken" "garlic".
  - Max total time slider/stepper (`max_time`, minutes).
  - Tag multi-select (AND), Tool multi-select (AND).
  - Title text field (`q`).
  - Sort dropdown: Newest · Oldest · Most cooked · Highest rated.
- **LLM search** — `POST /search/llm` with `{query}`. Big single text input: "I have chicken thighs, garlic, and a lemon." Results reuse the recipe card but add a `match_explanation` line ("Uses chicken + garlic you have"). Show a subtle "AI results" marker and note results may fall back to filter search.
- Results = same card grid as Library. Show active filters as removable chips. Empty state: "No matches — try fewer filters."

### 5.4 Add recipe — upload photo (primary flow)
Three API steps; present as one guided flow:
1. **Pick/drop image** — accept `image/jpeg`, `image/png`, `image/heic`, `application/pdf`; max 20 MB. Call `POST /recipes/upload` → get presigned URL + `s3_key`, PUT the file to S3 (show upload progress).
2. **Add source (optional)** — `origin` select (`instagram` · `web` · `cookbook` · `manual` · `ios_share`), optional `source_url`. Call `POST /recipes/extract` → `{recipe_id, extraction_status: "pending"}`.
3. **Extracting** — poll `GET /recipes/{id}/extraction-status`. Show animated progress with the four states: pending, processing, complete, failed.
   - On **complete**: route to Recipe Detail (ideally in edit/review mode so the user can confirm the extracted data).
   - On **failed**: error state with "Try another photo" and "Enter manually" options.

### 5.5 Add recipe — manual entry
Source: `POST /recipes`. Form fields: `title` (required), `description`, `prep_time_min`, `cook_time_min`, `total_time_min`, `servings`, `origin`, `source_url`, `source_citation`.
- Note: the create endpoint takes recipe-level fields only. Ingredients, tools, instructions, and tags are managed on the detail/edit screen after creation (they come back on `GET /recipes/{id}`). Design edit affordances for those lists there.
- Inline validation (400 → `{"error": "..."}`).

### 5.6 Recipe detail
Source: `GET /recipes/{id}` → full recipe: ingredients, tools, instructions, tags, `cook_summary {cook_count, avg_rating}`.
- Hero: source image, title, description, meta row (prep / cook / total time, servings), tag pills, origin + `source_citation` link.
- **Ingredients** list: `quantity` `unit` `name` (`preparation`), ordered by `sort_order`. Consider a servings scaler.
- **Tools** list.
- **Instructions**: numbered steps (`step_number`, `body`).
- **Cook summary** header: avg rating stars + cook count.
- Actions: Edit (`PATCH /recipes/{id}`), Delete (soft delete, `DELETE` → 204, confirm dialog), Log a cook (opens 5.7).

### 5.7 Cook log
Source: `GET/POST/PATCH/DELETE /recipes/{id}/cooks`.
- **List**: reverse-chronological entries, each with `cooked_at`, star `rating`, `notes`, optional `photo_url`.
- **Add/Edit entry** (modal or panel): date picker (`cooked_at`, required), 1–5 star rating, notes textarea, photo upload (reuse presigned-URL upload → `photo_s3_key`).
- Deleting or saving updates the recipe's `cook_count` / `avg_rating` — reflect the new values optimistically or on refetch.
- Empty state: "You haven't cooked this yet — log your first cook."

## 6. Shared components (Figma library)

- **Recipe card** (grid item) + variants: default, loading skeleton, LLM-result (with match explanation).
- **Tag pill** (default / selected / removable).
- **Star rating** (display + interactive input, 1–5).
- **Time badge**.
- **Extraction status indicator** (pending / processing / complete / failed).
- **Image uploader / dropzone** (idle / uploading-with-progress / error).
- **Top nav / app shell** + user menu.
- **Filter controls** (chip input, multi-select, sort dropdown, time stepper).
- **Empty state**, **error banner / retry**, **confirm dialog**, **toast**.
- **Form field** (label, input, inline error).

## 7. Cross-cutting states & rules

- **Auth**: every screen except Sign in requires a session; 401 → bounce to Sign in. 403 → "You don't have access to this recipe."
- **Loading**: skeletons for grids and detail; spinners for actions.
- **Errors**: map API codes — 400 (field validation), 404 (not found screen), 422 (business-rule message), 500 (generic retry).
- **Async extraction**: never block the UI; the recipe exists while extraction is pending/processing.
- **Soft delete**: deleted recipes disappear from Library/Search; no "trash" view required unless you want one.
- **Accessibility**: color contrast ≥ 4.5:1 for text, focus states on all interactive elements, star rating operable by keyboard, alt text on recipe images, labels on all inputs.

## 8. How to use this in Figma

1. Create a **Design tokens** page from section 3 (color/text/effect styles + variables).
2. Build the **component library** (section 6) first — card, tag, rating, status, uploader.
3. Lay out the **screens** (section 5) at desktop + mobile breakpoints, assembling from components.
4. Wire a **prototype** following the IA (section 4): Sign in → Library → Add (upload flow) → Detail → Cook log, plus Search.

> Note: Figma does not import markdown as artboards. This file is the written brief. Paste sections into Figma AI / FigJam to scaffold frames, or use it as the spec a designer builds against.
