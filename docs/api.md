This document is the full REST API reference for RecipeMate. Base URL: `https://api.recipemate.app/v1`.

## Authentication

All endpoints require `Authorization: Bearer <JWT>`. Tokens are issued by Amazon Cognito. Example header:

```
Authorization: Bearer eyJhbGciOiJSUzI1NiJ9...
```

## Error Codes

| Code | Meaning |
| --- | --- |
| 200 | OK |
| 201 | Created |
| 204 | No Content |
| 400 | Bad Request — validation error; body contains `{"error": "..."}` |
| 401 | Unauthorized — missing or invalid JWT |
| 403 | Forbidden — authenticated but not the resource owner |
| 404 | Not Found |
| 422 | Unprocessable Entity — valid request that fails business rules |
| 500 | Internal Server Error |

## Recipes

### GET /recipes

Auth required. List the authenticated user's recipes with pagination.

Query params:

- `limit` (integer, default 20, max 100)
- `offset` (integer, default 0)

Response: `{"recipes": [...], "total": int}` — each item: `id`, `title`, `thumbnail_url`, `total_time_min`, `cook_count`, `avg_rating`, `tags`, `created_at`.

### POST /recipes

Auth required. Create a recipe (manual entry, no image required).

Request body (JSON):

- `title` (string, required)
- `description` (string)
- `prep_time_min` (int)
- `cook_time_min` (int)
- `total_time_min` (int)
- `servings` (int)
- `origin` (one of: `instagram` | `web` | `cookbook` | `manual` | `ios_share`)
- `source_url` (string)
- `source_citation` (string)

Response: full recipe object (201).

### GET /recipes/{id}

Auth required. Get full recipe with ingredients, tools, instructions, tags, and `cook_summary` `{cook_count, avg_rating}`.

### PATCH /recipes/{id}

Auth required. Partial update. Same fields as POST, all optional.

Response: updated full recipe object.

### DELETE /recipes/{id}

Auth required. Soft-delete (sets `deleted_at`). Returns 204.

## Image Upload

### POST /recipes/upload

Auth required. Generate a presigned S3 PUT URL.

Request body: `{"filename": "photo.jpg", "content_type": "image/jpeg"}`

Response: `{"upload_url": "https://...", "s3_key": "uploads/uuid/photo.jpg"}` — URL expires in 15 min.

Supported types: `image/jpeg`, `image/png`, `image/heic`, `application/pdf`. Max 20 MB.

### POST /recipes/extract

Auth required. Trigger async extraction for an already-uploaded image.

Request body: `{"s3_key": "uploads/uuid/photo.jpg", "origin": "instagram", "source_url": "optional"}`

Response: `{"recipe_id": "<uuid>", "extraction_status": "pending"}` (201).

### GET /recipes/{id}/extraction-status

Auth required. Poll extraction progress.

Response: `{"extraction_status": "pending|processing|complete|failed"}`

## Cook Logs

### GET /recipes/{id}/cooks

Auth required. List cook logs reverse-chronological.

Response: `{"cooks": [{id, cooked_at, notes, rating, photo_url, created_at}]}`

### POST /recipes/{id}/cooks

Auth required. Add a cook log.

Request body:

- `cooked_at` (date `YYYY-MM-DD`, required)
- `notes` (string)
- `rating` (int 1-5)
- `photo_s3_key` (string)

Response: cook log object (201). Updates `cook_count` and `avg_rating` on parent recipe.

### PATCH /recipes/{id}/cooks/{cook_id}

Auth required. Update notes, rating, or photo.

Response: updated cook log object.

### DELETE /recipes/{id}/cooks/{cook_id}

Auth required. Delete cook log. Updates `cook_count` and `avg_rating`. Returns 204.

## Search

### GET /search

Auth required. Heuristic filter search. All params optional and combinable (AND logic).

| Param | Type | Example | Notes |
| --- | --- | --- | --- |
| `ingredient` | string (repeatable) | `?ingredient=chicken&ingredient=garlic` | Recipes must contain ALL listed ingredients |
| `max_time` | integer (minutes) | `?max_time=30` | Filters by `total_time_min` |
| `tag` | string (repeatable) | `?tag=Italian&tag=vegetarian` | Recipes must have ALL listed tags |
| `tool` | string (repeatable) | `?tool=cast+iron` | Recipes must require ALL listed tools |
| `q` | string | `?q=pasta` | Case-insensitive title text search |
| `sort` | string | `?sort=newest` | `newest` \| `oldest` \| `most_cooked` \| `highest_rated` |
| `limit` | integer | `?limit=20` | Default 20, max 100 |
| `offset` | integer | `?offset=0` | For pagination |

Response: `{"recipes": [...], "total": int}` — same recipe card shape as GET /recipes.

### POST /search/llm

Auth required. Natural-language search via GPT-4. Results cached 5 min in Redis.

Request body: `{"query": "I have chicken thighs, garlic, and a lemon"}`

Response: `{"recipes": [{id, title, thumbnail_url, match_explanation, ...}]}`

Falls back to heuristic search if LLM call fails.

## System

### GET /health

No auth required. Liveness check.

Response: `{"status": "ok"}` (200).
