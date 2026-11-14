# MTA Apps Center API integration

## Read-only website integration

Use the engine's public read endpoints from the MTA Apps Center frontend:

- List: `GET /api/v1/apps?limit=24&offset=0`
- Platform filter: `GET /api/v1/apps?platform=android&limit=24&offset=0`
- Search: `GET /api/v1/apps?q=browser&limit=24&offset=0`
- Source filter: `GET /api/v1/apps?source=fdroid&limit=24&offset=0`
- Catalogue counters: `GET /api/v1/catalog-stats`

Supported platform values currently include `android`, `linux`, `windows`, `macos`, `ios`, and `web`.

Each application record contains:
`id`, `source`, `external_id`, `platform`, `name`, `developer`, `summary`, `description`, `category`, `license`, `source_url`, `app_url`, `icon_url`, `first_seen_at`, and `updated_at`.

Render the record's `source_url` or `app_url` as an outbound link. Do not present an installer as hosted by MTA: the engine deliberately does not store installation files.

## Security and deployment

1. Deploy the API behind HTTPS.
2. Set `CORS_ORIGINS` to the exact MTA Apps Center origin(s).
3. Keep `ADMIN_API_TOKEN` only in a backend environment or GitHub Actions secret.
4. Never call `POST /api/v1/ingest/catalogs` from client-side website code.
5. Configure persistent PostgreSQL before enabling scheduled sync.

The engine repository now contains the API and integration contract. The MTA website repository has not been modified by these commits; the frontend must be connected separately after the API has a stable public deployment URL.
