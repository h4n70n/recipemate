# Git State — 2026-10-04

Captured at handoff. Re-run the commands below to refresh; this is a point-in-time
snapshot.

- **Remote:** `origin` → `git@github.com:h4n70n/recipemate.git`
- **Current branch:** `main`
- **`main` vs `origin/main`:** 3 commits **ahead**, 0 behind.

## Unpushed commits on `main`

These three Phase-8 commits are on local `main` but **not yet on `origin/main`**:

```
8f77529  feat: load Secrets Manager keys into the API at runtime (TASK-8.3)
bff217c  feat: add GitHub-OIDC frontend deploy role to WebStack
89add9f  feat: standardize region on us-east-2 and add backend OIDC deploy role
```

> As part of preparing this handoff these were pushed to `origin/main` (see the
> handoff commit). If you are reading this from a fresh clone, `origin/main`
> already contains them. If you still see them as "ahead," run `git push`.

## Untracked files at snapshot time

Working-tree files that were not yet committed when the handoff started:

- `.agents/tasks/plan.md`, `.agents/tasks/review.md`, `.agents/tasks/review.json`
  — plan + review artifacts for the WebStack OIDC deploy-role change.
- `docs/operational-readiness.md` — Phase 8 companion detail (referenced by
  `tasks.md`).
- `docs/handoff/` — this handoff package.

The handoff commit adds `docs/handoff/` and `docs/operational-readiness.md`. The
`.agents/tasks/` artifacts are working notes; keep or drop them per your
convention (they are not required by the app).

## Branch layout

Long-lived component branches exist both locally and on the remote:

```
app, chore/project-config, docs, infra, lambda, migrations
```

Feature branches on the remote (most already merged into `main` via PR):

```
origin/feat/async-extraction   origin/feat/cook-logs    origin/feat/image-upload
origin/feat/llm-search         origin/feat/recipe-crud  origin/feat/search
origin/feat/tags               origin/feat/web-frontend
```

Recent merge history shows features landed via pull requests from the `h4n70n`
fork (PRs #9–#14). The last merge on `origin/main` before the unpushed Phase-8
work was **#14 feat/web-frontend** (`52041a1`).

## How to get to a clean, current state

```bash
git -C recipemate fetch --all --prune
git -C recipemate checkout main
git -C recipemate pull --ff-only          # fast-forward to origin/main
git -C recipemate status                   # expect: clean, up to date
```

## Refresh these facts

```bash
git -C recipemate remote -v
git -C recipemate branch --show-current
git -C recipemate rev-list --left-right --count origin/main...main
git -C recipemate log --oneline origin/main..main    # unpushed commits
git -C recipemate status --short                      # untracked/dirty files
```
