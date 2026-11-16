"""Fetch the expanded public catalogues and write a reproducible JSON snapshot."""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from expanded_catalog_sources import fetch_expanded_catalogs

logging.basicConfig(level="INFO")


async def main() -> None:
    catalogues = await fetch_expanded_catalogs()
    output = Path("expanded-catalog-snapshot.json")
    output.write_text(json.dumps(catalogues, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {source: len(records) for source, records in catalogues.items()}
    print(json.dumps({"counts": counts, "total": sum(counts.values()), "snapshot": str(output)}, indent=2))
    if not any(counts.values()):
        raise SystemExit("No expanded catalogue returned records")


if __name__ == "__main__":
    asyncio.run(main())
