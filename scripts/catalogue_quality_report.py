"""Build a source-by-source metadata quality report for the Git-backed catalogue.

The report distinguishes registered stores from sources that actually have
records. Missing fields are reported as gaps, never filled with invented data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from github_catalog_store import GitHubCatalogueStore


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "store_sources.json"


def build_report(stats: dict, registry: list[dict]) -> dict:
    quality_sources = (stats.get("quality") or {}).get("sources") or {}
    records_by_source = stats.get("sources") or {}
    registered = {}
    for entry in registry:
        source_key = entry.get("source_key")
        registered[entry["id"]] = {
            "store_id": entry["id"],
            "name": entry.get("name") or entry["id"],
            "source_key": source_key,
            "integration_mode": entry.get("integration_mode") or "unspecified",
            "record_count": int(records_by_source.get(source_key, 0)) if source_key else 0,
            "quality": quality_sources.get(source_key, {}),
        }
    modes = {}
    for item in registered.values():
        mode = item["integration_mode"]
        modes[mode] = modes.get(mode, 0) + 1
    active_records = sum(1 for item in registered.values() if item["record_count"] > 0)
    return {
        "catalogue_record_count": int(stats.get("total", 0)),
        "registered_store_count": len(registered),
        "stores_with_records": active_records,
        "stores_without_records": len(registered) - active_records,
        "integration_modes": dict(sorted(modes.items())),
        "stores": dict(sorted(registered.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue-dir", default="catalogue_data")
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    parser.add_argument("--output", help="Optional path to write the JSON report")
    args = parser.parse_args()

    store = GitHubCatalogueStore(args.catalogue_dir)
    stats = store.stats()
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    report = build_report(stats, registry)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
