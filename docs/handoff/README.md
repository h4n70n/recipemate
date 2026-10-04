# RecipeMate — Handoff Package

This folder is the entry point for anyone picking up RecipeMate. It was assembled
for the transfer to **kirocrew** and **mobie** and captures the project's spec,
current build status, git state, and recommended next steps as of **2026-10-04**.

## Start here

Read these in order:

1. **[project-status.md](project-status.md)** — what's done, what's in flight, and
   what remains, broken down by phase and task.
2. **[spec-summary.md](spec-summary.md)** — the product/engineering spec in brief,
   with pointers to the authoritative spec files.
3. **[git-state.md](git-state.md)** — branch layout, what's merged vs. unpushed,
   and how to get to a clean working state.
4. **[next-steps.md](next-steps.md)** — the recommended pickup sequence and the
   decisions already made that constrain the remaining work.

## Authoritative sources (do not duplicate — these stay the source of truth)

| Topic | Location |
|-------|----------|
| Requirements (epics, user stories, acceptance criteria) | `.kiro/specs/recipemate/requirements.md` |
| Design | `.kiro/specs/recipemate/design.md` |
| Implementation tasks + checkboxes | `.kiro/specs/recipemate/tasks.md` |
| API contract | `docs/api.md` |
| Architecture | `docs/architecture.md` |
| Data model | `docs/data-model.md` |
| Configuration reference | `docs/configuration.md` |
| Local development | `docs/local-development.md` |
| Operational readiness (Phase 8 detail) | `docs/operational-readiness.md` |
| Web UI spec | `docs/web-ui-spec.md` |

The documents in this folder are a point-in-time **snapshot and index**. When the
spec and these notes disagree, the files under `.kiro/specs/recipemate/` win —
update the task checkboxes there as work lands.

## TL;DR

RecipeMate is a Flask API + React web client that turns recipe photos into a
searchable digital cookbook (async GPT-4o extraction), with natural-language and
filter search, and per-cook logs. The backend API, web frontend, and most of the
AWS CDK infrastructure are built. The remaining work is **first-production-deploy
hardening (Phase 8)** and the **iOS app (Phases 5 & 7)**.
