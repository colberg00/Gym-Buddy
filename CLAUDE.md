# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Gym Buddy is a self-hosted workout logger that serves two clients from a single Python container:

1. **An MCP server** (`mcp-server/server.py`) exposing workout-logging tools to Claude over `streamable_http`.
2. **A single-page PWA** (`index.html`) served at `/app` for quick on-phone logging during a workout, backed by a small REST API under `/api/`.

Both clients write to the same Postgres database. The system is designed to run on a home server exposed over Tailscale Funnel, so the MCP side is gated by an OAuth 2.0 + PKCE flow while the `/api/*` routes are left unauthenticated and protected by the network boundary only.

## Commands

The project is Docker-first — there is no local `venv` or test suite.

```bash
# First-time setup: copy .env.example to .env and fill in values
cp .env.example .env

# Build and run both containers (postgres + mcp-server)
docker compose up -d --build

# Tail logs
docker compose logs -f mcp-server

# Rebuild after changing server.py
docker compose up -d --build mcp-server

# Apply schema changes from init.sql — init.sql only runs on an EMPTY pgdata volume
docker compose down -v && docker compose up -d --build

# Open a psql shell against the running DB
docker compose exec postgres psql -U gym -d gymdb
```

Required `.env` variables (see `.env.example`): `POSTGRES_PASSWORD`, `ADMIN_PASSWORD` (used on the OAuth consent page), `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `SERVER_URL` (public funnel URL, used as the OAuth issuer and as an allowed transport host).

The server binds to `127.0.0.1:8000` on the host — expose it externally via Tailscale Funnel, not by changing the port binding.

## Architecture

### One process, three surfaces

`server.py` wires a single Starlette `app` that mounts:

- **Explicit `Route`s** for OAuth (`/oauth/authorize`, `/oauth/token`, plus `/authorize` + `/token` aliases and `/.well-known/oauth-authorization-server`), the PWA (`/app`, `/manifest.json`), and the REST API (`/api/templates`, `/api/workout/{template_id}`, `/api/sessions`).
- **A `Mount("/")` for the FastMCP ASGI app** — this catches everything else and serves the MCP streamable_http endpoint at the root.

`BearerAuthMiddleware` gates the MCP mount behind a bearer token. It lets through anything in `OPEN_PATHS` or under `/api/` unauthenticated; everything else requires a token issued by the OAuth flow. Browser GETs that lack a token are redirected to `/app` so the PWA loads directly.

Tokens and authorization codes live in in-memory dicts (`_tokens`, `_auth_codes`). A restart invalidates all tokens; Claude re-authenticates automatically. Don't add a persistent token store without a reason.

### Data model (`init.sql`)

Six tables plus one view:

- `exercises` — unique by name, auto-created on first log.
- `sessions` — one per workout; `session_date` is a DATE, `notes` is a free-text field.
- `sets` — belongs to a session and exercise, with `set_number` scoped per (session, exercise). Deletions renumber remaining sets.
- `bodyweight` — standalone time series.
- `workout_templates` + `template_exercises` — the Upper/Lower split the PWA loads from. `init.sql` seeds these; if you add exercises to a template, do it via a new migration or by editing `init.sql` and recreating the volume.
- `set_history` view — joins sets/sessions/exercises and computes `e1rm` via the Epley formula (`weight * (1 + reps/30)`, or just `weight` for reps=1). **Always query through this view** rather than recomputing e1RM inline.

### Exercise resolution

`resolve_exercise(cur, name)` is the one place name matching happens: exact case-insensitive match → single-result substring match → create new. Any tool or endpoint that accepts a free-text exercise name should go through this helper so fuzzy-matching stays consistent and new exercises get created implicitly.

### MCP tools vs. REST API

The **MCP tools** (decorated with `@mcp.tool()`) are the surface Claude uses — they cover CRUD on sessions, sets, bodyweight, exercises, plus reads for history, PRs, volume, and the training philosophy. Tool docstrings are the contract Claude sees, so keep them accurate and action-oriented.

The **REST API** is minimal and exists only for the PWA:

- `GET /api/templates` — list `{id, name}`.
- `GET /api/workout/{template_id}` — template exercises with `last_sets` (the most recent session's sets for that exercise) so the UI can prefill defaults.
- `POST /api/sessions` — atomic session create + bulk set insert. Accepts either `exercise_id` (from a template) or a free-text `name` (resolved via `resolve_exercise`).

When a template is used, the PWA tags the session notes with `[template:{id}]` so the source is recoverable later.

### PWA (`index.html`)

Single self-contained file — HTML, CSS, and vanilla JS, no build step. State lives in a `exercises[]` array mirrored to `localStorage` under `gymbuddy_session` / `gymbuddy_index` / `gymbuddy_template`, so an in-progress workout survives reloads and crashes. `save()` is called after every mutation; only `saveWorkout()` → `POST /api/sessions` commits to the DB.

The app is installable as a PWA via `manifest.json` (`start_url: /app`, standalone display) and includes iOS-specific meta tags for home-screen install.

## Conventions

- **Weight is in kilograms** throughout; reps are integers. The schema allows `NUMERIC(6,2)` for weight.
- **`session_date` is an ISO `YYYY-MM-DD` string** in tool arguments; `measured_at` for bodyweight is ISO 8601 with time.
- **Destructive tools** (`delete_set`, `delete_session`, `delete_exercise`) are irreversible and cascade through FK `ON DELETE CASCADE` for sets. Keep the confirmation burden on the caller; don't add a two-step soft-delete.
- **`philosophy.md` is user-editable via `update_training_philosophy`** — the file is bind-mounted into the container at `/data/philosophy.md`. Treat it as data, not code: don't overwrite it from scripts and don't reformat it on read.
- **PRs are computed on the fly** from `set_history` (`get_prs`) — there is no `records` table. If you add a new PR type, extend the `pr_type` switch in `get_prs` rather than denormalizing.
- **Don't break the MCP tool signatures.** Claude's server-side tool schema is derived from the Python signatures + docstrings; renaming a parameter is a breaking change for existing sessions.

## Training philosophy

`philosophy.md` encodes the user's programming preferences (Upper/Lower split in the current schema despite the doc mentioning PPL — the doc is the source of truth for *preferences*, the DB templates are what the UI serves). When generating or suggesting workouts, read it via `get_training_philosophy` and respect its constraints (rep ranges, volume caps, exercise selection criteria) rather than overriding them from general fitness knowledge.
