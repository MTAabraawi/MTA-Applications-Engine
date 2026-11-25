"""Unified GitHub-native catalogue pipeline.

Runs every configured public source connector, preserves existing catalogue data,
executes the verified detail-page enrichers, and writes per-source quality state.
Unknown or credential-gated sources are never scraped or fabricated.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import sync_catalogs
import scripts.enrich_github_catalog as detail_enrichment
from github_catalog_store import GitHubCatalogueStore

QUALITY_FIELDS = (
    "icon_url", "app_url", "description", "summary", "developer",
    "category", "license", "source_url",
)


def has_value(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def build_source_quality(records: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("source_key") or "unknown")].append(record)

    result = {}
    for source, rows in sorted(grouped.items()):
        fields = {}
        for field in QUALITY_FIELDS:
            present = sum(
                1 for row in rows
                if has_value(row.get(field))
                and (not field.endswith("_url") or str(row.get(field)).startswith("https://"))
            )
            fields[field] = {
                "present": present,
                "missing": len(rows) - present,
                "percent": round(present * 100 / len(rows), 2) if rows else 0.0,
            }
        result[source] = {"records": len(rows), "fields": fields}
    return result


def write_state(root: Path, quality: dict, manifest: dict) -> dict:
    registry_path = Path("store_sources.json")
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else []
    registry_by_key = {
        item.get("source_key"): item
        for item in registry if item.get("source_key")
    }
    state = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "storage": "github-native-json-shards",
        "record_count": manifest.get("record_count", 0),
        "registered_store_count": len(registry),
        "sources_with_records": len(quality),
        "sources": {},
    }
    for source, report in quality.items():
        registry_item = registry_by_key.get(source, {})
        state["sources"][source] = {
            "record_count": report["records"],
            "integration_mode": registry_item.get("integration_mode", "registered_source"),
            "quality": report["fields"],
            "detail_enrichment": (
                "verified_detail_adapter"
                if source in {"bluejetty", "fedora-packages"}
                else "connector_metadata_and_quality_audit"
            ),
            "status": "indexed" if report["records"] else "empty",
        }
    target = root / "enrichment_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return state


async def main() -> None:
    # Full source discovery/import is the first stage; blank SYNC_SOURCES means
    # all configured connectors, not a selected subset.
    os.environ["MTA_STORAGE_BACKEND"] = "github"
    os.environ.setdefault("GITHUB_CATALOGUE_DIR", "catalogue_data")
    os.environ["SYNC_SOURCES"] = ""
    await sync_catalogs.main()

    # Retain the already-tested Blue Jetty/Fedora detail parsers and exact
    # official mobile-store icon cross-match while the remaining source-specific
    # detail adapters are implemented and verified.
    await detail_enrichment.main()

    store = GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    records = list(store.iter_records())
    quality = build_source_quality(records)
    manifest = store.rebuild_indexes()
    state = write_state(Path(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data")), quality, manifest)

    summary = {
        "record_count": manifest["record_count"],
        "registered_stores": state["registered_store_count"],
        "sources_with_records": state["sources_with_records"],
        "source_quality": quality,
        "detail_adapters_verified": ["bluejetty", "fedora-packages"],
        "detail_adapters_pending": sorted(
            source for source in quality if source not in {"bluejetty", "fedora-packages"}
        ),
        "state_file": "catalogue_data/enrichment_state.json",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
