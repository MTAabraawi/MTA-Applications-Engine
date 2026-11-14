"""Run one full catalogue metadata sync against the configured database."""
import asyncio
import json
import logging
import os

from app import SessionLocal, upsert_records
from catalog_sources import fetch_all_catalogs

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


async def main() -> None:
    if not os.getenv("DATABASE_URL"):
        raise SystemExit("DATABASE_URL must point to a persistent database (for example PostgreSQL).")
    catalogues = await fetch_all_catalogs()
    summary = {}
    db = SessionLocal()
    try:
        for source, records in catalogues.items():
            try:
                summary[source] = {"fetched": len(records), **upsert_records(db, records)}
            except Exception as exc:
                db.rollback()
                logging.exception("Could not save source %s", source)
                summary[source] = {"fetched": len(records), "error": str(exc)}
    finally:
        db.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not any("error" not in result and result.get("fetched", 0) for result in summary.values()):
        raise SystemExit("No catalogue source returned records; review source availability/logs.")


if __name__ == "__main__":
    asyncio.run(main())
