"""Live, read-only smoke test for upstream catalogue connectors."""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_sources import fetch_all_catalogs
from expanded_catalog_sources import fetch_expanded_catalogs

logging.basicConfig(level="INFO")


async def main() -> None:
    catalogues = await fetch_all_catalogs()
    expanded = await fetch_expanded_catalogs(source_filter={"bluejetty", "openuse"})
    catalogues.update(expanded)
    report = {
        source: {
            "count": len(records),
            "sample": [item["name"] for item in records[:3]],
        }
        for source, records in catalogues.items()
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    missing_public = [name for name in ("bluejetty", "openuse") if not report.get(name, {}).get("count")]
    if missing_public:
        raise SystemExit("New public catalogue connectors returned no records: " + ", ".join(missing_public))
    if not any(item["count"] for item in report.values()):
        raise SystemExit("All upstream catalogue connectors returned zero records.")


if __name__ == "__main__":
    asyncio.run(main())
