# MTA Applications Engine

A metadata-only discovery and indexing API for MTA Apps Center. It indexes application listings from public catalogues and developer-controlled web sources across Android, Linux, Windows, macOS, iOS and the web.

**Policy:** this service stores catalogue metadata and links to source/product pages. It does not download, mirror, proxy or host APK, EXE, MSI, DMG, IPA or other installer binaries.

## Supported catalogues

| Source | Platform | Source type |
|---|---|---|
| F-Droid | Android | Public F-Droid repository index |
| Flathub | Linux | Flathub AppStream catalogue |
| WinGet community index | Windows | winget.run search API (third-party index) |
| Homebrew Cask | macOS | Official Homebrew Cask JSON feed |
| Apple Search API | iOS | Apple public Search API, country configurable |
| MTA curated web catalogue | Web | Maintained in `web_apps.json` |

Connectors fail independently. A temporary outage at one source should not stop ingestion from the others. Results are deduplicated by source key and external identifier, and non-HTTPS source links are rejected.

## API

- `GET /health` — service health.
- `GET /api/v1/apps` — list/search indexed applications.
- `GET /api/v1/apps?q=browser&platform=windows&limit=50&offset=0` — filtered results.
- `GET /api/v1/catalog-stats` — totals by platform and source.
- `POST /api/v1/ingest/catalogs` — ingest all configured catalogues (admin token required).
- `POST /api/v1/ingest/fdroid` — ingest F-Droid only (admin token required).

The ingestion endpoints require the `X-Admin-Token` header. Keep the token server-side; never place it in browser JavaScript or a public website bundle.

Example browser-side catalogue request:

```javascript
const response = await fetch("https://YOUR-API-HOST/api/v1/apps?platform=android&limit=24");
if (!response.ok) throw new Error("Catalogue request failed");
const apps = await response.json();
```

For public MTA Apps Center pages, call only the read-only GET endpoints from the browser. Configure `CORS_ORIGINS` to the exact production website origins. Do not expose admin ingestion endpoints or the admin token to visitors.

## Configuration

Copy `.env.example` to `.env` and set strong unique secrets. Production should use PostgreSQL; SQLite is intended for local development and tests.

Required production variables:
- `DATABASE_URL` — PostgreSQL connection string using `postgresql+psycopg://...`.
- `ADMIN_API_TOKEN` — long random secret used only by trusted server-side automation.
- `CORS_ORIGINS` — comma-separated exact origins allowed to read the API.

Optional catalogue controls:
- `APPLE_COUNTRY`, `APPLE_SEARCH_TERMS`, `APPLE_LIMIT`
- `WINGET_SEARCH_TERMS`, `WINGET_LIMIT`
- `FLATHUB_MAX_APPS`, `CATALOG_CONCURRENCY`

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app:app --reload
```

For Docker Compose, set `POSTGRES_PASSWORD` and `ADMIN_API_TOKEN` in the environment, then run `docker compose up --build -d`.

## Automated ingestion

`.github/workflows/catalogue-sync.yml` is configured for a daily UTC run and manual dispatch. Add a GitHub Actions repository secret named `DATABASE_URL` containing the production PostgreSQL connection string. The workflow skips safely when that secret is absent.

The scheduled workflow becomes active only after it is merged into the repository's default branch. GitHub also requires a workflow containing `workflow_dispatch` to exist on the default branch before the manual Run workflow control is available; after that, the branch selector can target this feature branch.

## Development checks

```bash
pip install -r requirements-dev.txt
pytest -q
python -m py_compile app.py catalog_sources.py sync_catalogs.py
```

The source integrations depend on upstream public APIs and their current availability/terms. Catalogue coverage is bounded by each upstream's search and pagination limits; a single import should not be interpreted as a complete inventory of every application worldwide.
