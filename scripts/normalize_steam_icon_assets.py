"""Remove Steam capsule/header banners from the application icon field without discarding source URLs."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from github_catalog_store import GitHubCatalogueStore


NON_ICON_PATTERNS = (
    re.compile(r"capsule[_-]?\d+x\d+", re.IGNORECASE),
    re.compile(r"capsule_sm_\d+", re.IGNORECASE),
    re.compile(r"header(?:[_-]?\d+x\d+)?\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE),
)


def is_non_icon_steam_art(url):
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        path = urlsplit(url.strip()).path
    except ValueError:
        return False
    return any(pattern.search(path) for pattern in NON_ICON_PATTERNS)


def normalize(records, checked_at=None):
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    updates = []
    examples = []
    for record in records:
        if record.get("source_key") != "steam":
            continue
        original = record.get("icon_url")
        if not is_non_icon_steam_art(original):
            continue
        item = dict(record)
        metadata = item.get("metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        steam_detail = metadata.get("steam_detail")
        steam_detail = dict(steam_detail) if isinstance(steam_detail, dict) else {}
        steam_detail["capsule_or_header_art_url"] = original
        metadata["steam_detail"] = steam_detail
        metadata["steam_icon_cleanup"] = {
            "status": "removed_non_square_artwork_from_icon_field",
            "original_url": original,
            "checked_at": checked_at,
            "reason": "Steam capsule/header artwork is banner artwork, not a square application icon.",
        }
        item["metadata_json"] = metadata
        item["icon_url"] = None
        item["_clear_fields"] = ["icon_url"]
        updates.append(item)
        if len(examples) < 25:
            examples.append({"external_id": record.get("external_id"), "original_url": original})
    return updates, examples


def main():
    root = Path("catalogue_data")
    output = Path("reports/steam-icon-normalization.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    store = GitHubCatalogueStore(root)
    updates, examples = normalize(store.iter_records())
    result = store.upsert_many(updates, rebuild_indexes=False) if updates else {"updated": 0, "inserted": 0, "unchanged": 0, "skipped": 0}
    manifest = store.rebuild_indexes() if updates else store.manifest()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "steam",
        "removed_from_icon_url": len(updates),
        "storage": result,
        "catalogue_total_after": manifest.get("record_count", manifest.get("total")),
        "examples": examples,
        "policy": "Original Steam capsule/header URLs are preserved in metadata_json.steam_detail.capsule_or_header_art_url; they are not presented as application icons.",
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
