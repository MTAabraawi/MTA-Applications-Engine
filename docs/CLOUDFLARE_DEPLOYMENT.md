# Cloudflare Workers deployment

The API can run as a Python Worker while keeping PostgreSQL as the durable catalogue database. The Worker uses Cloudflare Hyperdrive to reach PostgreSQL; the daily GitHub Actions job continues to use a direct PostgreSQL connection string for catalogue ingestion.

## Architecture

- Cloudflare Worker: FastAPI ASGI application in `worker.py`.
- Database: managed PostgreSQL, connected to the Worker through the `HYPERDRIVE` binding.
- Ingestion: GitHub Actions runs `sync_catalogs.py` daily using the repository secret `DATABASE_URL`.
- Website: MTA Apps Center calls only the public read endpoints over HTTPS.

The Worker does not create tables at startup. The sync runner creates the SQLAlchemy schema before importing records.

## One-time Cloudflare setup

1. Create or select a PostgreSQL database. Keep its direct connection string private.
2. In Cloudflare, create a Hyperdrive configuration for that PostgreSQL connection. The CLI equivalent is:

   ```bash
   npx wrangler hyperdrive create mta-applications-engine --connection-string="$DATABASE_URL"
   ```

3. Copy the returned Hyperdrive configuration ID into `wrangler.jsonc`, replacing `REPLACE_WITH_HYPERDRIVE_CONFIG_ID`.
4. Add a strong ingestion token as a Worker secret:

   ```bash
   npx wrangler secret put ADMIN_API_TOKEN
   ```

5. Deploy from this repository:

   ```bash
   uv run pywrangler deploy
   ```

6. Attach a custom domain such as `api.mtahqq.dpdns.org` to the Worker in Cloudflare's dashboard. Do not point the website's existing Pages hostname at the Worker.

Cloudflare's Python Workers support FastAPI through its ASGI adapter. Python Hyperdrive supports PostgreSQL drivers including psycopg and synchronous SQLAlchemy usage.

## GitHub Actions setup

Add a repository Actions secret named `DATABASE_URL` containing the same PostgreSQL database's direct connection string. This is separate from the Worker Hyperdrive binding and is used by `.github/workflows/catalogue-sync.yml`.

The scheduled workflow is already configured for daily UTC execution. It skips safely if `DATABASE_URL` is absent. It becomes active on the default branch only after this pull request is merged.

## Verify deployment

After deployment, verify:

- `GET https://api.mtahqq.dpdns.org/health`
- `GET https://api.mtahqq.dpdns.org/api/v1/catalog-stats`
- `GET https://api.mtahqq.dpdns.org/api/v1/apps?limit=5`

The MTA website frontend is not changed by this deployment guide. Once the API hostname is live, its public read endpoints can be connected to the website without exposing the admin token.

## Security

- Never commit the PostgreSQL connection string, Hyperdrive credentials, or `ADMIN_API_TOKEN`.
- Do not place `ADMIN_API_TOKEN` in website JavaScript.
- Keep `CORS_ORIGINS` limited to the exact MTA Apps Center origins.
- The catalogue stores metadata and official outbound listing URLs only; it does not download or host installers.
