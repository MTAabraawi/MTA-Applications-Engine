"""Fill missing Blue Jetty Android icons only from official Google Play OG image metadata."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from github_catalog_store import GitHubCatalogueStore


class OpenGraphImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = {str(key).lower(): value for key, value in attrs}
        if values.get("property", "").lower() == "og:image" and values.get("content"):
            self.image = values["content"].strip()


def official_google_play_url(record):
    metadata = record.get("metadata_json")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    metadata = metadata if isinstance(metadata, dict) else {}
    candidates = [record.get("app_url"), record.get("source_url")]
    for listing in metadata.get("store_destinations", []) if isinstance(metadata.get("store_destinations"), list) else []:
        if isinstance(listing, dict):
            candidates.append(listing.get("url"))
    detail = metadata.get("bluejetty_detail", {})
    for listing in detail.get("listings", []) if isinstance(detail, dict) and isinstance(detail.get("listings"), list) else []:
        if isinstance(listing, dict):
            candidates.extend([listing.get("listingUrl"), listing.get("url")])
    for value in candidates:
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value.strip())
            if parsed.scheme == "https" and parsed.hostname == "play.google.com" and parsed.path.startswith("/store/apps/details") and not parsed.username and not parsed.password:
                return value.strip()
        except ValueError:
            pass
    return None


def extract_official_og_icon(html):
    parser = OpenGraphImageParser()
    parser.feed(html or "")
    value = parser.image
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname == "play-lh.googleusercontent.com" and parsed.path and not parsed.username and not parsed.password:
            return value
    except ValueError:
        return None
    return None


def select_candidates(records, limit=1000, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    candidates = []
    for record in records:
        if record.get("source_key") != "bluejetty" or str(record.get("platform") or "").lower() != "android":
            continue
        if record.get("icon_url"):
            continue
        metadata = record.get("metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        metadata = metadata if isinstance(metadata, dict) else {}
        attempt = metadata.get("google_play_icon_enrichment", {})
        if isinstance(attempt, dict) and attempt.get("checked_at"):
            try:
                checked = datetime.fromisoformat(str(attempt["checked_at"]).replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                if checked > cutoff:
                    continue
            except ValueError:
                pass
        listing_url = official_google_play_url(record)
        if not listing_url:
            continue
        detail = metadata.get("bluejetty_detail", {})
        ratings = detail.get("ratings", {}) if isinstance(detail, dict) else {}
        rating_count = ratings.get("count", 0) if isinstance(ratings, dict) else 0
        try:
            rating_count = int(rating_count or 0)
        except (TypeError, ValueError):
            rating_count = 0
        candidates.append((-rating_count, str(record.get("external_id") or ""), record, listing_url))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [(record, url) for _, _, record, url in candidates[:max(0, int(limit))]]


async def fetch_one(client, record, listing_url, semaphore):
    async with semaphore:
        try:
            response = await client.get(listing_url, timeout=httpx.Timeout(20.0, connect=7.0))
            if response.status_code != 200:
                return record, listing_url, None, f"http_{response.status_code}"
            final_url = urlsplit(str(response.url))
            if final_url.scheme != "https" or final_url.hostname != "play.google.com":
                return record, listing_url, None, "untrusted_redirect"
            icon = extract_official_og_icon(response.text)
            return record, listing_url, icon, "matched" if icon else "og_image_missing"
        except httpx.TimeoutException:
            return record, listing_url, None, "timeout"
        except httpx.HTTPError as exc:
            return record, listing_url, None, type(exc).__name__


async def enrich(records, limit=1000, concurrency=6, now=None):
    now = now or datetime.now(timezone.utc)
    timestamp = now.isoformat()
    candidates = select_candidates(records, limit=limit, now=now)
    semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 12)))
    headers = {
        "User-Agent": "MTA-Applications-Engine/1.0 (+https://github.com/MTAabraawi/MTA-Applications-Engine)",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=5, headers=headers) as client:
        results = await asyncio.gather(*[
            fetch_one(client, record, url, semaphore)
            for record, url in candidates
        ])

    updates = []
    counts = {"selected": len(candidates), "matched": 0, "og_image_missing": 0, "http_error": 0, "timeout": 0, "other_error": 0}
    for record, listing_url, icon, status in results:
        item = dict(record)
        metadata = item.get("metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        provenance = {
            "status": status,
            "source_url": listing_url,
            "method": "official_google_play_og_image",
            "checked_at": timestamp,
        }
        if icon and not item.get("icon_url"):
            item["icon_url"] = icon
            provenance["icon_url"] = icon
            counts["matched"] += 1
        elif status == "og_image_missing":
            counts["og_image_missing"] += 1
        elif status.startswith("http_"):
            counts["http_error"] += 1
        elif status == "timeout":
            counts["timeout"] += 1
        else:
            counts["other_error"] += 1
        metadata["google_play_icon_enrichment"] = provenance
        item["metadata_json"] = metadata
        updates.append(item)
    return updates, counts


def main():
    root = Path(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    output = Path(os.getenv("GOOGLE_PLAY_ICON_REPORT", "reports/google-play-icon-enrichment.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    store = GitHubCatalogueStore(root)
    updates, counts = asyncio.run(enrich(
        store.iter_records(),
        limit=max(1, int(os.getenv("GOOGLE_PLAY_ICON_ENRICH_LIMIT", "1000"))),
        concurrency=max(1, int(os.getenv("GOOGLE_PLAY_ICON_ENRICH_CONCURRENCY", "6"))),
    ))
    result = store.upsert_many(updates) if updates else {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "official Google Play listing og:image; no screenshot or domain favicon substitution",
        "counts": counts,
        "storage": result,
        "policy": "Only HTTPS og:image URLs hosted on play-lh.googleusercontent.com are accepted. Existing icons are never overwritten.",
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
