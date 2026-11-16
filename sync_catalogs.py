"""Run a resumable-by-source catalogue metadata sync against the configured database.

Each source is committed as soon as its fetch completes. Re-running the workflow is
safe because upsert_records uses the (source_key, external_id) unique key.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from app import SessionLocal, engine, upsert_records
from catalog_sources import fetch_all_catalogs
from expanded_catalog_sources import fetch_expanded_catalogs

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def main() -> None:
    if not os.getenv("DATABASE_URL"):
        raise SystemExit("DATABASE_URL must point to a persistent database (for example PostgreSQL).")

    # Widen bounded metadata fields in place. PostgreSQL otherwise rejects long
    # category tag lists and SPDX license expressions; existing rows are preserved.
    if engine.dialect.name == "postgresql":
        columns = {column["name"]: column for column in inspect(engine).get_columns("apps")}
        with engine.begin() as connection:
            for column_name in ("category", "license"):
                column = columns.get(column_name)
                if column and getattr(column["type"], "length", None) is not None:
                    connection.execute(text(
                        f"ALTER TABLE apps ALTER COLUMN {column_name} TYPE TEXT USING {column_name}::text"
                    ))
                    logging.info("Expanded apps.%s to TEXT without changing existing rows", column_name)

    selected_sources = {
        item.strip() for item in os.getenv("SYNC_SOURCES", "").split(",") if item.strip()
    }
    summary = {}
    db = SessionLocal()

    def save_source(source: str, records: list[dict], error: Exception | None = None) -> None:
        started = datetime.now(timezone.utc).isoformat()
        if error is not None:
            result = {"fetched": 0, "error": str(error)}
            logging.error("Catalogue source %s failed during fetch: %s", source, error)
        else:
            try:
                result = {"fetched": len(records), **upsert_records(db, records)}
            except Exception as exc:
                db.rollback()
                logging.exception("Could not save source %s", source)
                result = {"fetched": len(records), "error": str(exc)}
        summary[source] = result
        logging.info("SOURCE_PROGRESS %s", json.dumps({
            "source": source, "completed_at": started, **result
        }, ensure_ascii=False))
        print(json.dumps({"source": source, **result}, ensure_ascii=False), flush=True)

    try:
        if selected_sources:
            logging.info("Starting targeted catalogue retry for: %s", ", ".join(sorted(selected_sources)))
            await fetch_expanded_catalogs(on_source=save_source, source_filter=selected_sources)
        else:
            logging.info("Starting base catalogue sources")
            await fetch_all_catalogs(on_source=save_source)
            logging.info("Starting expanded catalogue sources")
            await fetch_expanded_catalogs(on_source=save_source)
    finally:
        db.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not any("error" not in result and result.get("fetched", 0) for result in summary.values()):
        raise SystemExit("No catalogue source returned records; review source availability/logs.")


if __name__ == "__main__":
    asyncio.run(main())
