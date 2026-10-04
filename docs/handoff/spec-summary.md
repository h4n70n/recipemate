# Spec Summary

A condensed read of the RecipeMate spec. The full, authoritative spec lives in
`.kiro/specs/recipemate/` (`requirements.md`, `design.md`, `tasks.md`). Read those
before making product decisions; this page is orientation only.

## What RecipeMate is

A Python/Flask API with a React web client that lets home cooks build a searchable
digital cookbook from photos and screenshots of recipes (Instagram, magazines,
cookbooks, etc.). An uploaded image is run through GPT-4o vision to extract
structured recipe data asynchronously. Users search by natural language or
structured filters and keep a log of every time they cook a dish.

## The four product epics (requirements.md)

- **Epic 1 — Adding Recipes.** Upload a photo/screenshot; async extraction returns
  title, ingredients (name/quantity/unit), tools, ordered instructions, and
  timings. User reviews and edits before saving. Manual entry supported (no photo).
  Recipes carry an **origin** (`instagram`, `web`, `cookbook`, `manual`,
  `ios_share`), an optional source URL/citation, tags, and a permanently stored
  source image. iOS share-sheet capture is a goal (future).
  - Accepts JPEG, PNG, HEIC, PDF up to 20MB. Extraction is async: upload returns
    immediately, user is notified on completion.

- **Epic 2 — LLM-Based Search.** Natural-language queries ("I have chicken thighs,
  garlic, and a lemon") over the user's **own** recipes, with a match explanation
  per result. Partial-ingredient matches surface. Target: < 5s for ≤ 500 recipes.

- **Epic 3 — Heuristic / Filter Search.** Browse/filter by ingredient, total cook
  time, tags, tools, and title text; combinable (AND). Sort by newest, oldest,
  most-cooked, highest-rated. Filter state preserved in the URL. Target: < 500ms
  for ≤ 500 recipes.

- **Epic 4 — Cook Logs.** Log each cook with a required date; optional notes,
  photo, and 1–5 star rating. History shown reverse-chronologically. Cook count
  and average rating (unrated cooks excluded) update in real time and show on the
  recipe card.

### Backlog (explicitly deferred)

- Filter/search cook logs ("cooked more than 3 times").
- LLM search suggesting recipes not yet in the cookbook.
- URL import — paste a recipe URL and auto-scrape/extract.

## Architecture in one breath

Flask API behind API Gateway + ECS Fargate; PostgreSQL (RDS) for data; Redis
(ElastiCache) for upload metadata and LLM-search result caching; S3 + CloudFront
for images; SQS + Lambda for async GPT-4o extraction (SNS→APNs for the future iOS
push path); Amazon Cognito for auth; all infra as AWS CDK (Python). The React web
client deploys to S3 behind CloudFront. See `docs/architecture.md` and
`docs/data-model.md` for the real detail, `docs/api.md` for the endpoint contract.

## How the spec is organized

- `requirements.md` — epics, user stories (`REQ-x.y`), acceptance criteria, backlog.
- `design.md` — technical design.
- `tasks.md` — dependency-ordered implementation tasks (`TASK-x.y`) with a
  definition of done each, and milestone tables. **This is where completion is
  tracked** — keep the checkboxes current.

The spec files support `#[[file:...]]` references, so API/data-model docs can be
pulled into spec context directly.
