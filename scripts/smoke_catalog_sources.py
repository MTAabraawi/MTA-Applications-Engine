"""Live, read-only smoke test for upstream catalogue connectors."""
import asyncio
import json
import logging
import os

from catalog_sources import fetch_all_catalogs

logging.basicConfig(level="INFO")


async def main() -> None:
    catalogues = await fetch_all_catalogs()
    report = {
        source: {
            "count": len(records),
            "sample": [item["name"] for item in records[:3]],
        }
        for source, records in catalogues.items()
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not any(item["count"] for item in report.values()):
        raise SystemExit("All upstream catalogue connectors returned zero records.")


if __name__ == "__main__":
    asyncio.run(main())
