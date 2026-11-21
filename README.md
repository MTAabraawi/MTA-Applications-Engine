# MTA Applications Engine

A metadata-only discovery and indexing API for MTA Apps Center. It indexes application listings from public catalogues and developer-controlled web sources across Android, Linux, Windows, macOS, iOS and the web.

**Policy:** this service stores catalogue metadata and links to source/product pages. It does not download, mirror, proxy or host APK, EXE, MSI, DMG, IPA or other installer binaries.

## Supported catalogues

| Source | Platform | Source type |
|---|---|---|
| F-Droid | Android | Public F-Droid repository index |
| IzzyOnDroid | Android | Independent F-Droid-compatible repository index |
| Snap Store | Linux | Public snapd `/v2/find` search API |
| Flathub | Linux | Flathub AppStream catalogue |
| WinGet community index | Windows | winget.run search API (third-party index) |
| Steam storefront search | PC games | Public storefront JSON search by configured terms; partial coverage, not a full inventory |
| Homebrew Cask | macOS | Official Homebrew Cask JSON feed |
| Apple App Store | iOS | Apple public Search API, country configurable |
| Mac App Store | macOS | Apple public Search API with `macSoftware` entity |
| MTA curated web catalogue | Web | Maintained in `web_apps.json` |

The complete store directory and each store's integration state are maintained in `store_sources.json`. New live metadata connectors include Mozilla Firefox Add-ons, WordPress.org Plugins, Homebrew/core formulae, Scoop buckets, Alpine Linux APKINDEX, JetBrains Marketplace plugins, GNOME Shell extensions, and Fedora's public package index. Google Play has an optional AppBrain API connector; it supports bounded pagination (`APPBRAIN_MAX_PAGES`, up to 11 pages per category) and preserves the complete returned public metadata in the record metadata. The same connector supports Apple App Store results using `platform=IOS` and maps them to the Apple App Store listing, but this is opt-in via `APPBRAIN_ENABLE_IOS=true` to avoid unexpected API-credit usage. `APPBRAIN_IOS_MAX_PAGES` controls its page depth. AppBrain browse offsets are capped at 500 and the API has monthly credit limits; a full global inventory cannot be promised by a single browse sweep. JetBrains and GNOME search endpoints are public website endpoints rather than versioned public API contracts, so their collectors are isolated and failure-tolerant. Only entries marked `live_api` are included in ingestion; `external_only` entries remain direct links until a supported public API or approved partner feed is available. This prevents a store link from being misrepresented as an indexed catalogue.

Connectors fail independently. A temporary outage at one source should not stop ingestion from the others. Results are deduplicated by source key and external identifier, and non-HTTPS source links are rejected.

## API

- `GET /health` — service health.
- `GET /api/v1/apps` — list/search indexed applications.
- `GET /api/v1/apps?q=browser&platform=windows&limit=50&offset=0` — filtered results.
- `GET /api/v1/catalog-stats` — totals by platform and source.
- `POST /api/v1/ingest/catalogs` — ingest all configured catalogues (admin token required).
- `POST /api/v1/ingest/fdroid` — ingest F-Droid only (admin token required).

In the optional legacy SQL mode, the ingestion endpoints require the `X-Admin-Token` header. In GitHub mode, the API is read-only and POST ingestion requests return HTTP 405; use the GitHub Actions workflows to update the catalogue. Never place admin tokens in browser JavaScript or a public website bundle.

Example browser-side catalogue request:

```javascript
const response = await fetch("https://YOUR-API-HOST/api/v1/apps?platform=android&limit=24");
if (!response.ok) throw new Error("Catalogue request failed");
const apps = await response.json();
```

For public MTA Apps Center pages, call only the read-only GET endpoints from the browser. Configure `CORS_ORIGINS` to the exact production website origins. Do not expose admin ingestion endpoints or the admin token to visitors.

## Configuration

GitHub-native storage is the selected catalogue backend. The public catalogue is stored as sharded JSON and indexes in this repository; no PostgreSQL service, Neon plan, or Hyperdrive connection is required for the Cloudflare Worker.

For the Python API, set:
- `MTA_STORAGE_BACKEND=github`
- `GITHUB_CATALOGUE_DIR=catalogue_data`
- `CORS_ORIGINS` — comma-separated exact origins allowed to read the API.

The GitHub-backed API is read-only. Catalogue updates and enrichment run in GitHub Actions and are committed as snapshots. The previous SQL backend remains available as an optional compatibility mode, but it is not required for GitHub mode. Do not commit secrets or private/user data to the public catalogue repository.

Optional catalogue controls:
- `APPLE_COUNTRY`, `APPLE_SEARCH_TERMS`, `APPLE_LIMIT`
- `FIREFOX_ADDON_TERMS`, `FIREFOX_ADDON_PAGES`, `WORDPRESS_PLUGIN_TERMS`, `WORDPRESS_PLUGIN_PAGES`
- `APPBRAIN_API_KEY` (optional; enables the Google Play connector; API usage is metered by AppBrain)
- `WINGET_SEARCH_TERMS`, `WINGET_LIMIT`
- `FLATHUB_MAX_APPS`, `CATALOG_CONCURRENCY`

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
MTA_STORAGE_BACKEND=github GITHUB_CATALOGUE_DIR=catalogue_data uvicorn app:app --reload
```

For Docker Compose, make sure the `catalogue_data/` snapshot is present beside the compose file, then run `docker compose up --build -d`. The default compose stack is read-only and does not start PostgreSQL.

## Cloudflare deployment

The Cloudflare Worker serves the GitHub JSON catalogue directly through cached read requests to the repository's raw snapshot files. Its configuration uses `CATALOGUE_STORAGE=github` and `GITHUB_CATALOGUE_BRANCH=main`; no Hyperdrive binding or database credentials are needed. This does not change the existing static Pages site. See [Cloudflare deployment instructions](docs/CLOUDFLARE_DEPLOYMENT.md).

## Automated ingestion

`.github/workflows/github-native-catalogue.yml` performs source ingestion, metadata enrichment, index generation, and one Git commit of the resulting snapshot. It does not require `DATABASE_URL`. The scheduled workflow becomes active on the default branch after this pull request is merged. The existing SQL workflows are retained during migration and must only be retired after the GitHub snapshot and Cloudflare read path have passed verification.

## Development checks

```bash
pip install -r requirements-dev.txt
pytest -q
python -m py_compile app.py catalog_sources.py sync_catalogs.py
```

The source integrations depend on upstream public APIs and their current availability/terms. Catalogue coverage is bounded by each upstream's search and pagination limits; a single import should not be interpreted as a complete inventory of every application worldwide.
