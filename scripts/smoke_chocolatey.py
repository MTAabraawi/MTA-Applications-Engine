"""Live read-only smoke test for the public Chocolatey Community catalogue."""
import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_catalog_sources import fetch_chocolatey

logging.basicConfig(level="INFO")


async def main() -> None:
    async with httpx.AsyncClient(timeout=60.0) as client:
        records = await fetch_chocolatey(client)
    report = {
        "source": "chocolatey",
        "count": len(records),
        "sample": [
            {"name": item["name"], "url": item["source_url"]}
            for item in records[:10]
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not records:
        raise SystemExit("Chocolatey CLI returned no approved Community Repository packages.")


if __name__ == "__main__":
    asyncio.run(main())
