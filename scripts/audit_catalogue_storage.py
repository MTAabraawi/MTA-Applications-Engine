"""Read-only PostgreSQL storage audit for the MTA catalogue.

This script never deletes, updates, vacuums, or alters database objects.
Run with DATABASE_URL set to the production PostgreSQL connection string.
"""
import os
from sqlalchemy import create_engine, text


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        if engine.dialect.name != "postgresql":
            raise SystemExit("This audit currently supports PostgreSQL only.")

        with engine.connect() as connection:
            print("=== PostgreSQL relation storage ===")
            relations = connection.execute(text("""
                SELECT n.nspname AS schema_name, c.relname AS relation_name,
                       c.relkind,
                       pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
                       pg_total_relation_size(c.oid) AS total_bytes,
                       pg_size_pretty(pg_relation_size(c.oid)) AS table_size,
                       pg_size_pretty(pg_indexes_size(c.oid)) AS indexes_size
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND c.relkind IN ('r', 'm', 'i')
                ORDER BY pg_total_relation_size(c.oid) DESC
                LIMIT 50
            """)).mappings().all()
            for row in relations:
                print(f"{row['schema_name']}.{row['relation_name']}: "
                      f"{row['total_size']} total "
                      f"(table {row['table_size']}, indexes {row['indexes_size']})")

            print("\\n=== Catalogue rows and estimated payload by source ===")
            sources = connection.execute(text("""
                SELECT source_key,
                       count(*) AS rows,
                       pg_size_pretty(sum(pg_column_size(apps))) AS estimated_row_payload,
                       sum(pg_column_size(apps)) AS estimated_bytes,
                       count(*) FILTER (WHERE icon_url IS NOT NULL AND btrim(icon_url) <> '') AS with_icon,
                       count(*) FILTER (WHERE description IS NOT NULL AND btrim(description) <> '') AS with_description,
                       count(*) FILTER (WHERE developer IS NOT NULL AND btrim(developer) <> '') AS with_developer,
                       min(first_seen_at) AS first_seen,
                       max(updated_at) AS last_updated
                FROM apps
                GROUP BY source_key
                ORDER BY sum(pg_column_size(apps)) DESC
            """)).mappings().all()
            for row in sources:
                print(f"{row['source_key']}: {row['rows']} rows, "
                      f"{row['estimated_row_payload']} estimated payload, "
                      f"icons={row['with_icon']}, descriptions={row['with_description']}, "
                      f"developers={row['with_developer']}, "
                      f"first_seen={row['first_seen']}, last_updated={row['last_updated']}")

            print("\\n=== Safe cleanup assessment ===")
            print("No records were changed.")
            print("Per-source payload is an estimate, not exact reclaimable disk space.")
            print("Review source counts, quality, dependencies, and backups before any deletion.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
