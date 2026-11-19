# Git-backed catalogue storage (initial implementation)

This repository now has a pure-Python JSON shard store in `github_catalog_store.py`.
It is designed for the Git repository itself to hold public catalogue metadata:

- deterministic SHA-256 partitioning across 256 JSON shards;
- stable `source_key:external_id` identity;
- sparse-refresh protection and recursive metadata merge;
- atomically written shard files;
- generated platform, source, and category indexes plus a versioned manifest;
- deterministic search and pagination without a database service.

## Important deployment boundary

This is a Git-backed file store, **not PostgreSQL** and not a transactional database.
The intended writer is a GitHub Actions job that checks out the repository, writes a complete batch, and commits the resulting files once. Concurrent writers must be serialized (or use a ref/SHA compare-and-swap); never let independent jobs push overlapping snapshots concurrently.

The API process must still run on an actual application host. GitHub stores the source files and history; it does not host the FastAPI process. Public catalogue data can be served from the repository, but no secrets or user data may be committed.

## Current migration status

The existing SQLAlchemy API, ingestion scripts, and enrichment job remain unchanged at this stage. They continue to use their existing database configuration. This keeps the current service operational while the JSON store is tested and the remaining API/ingestion/enrichment adapters are migrated in separate, reviewable steps.

**No Neon data has been copied into this store.** The store starts empty until a real export is available. Do not point production traffic at it or disable the existing database until parity checks confirm record counts and representative records.
