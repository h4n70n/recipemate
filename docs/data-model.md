This document describes the PostgreSQL database schema for RecipeMate, including all tables, columns, constraints, foreign-key relationships, and the indexing strategy.

## Tables

### users

Stores authenticated user accounts, provisioned lazily on first login.

```sql
id              UUID        PRIMARY KEY
email           TEXT        UNIQUE NOT NULL
cognito_sub     TEXT        UNIQUE NOT NULL  -- Cognito User Pool subject
created_at      TIMESTAMP   NOT NULL
```

No FK dependencies. `cognito_sub` maps to the Cognito JWT `sub` claim and is set on first authenticated request.

### recipes

Core recipe record. Each recipe belongs to one user.

```sql
id                UUID        PRIMARY KEY
user_id           UUID        NOT NULL  REFERENCES users(id)
title             TEXT        NOT NULL
description       TEXT
prep_time_min     INTEGER
cook_time_min     INTEGER
total_time_min    INTEGER
servings          INTEGER
origin            TEXT        CHECK (origin IN ('instagram','web','cookbook','manual','ios_share'))
source_url        TEXT
source_citation   TEXT
image_s3_key      TEXT        -- original source image
thumbnail_s3_key  TEXT        -- generated thumbnail
extraction_status TEXT        CHECK (extraction_status IN ('pending','processing','complete','failed'))
cook_count        INTEGER     DEFAULT 0
avg_rating        NUMERIC(3,2)
created_at        TIMESTAMP   NOT NULL
updated_at        TIMESTAMP   NOT NULL
deleted_at        TIMESTAMP                 -- NULL for active recipes (soft delete)
```

FK: `user_id` → `users(id)`. `deleted_at` is NULL for active recipes.

### ingredients

Individual ingredients for a recipe, ordered by `sort_order`.

```sql
id           UUID     PRIMARY KEY
recipe_id    UUID     NOT NULL  REFERENCES recipes(id)
name         TEXT     NOT NULL
quantity     NUMERIC
unit         TEXT
preparation  TEXT     -- e.g. "finely chopped"
sort_order   INTEGER  NOT NULL
```

FK: `recipe_id` → `recipes(id)`.

### tools

Equipment required by a recipe.

```sql
id           UUID     PRIMARY KEY
recipe_id    UUID     NOT NULL  REFERENCES recipes(id)
name         TEXT     NOT NULL
sort_order   INTEGER  NOT NULL
```

FK: `recipe_id` → `recipes(id)`.

### instructions

Ordered steps for a recipe.

```sql
id           UUID     PRIMARY KEY
recipe_id    UUID     NOT NULL  REFERENCES recipes(id)
step_number  INTEGER  NOT NULL
body         TEXT     NOT NULL
```

FK: `recipe_id` → `recipes(id)`. `step_number` starts at 1 and is unique per recipe.

### tags

Global tag vocabulary shared across all users; created on first use.

```sql
id    UUID  PRIMARY KEY
name  TEXT  UNIQUE NOT NULL
```

### recipe_tags

Many-to-many association between recipes and tags.

```sql
recipe_id  UUID  NOT NULL  REFERENCES recipes(id)
tag_id     UUID  NOT NULL  REFERENCES tags(id)
PRIMARY KEY (recipe_id, tag_id)
```

FKs: `recipe_id` → `recipes(id)`, `tag_id` → `tags(id)`. The composite PK prevents duplicate tag associations.

### cook_logs

Records each time a user cooked a recipe, with optional notes, rating, and photo.

```sql
id            UUID      PRIMARY KEY
recipe_id     UUID      NOT NULL  REFERENCES recipes(id)
user_id       UUID      NOT NULL  REFERENCES users(id)
cooked_at     DATE      NOT NULL
notes         TEXT
rating        INTEGER   CHECK (rating BETWEEN 1 AND 5)
photo_s3_key  TEXT
created_at    TIMESTAMP NOT NULL
```

FKs: `recipe_id` → `recipes(id)`, `user_id` → `users(id)`. `rating` and `notes` are optional. Unrated entries are excluded from `avg_rating` calculations.

## Computed / Denormalized Fields

The `recipes` table carries two columns maintained by the application layer after every cook log mutation:

- `cook_count` (INTEGER DEFAULT 0): total cook log entries for the recipe.
- `avg_rating` (NUMERIC(3,2)): mean of non-NULL `rating` values in `cook_logs` for that recipe. NULL when no cook has been rated.

These are denormalized so browse and search endpoints can sort by `cook_count` and `avg_rating` without a GROUP BY join on every request.

## Indexing Strategy

Migration `006_add_indexes.py` adds:

| Table | Index | Column(s) | Purpose |
| --- | --- | --- | --- |
| recipes | idx_recipes_user_id | user_id | Scopes all recipe queries to the current user |
| ingredients | idx_ingredients_recipe_id | recipe_id | JOIN performance when loading recipe detail |
| cook_logs | idx_cook_logs_recipe_id | recipe_id | Fast cook log lookups per recipe |
| recipes | idx_recipes_deleted_at | deleted_at | Efficiently excludes soft-deleted rows |
