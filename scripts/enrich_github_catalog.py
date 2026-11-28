"""Git-backed, resumable catalogue enrichment.

This mirrors the existing SQL enrichment policy for Blue Jetty and Fedora
records while writing only to the checked-out Git JSON catalogue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_catalog_store import GitHubCatalogueStore, _merge_metadata
from fedora_catalog_source import parse_fedora_detail
from open_catalog_sources import parse_bluejetty_detail

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mta.github_enrichment")
HEADERS = {"User-Agent": "MTA-Applications-Engine/1.0 (+https://github.com/MTAabraawi/MTA-Applications-Engine)"}
TIMEOUT = httpx.Timeout(35.0, connect=10.0)
PRIORITY = {
    "firefox", "chromium", "google-chrome", "libreoffice", "gimp", "blender",
    "vlc", "inkscape", "krita", "thunderbird", "audacity", "obs-studio",
    "mpv", "code", "vscode", "git", "python3", "nodejs", "steam",
    "telegram-desktop", "signal-desktop", "discord", "qbittorrent",
    "transmission", "filezilla", "keepassxc", "shotcut", "kdenlive",
    "darktable", "handbrake", "wireshark", "neovim", "virt-manager",
}
FILL_FIELDS = ("developer", "summary", "description", "category", "license", "app_url", "icon_url")


def has_value(value):
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def metadata_object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def trusted_url(value, hostname):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and parsed.hostname == hostname and not parsed.username and not parsed.password
    except ValueError:
        return False


async def get_response(client, url, trusted_host):
    last_error = None
    for attempt in range(3):
        current = url
        try:
            for _ in range(4):
                response = await client.get(current, timeout=TIMEOUT, headers=HEADERS, follow_redirects=False)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        response.raise_for_status()
                    target = urljoin(current, location)
                    if not trusted_url(target, trusted_host):
                        raise ValueError("Untrusted detail-page redirect target")
                    current = target
                    continue
                response.raise_for_status()
                if not trusted_url(str(response.url), trusted_host):
                    raise ValueError("Untrusted detail-page response host")
                return response
            raise ValueError("Too many detail-page redirects")
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    raise last_error or RuntimeError("Upstream request failed")


def due(record, prefix, days, required_fields):
    if not any(not has_value(record.get(field)) for field in required_fields):
        return False
    metadata = metadata_object(record.get("metadata_json"))
    attempt = metadata.get(f"{prefix}_enrichment_last_attempt_at")
    if not attempt:
        return True
    try:
        value = datetime.fromisoformat(str(attempt).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value < datetime.now(timezone.utc) - timedelta(days=days)
    except (TypeError, ValueError):
        return True


async def fetch_records(rows, client, parser, hostname, concurrency):
    semaphore = asyncio.Semaphore(concurrency)

    async def one(row):
        metadata = metadata_object(row.get("metadata_json"))
        url = metadata.get("record_json_url") if hostname == "bluejetty.ai" else row.get("source_url")
        if not trusted_url(url, hostname):
            return row, None, ValueError("Untrusted or missing detail URL")
        async with semaphore:
            await asyncio.sleep(0.35)
            try:
                response = await get_response(client, url, hostname)
                payload = response.json() if hostname == "bluejetty.ai" else response.text
                parsed = parser(payload)
                return row, parsed, None
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                return row, None, exc

    return await asyncio.gather(*(one(row) for row in rows))


def apply_results(results, prefix, now):
    updates = []
    counts = {"checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}}
    for row, data, error in results:
        counts["checked"] += 1
        item = dict(row)
        metadata = metadata_object(item.get("metadata_json"))
        metadata[f"{prefix}_enrichment_last_attempt_at"] = now
        if error is not None or not isinstance(data, dict):
            metadata[f"{prefix}_enrichment_status"] = "failed"
            metadata[f"{prefix}_enrichment_error"] = type(error).__name__ if error else "invalid_payload"
            counts["failed"] += 1
            item["metadata_json"] = metadata
            updates.append(item)
            continue
        filled = 0
        for field in FILL_FIELDS:
            value = data.get(field)
            if not has_value(item.get(field)) and has_value(value):
                item[field] = value
                counts["fields_filled"][field] = counts["fields_filled"].get(field, 0) + 1
                filled += 1
        item["metadata_json"] = _merge_metadata(metadata, data.get("metadata_json") or {})
        item["metadata_json"][f"{prefix}_enrichment_status"] = "enriched" if filled else "checked"
        item["metadata_json"].pop(f"{prefix}_enrichment_error", None)
        if filled:
            counts["enriched"] += 1
        updates.append(item)
    return updates, counts


def store_ids(value, depth=0):
    if depth > 12:
        return set()
    found = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(store_ids(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.update(store_ids(child, depth + 1))
    elif isinstance(value, str) and value.startswith("https://"):
        try:
            parsed = urlsplit(value)
            if parsed.hostname == "play.google.com" and parsed.path.rstrip("/") == "/store/apps/details":
                package = (parse_qs(parsed.query).get("id") or [""])[0].strip()
                if package:
                    found.add(("android", package))
            elif parsed.hostname == "apps.apple.com":
                match = re.search(r"/id(\d+)(?:/|$)", parsed.path)
                if match:
                    found.add(("ios", match.group(1)))
        except ValueError:
            pass
    return found


def icon_provider_index(records):
    providers = {}
    official = {"google-play", "apple-itunes", "apple-appbrain", "fdroid", "izzyondroid"}
    for row in records:
        if row.get("source_key") not in official or row.get("platform") not in {"android", "ios"} or not has_value(row.get("icon_url")):
            continue
        providers.setdefault((row["platform"], str(row.get("external_id"))), {
            "source_key": row["source_key"], "icon_url": row["icon_url"]
        })
    return providers


def crossmatch_icons(records, provider_index, now):
    counts = {"scanned": 0, "matched": 0, "unmatched": 0, "ambiguous": 0}
    updates = []
    for row in records:
        # Reconcile missing mobile icons across every catalogue source, not
        # only Blue Jetty. A match is accepted only from an exact official
        # Google Play package ID or Apple App Store numeric listing ID.
        if has_value(row.get("icon_url")) or row.get("platform") not in {"android", "ios"}:
            continue
        if row.get("source_key") in {"google-play", "apple-itunes", "apple-appbrain", "fdroid", "izzyondroid"}:
            continue
        metadata = metadata_object(row.get("metadata_json"))
        candidates = []
        link_evidence = {
            "source_url": row.get("source_url"),
            "app_url": row.get("app_url"),
            "metadata_json": metadata,
        }
        for platform, external_id in store_ids(link_evidence):
            if row.get("platform") in {"android", "ios"} and platform != row.get("platform"):
                continue
            provider = provider_index.get((platform, external_id))
            if provider:
                candidates.append((platform, external_id, provider["source_key"], provider["icon_url"]))
        unique_icons = {item[3] for item in candidates}
        item = dict(row)
        metadata["icon_crossmatch_last_attempt_at"] = now
        if len(unique_icons) == 1:
            platform, external_id, source, icon = sorted(candidates)[0]
            item["icon_url"] = icon
            metadata["icon_crossmatch"] = {
                "status": "matched", "method": "exact_official_store_listing_id",
                "store_platform": platform, "store_external_id": external_id,
                "provider_source": source, "matched_at": now,
            }
            counts["matched"] += 1
        elif len(unique_icons) > 1:
            metadata["icon_crossmatch"] = {"status": "ambiguous", "method": "exact_official_store_listing_id", "checked_at": now}
            counts["ambiguous"] += 1
        else:
            metadata["icon_crossmatch"] = {"status": "unmatched", "method": "exact_official_store_listing_id", "checked_at": now}
            counts["unmatched"] += 1
        item["metadata_json"] = metadata
        updates.append(item)
        counts["scanned"] += 1
    return updates, counts


async def enrich_bluejetty_ios_from_apple(store, records, client):
    """Batch-enrich Blue Jetty iOS records from Apple's public Lookup API.

    Blue Jetty supplies exact App Store listing IDs in its publisher-authorized
    store observations. Apple's lookup endpoint accepts multiple exact IDs in
    one request and returns the app artwork and publisher metadata. No names
    or fuzzy matches are used.
    """
    rows_by_id = {}
    for row in records:
        if row.get("source_key") != "bluejetty" or row.get("platform") != "ios" or has_value(row.get("icon_url")):
            continue
        evidence = {
            "source_url": row.get("source_url"),
            "app_url": row.get("app_url"),
            "metadata_json": metadata_object(row.get("metadata_json")),
        }
        ids = sorted(external_id for platform, external_id in store_ids(evidence)
                     if platform == "ios" and str(external_id).isdigit())
        if len(ids) != 1:
            continue
        rows_by_id.setdefault(ids[0], []).append(row)

    app_ids = sorted(rows_by_id)
    if not app_ids:
        return {"selected": 0, "requests": 0, "matched": 0, "icons_filled": 0,
                "descriptions_filled": 0, "developers_filled": 0, "categories_filled": 0}

    batch_size = 200
    concurrency = 3
    country = os.getenv("APPLE_COUNTRY", "us").strip().lower() or "us"
    totals = {"selected": len(app_ids), "requests": 0, "matched": 0,
              "icons_filled": 0, "descriptions_filled": 0,
              "developers_filled": 0, "categories_filled": 0}
    now = datetime.now(timezone.utc).isoformat()
    batches = [app_ids[i:i + batch_size] for i in range(0, len(app_ids), batch_size)]

    async def lookup(batch):
        last_error = None
        for attempt in range(4):
            try:
                response = await client.get(
                    "https://itunes.apple.com/lookup",
                    params={"id": ",".join(batch), "entity": "software", "country": country},
                    timeout=httpx.Timeout(40.0, connect=12.0),
                    headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"},
                )
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                payload = response.json()
                return payload.get("results", []) if isinstance(payload, dict) else []
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt == 3:
                    break
                await asyncio.sleep(min(2 ** attempt, 12))
        log.warning("Apple Lookup batch failed (%d IDs): %s", len(batch), last_error)
        return []

    for offset in range(0, len(batches), concurrency):
        group = batches[offset:offset + concurrency]
        responses = await asyncio.gather(*(lookup(batch) for batch in group))
        totals["requests"] += len(group)
        updates_by_key = {}
        for results in responses:
            for app in results:
                if not isinstance(app, dict):
                    continue
                track_id = str(app.get("trackId") or "").strip()
                if not track_id or track_id not in rows_by_id:
                    continue
                artwork = app.get("artworkUrl512") or app.get("artworkUrl100") or app.get("artworkUrl60")
                developer = app.get("sellerName") or app.get("artistName")
                description = app.get("description")
                category = app.get("primaryGenreName")
                app_url = app.get("trackViewUrl")
                for row in rows_by_id[track_id]:
                    item = dict(row)
                    changed = False
                    if not has_value(item.get("icon_url")) and has_value(artwork):
                        item["icon_url"] = artwork
                        totals["icons_filled"] += 1
                        changed = True
                    if not has_value(item.get("developer")) and has_value(developer):
                        item["developer"] = developer
                        totals["developers_filled"] += 1
                        changed = True
                    if not has_value(item.get("description")) and has_value(description):
                        item["description"] = description
                        changed = True
                    if not has_value(item.get("summary")) and has_value(description):
                        item["summary"] = str(description)[:1000]
                        totals["descriptions_filled"] += 1
                        changed = True
                    if not has_value(item.get("category")) and has_value(category):
                        item["category"] = category
                        totals["categories_filled"] += 1
                        changed = True
                    if not has_value(item.get("app_url")) and has_value(app_url):
                        item["app_url"] = app_url
                        changed = True
                    if changed:
                        metadata = metadata_object(item.get("metadata_json"))
                        metadata["apple_lookup_enrichment"] = {
                            "source": "Apple iTunes Lookup API",
                            "track_id": track_id,
                            "country": country,
                            "checked_at": now,
                        }
                        item["metadata_json"] = metadata
                        updates_by_key[(item["source_key"], item["external_id"])] = item

        if updates_by_key:
            updates = list(updates_by_key.values())
            store.upsert_many(updates, rebuild_indexes=False)
            for index, row in enumerate(records):
                replacement = updates_by_key.get((row.get("source_key"), row.get("external_id")))
                if replacement is not None:
                    records[index] = replacement
            totals["matched"] += len(updates)
        if (offset // concurrency + 1) % 25 == 0:
            store.checkpoint_git("Checkpoint Blue Jetty iOS Apple Lookup enrichment")
            log.info("Blue Jetty Apple Lookup progress: requests=%d/%d matched=%d icons=%d",
                     totals["requests"], len(batches), totals["matched"], totals["icons_filled"])

    store.checkpoint_git("Checkpoint completed Blue Jetty iOS Apple Lookup enrichment")
    return totals


async def main():
    store = GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    records = list(store.iter_records())
    if not records:
        raise SystemExit("GitHub catalogue is empty; run catalogue sync before enrichment.")
    blue_limit = max(1, min(int(os.getenv("BLUEJETTY_ENRICH_BATCH_SIZE", "200")), 500))
    fedora_limit = max(1, min(int(os.getenv("FEDORA_ENRICH_BATCH_SIZE", "25")), 100))
    concurrency = max(1, min(int(os.getenv("ENRICH_CONCURRENCY", "2")), 4))
    max_batches = max(1, min(int(os.getenv("ENRICHMENT_MAX_BATCHES", "1000")), 1000))
    runtime = max(5, min(int(os.getenv("ENRICHMENT_MAX_RUNTIME_MINUTES", "300")), 330))
    deadline = time.monotonic() + runtime * 60
    totals = {
        "bluejetty": {"batches": 0, "selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}},
        "fedora": {"batches": 0, "selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}},
    }
    providers = icon_provider_index(records)
    cross_updates, cross_counts = crossmatch_icons(records, providers, datetime.now(timezone.utc).isoformat())
    if cross_updates:
        store.upsert_many(cross_updates, rebuild_indexes=False)
        store.checkpoint_git("Checkpoint exact official-store icon crossmatch")
        cross_by_key = {(row["source_key"], row["external_id"]): row for row in cross_updates}
        for index, row in enumerate(records):
            replacement = cross_by_key.get((row.get("source_key"), row.get("external_id")))
            if replacement is not None:
                records[index] = replacement
        log.info("Exact official-store icon cross-match: %s", cross_counts)

    async with httpx.AsyncClient(follow_redirects=False) as client:
        apple_lookup_totals = await enrich_bluejetty_ios_from_apple(store, records, client)
        log.info("Blue Jetty iOS exact Apple Lookup enrichment: %s", apple_lookup_totals)
        for batch in range(max_batches):
            if time.monotonic() >= deadline:
                log.warning("Enrichment runtime limit reached; saved progress is resumable.")
                break
            any_selected = False
            for source, prefix, hostname, parser, limit, days, fields in (
                ("bluejetty", "bluejetty", "bluejetty.ai", parse_bluejetty_detail, blue_limit, 3,
                 ("developer", "summary", "description", "category", "app_url", "icon_url")),
                ("fedora", "fedora", "packages.fedoraproject.org", parse_fedora_detail, fedora_limit, 7,
                 ("summary", "description", "app_url", "icon_url")),
            ):
                eligible = [row for row in records if row.get("source_key") == ("bluejetty" if source == "bluejetty" else "fedora-packages")
                            and due(row, prefix, days, fields)]
                if source == "bluejetty":
                    eligible = [row for row in eligible if trusted_url(metadata_object(row.get("metadata_json")).get("record_json_url"), hostname)]
                else:
                    eligible.sort(key=lambda row: (0 if str(row.get("name") or "").casefold() in PRIORITY else 1,
                                                   str(metadata_object(row.get("metadata_json")).get("fedora_enrichment_last_attempt_at") or ""),
                                                   str(row.get("name") or "").casefold()))
                selected = eligible[:limit]
                if not selected:
                    continue
                any_selected = True
                results = await fetch_records(selected, client, parser, hostname, concurrency)
                now = datetime.now(timezone.utc).isoformat()
                updates, counts = apply_results(results, prefix, now)
                store.upsert_many(updates, rebuild_indexes=False)
                by_key = {(row["source_key"], row["external_id"]): row for row in updates}
                for index, row in enumerate(records):
                    replacement = by_key.get((row.get("source_key"), row.get("external_id")))
                    if replacement is not None:
                        records[index] = replacement
                target = totals[source]
                target["batches"] += 1
                target["selected"] += len(selected)
                for key in ("checked", "enriched", "failed"):
                    target[key] += counts[key]
                for field, count in counts["fields_filled"].items():
                    target["fields_filled"][field] = target["fields_filled"].get(field, 0) + count
                log.info("%s batch %s: selected=%s checked=%s enriched=%s failed=%s fields=%s",
                         source, batch + 1, len(selected), counts["checked"], counts["enriched"], counts["failed"], counts["fields_filled"])
            # Persist each completed batch: an Actions cancellation must not
            # discard up to nine batches of verified metadata held in memory.
            if any_selected:
                store.checkpoint_git(f"Checkpoint verified detail enrichment batch {batch + 1}")
            if not any_selected:
                break

    manifest = store.rebuild_indexes()
    store.checkpoint_git("Checkpoint verified Blue Jetty and Fedora enrichment")
    print(json.dumps({
        "enrichment": totals,
        "icon_crossmatch": cross_counts,
        "catalogue": {"record_count": manifest["record_count"], "schema_version": manifest["schema_version"]},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
