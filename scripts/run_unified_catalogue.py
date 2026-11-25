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
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sync_catalogs
import scripts.enrich_github_catalog as detail_enrichment
from scripts.enrich_catalogue_official_pages import enrich as enrich_official_pages
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



def _metadata_object(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _metadata_values(metadata: dict) -> dict:
    """Extract only explicit, source-provided metadata; never infer facts."""
    containers = [metadata]
    for key in ("app", "package", "details", "listing", "publisher", "links"):
        child = metadata.get(key)
        if isinstance(child, dict):
            containers.append(child)
    aliases = {
        "developer": ("developer", "publisher_name", "author", "owner"),
        "summary": ("summary", "short_description", "tagline"),
        "description": ("description", "long_description", "about"),
        "category": ("category", "categories", "genre"),
        "license": ("license", "license_name", "spdx_license"),
        "app_url": ("app_url", "homepage", "website", "download_url"),
        "icon_url": ("icon_url", "icon", "logo_url", "avatar_url", "image_url"),
    }
    result = {}
    for field, keys in aliases.items():
        for container in containers:
            candidate = next((container.get(key) for key in keys if has_value(container.get(key))), None)
            # Publisher objects commonly expose the verified organization name
            # as {"publisher": {"name": "..."}}. Read that name only from
            # the publisher object, never from an app's top-level name.
            if (
                field == "developer"
                and candidate is None
                and container is metadata.get("publisher")
                and isinstance(container, dict)
                and has_value(container.get("name"))
            ):
                candidate = container.get("name")
            if isinstance(candidate, list):
                candidate = ", ".join(str(item).strip() for item in candidate if has_value(item))
            if isinstance(candidate, dict):
                candidate = candidate.get("name") or candidate.get("url")
            if not has_value(candidate):
                continue
            candidate = str(candidate).strip()
            if field.endswith("_url") and not candidate.startswith("https://"):
                continue
            result[field] = candidate
            break
    return result


def enrich_from_source_metadata(store: GitHubCatalogueStore) -> dict:
    """Backfill gaps from explicit fields already supplied by each source API."""
    updates = []
    counts = {"scanned": 0, "updated": 0, "fields_filled": {}}
    for row in store.iter_records():
        counts["scanned"] += 1
        metadata = _metadata_object(row.get("metadata_json"))
        candidates = _metadata_values(metadata)
        item = dict(row)
        changed = False
        for field, value in candidates.items():
            if not has_value(item.get(field)):
                item[field] = value
                counts["fields_filled"][field] = counts["fields_filled"].get(field, 0) + 1
                changed = True
        if changed:
            updates.append(item)
            counts["updated"] += 1
    if updates:
        store.upsert_many(updates, rebuild_indexes=False)
    return counts

def write_state(root: Path, quality: dict, manifest: dict) -> dict:
    registry_path = Path("store_sources.json")
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else []
    registry_by_key = {
        item.get("source_key"): item for item in registry if item.get("source_key")
    }
    records_sources = set(quality)
    state = {
        "schema_version": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "storage": "github-native-json-shards",
        "record_count": manifest.get("record_count", 0),
        "registered_store_count": len(registry),
        "sources_with_records": len(records_sources),
        "stores": {},
        "sources": {},
    }
    for item in registry:
        store_id = item.get("id") or item.get("source_key") or "unknown"
        source_key = item.get("source_key")
        count = quality.get(source_key, {}).get("records", 0) if source_key else 0
        mode = item.get("integration_mode", "unclassified")
        state["stores"][store_id] = {
            "name": item.get("name"),
            "platform": item.get("platform"),
            "source_key": source_key,
            "integration_mode": mode,
            "record_count": count,
            "status": "indexed" if count else (
                "credential_required" if mode == "credential_required"
                else "external_only" if mode == "external_only"
                else "connector_empty_or_not_yet_synced"
            ),
            "reason": item.get("reason"),
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
                else "source_metadata_backfill_only"
            ),
            "status": "indexed",
        }
    target = root / "enrichment_state.json"
    root.mkdir(parents=True, exist_ok=True)
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

    store = GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    metadata_backfill = enrich_from_source_metadata(store)

    # Retain the already-tested Blue Jetty/Fedora detail parsers and exact
    # official mobile-store icon cross-match while the remaining source-specific
    # detail adapters are implemented and verified.
    await detail_enrichment.main()
    official_page_enrichment = await enrich_official_pages(store)

    records = list(store.iter_records())
    quality = build_source_quality(records)
    manifest = store.rebuild_indexes()
    state = write_state(Path(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data")), quality, manifest)

    summary = {
        "record_count": manifest["record_count"],
        "registered_stores": state["registered_store_count"],
        "sources_with_records": state["sources_with_records"],
        "source_metadata_backfill": metadata_backfill,
        "official_page_enrichment": official_page_enrichment,
        "source_quality": quality,
        "detail_adapters_verified": ["bluejetty", "fedora-packages", "generic_official_html_schema_org"],
        "detail_adapters_pending": sorted(
            source for source in quality if source not in {"bluejetty", "fedora-packages"}
        ),
        "state_file": "catalogue_data/enrichment_state.json",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
