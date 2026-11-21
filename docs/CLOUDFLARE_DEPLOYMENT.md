# Cloudflare Workers deployment

The Cloudflare Worker serves the public catalogue directly from the GitHub repository's JSON snapshot. This mode requires no PostgreSQL database, Neon project, Hyperdrive binding, or database connection secret.

## Architecture

- Cloudflare Worker: `worker.js`, serving the public read API.
- Durable catalogue: `catalogue_data/` in this Git repository, partitioned into JSON shards and generated indexes.
- Ingestion and enrichment: `.github/workflows/github-native-catalogue.yml`, which runs the existing source collectors, enriches metadata, rebuilds indexes, and commits one catalogue snapshot.
- Website: calls the same public read endpoints over HTTPS.

The Worker reads `manifest.json`, the search/browse indexes, and only the shards needed for a request from `raw.githubusercontent.com`. Responses are cached at the edge and in the Worker isolate. The API is read-only in GitHub mode; POST ingestion requests return HTTP 405. This prevents runtime requests from attempting to mutate the Git repository.

## Worker configuration

The repository's `wrangler.jsonc` selects:

- `CATALOGUE_STORAGE=github`
- `GITHUB_CATALOGUE_BRANCH=main`

No Hyperdrive binding is configured. Deploy only after the GitHub-native catalogue workflow has committed `catalogue_data/manifest.json` and the indexes to the default branch.

## Deploy

Configure only the Cloudflare deployment credentials required by the existing deployment workflow:

- Actions secret: `CLOUDFLARE_API_TOKEN`
- Actions secret: `CLOUDFLARE_ACCOUNT_ID`

Then run **Actions → Deploy Cloudflare Worker → Run workflow**, or merge a change to the Worker files on `main` to trigger deployment. The workflow no longer requires a Hyperdrive ID or database secret.

## GitHub catalogue updates

The GitHub-native catalogue workflow runs daily and can be started manually once it exists on the default branch. It uses the existing source collectors and retains their independent failure handling. It then enriches Blue Jetty and Fedora records, creates search/browse/statistics indexes, and commits the updated catalogue.

The catalogue repository is public, so it must contain only public application metadata and outbound listing links. Never commit API keys, access tokens, personal information, user records, or private datasets. Optional source API credentials belong in GitHub Actions secrets.

## Verify deployment

After the catalogue snapshot exists on `main` and the Worker is deployed, verify:

- `GET /health` — should report `storage: github-json-shards`.
- `GET /api/v1/catalog-stats` — should return source/platform counts and metadata completeness.
- `GET /api/v1/apps?limit=5` — should return the first five records.
- `GET /api/v1/apps?q=firefox&limit=10` — should use the generated token index.

The existing public API paths and response fields are preserved. Record `id` becomes the stable `source_key:external_id` string in GitHub mode; consumers should use `source` and `external_id` as the canonical identity pair.

## Limitations and safety

- Git is versioned file storage, not SQL. There are no SQL queries or multi-record database transactions.
- Updates are batched and committed as snapshots. GitHub Actions serializes the writer workflow to avoid overlapping commits.
- Search uses generated token indexes; pagination and filtering are deterministic.
- The Worker reads only public repository files. Do not put non-public data in this store.
- The previous SQL mode remains in the code as a compatibility fallback during migration. Do not delete the old database or retire the old workflows until the Git-backed catalogue counts and API responses have been checked against a trusted export.
