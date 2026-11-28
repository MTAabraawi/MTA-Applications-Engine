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
            delay = max(0.0, min(float(os.getenv("ENRICH_REQUEST_DELAY", "0.15")), 2.0))
            await asyncio.sleep(delay)
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
    """Match only exact Apple listing IDs where a matching Apple icon provider exists.

    The previous implementation recursively walked metadata for every missing
    Android/iOS record. It spent hours scanning nearly 200k Blue Jetty rows,
    even though there was no Google Play provider and it produced zero matches.
    Blue Jetty records retain their exact store URL in source_url when present,
    so this fast path only examines iOS rows with an explicit App Store URL.
    """
    counts = {"scanned": 0, "matched": 0, "unmatched": 0, "ambiguous": 0}
    updates = []
    if not any(platform == "ios" for platform, _external_id in provider_index):
        return updates, counts

    excluded = {"google-play", "apple-itunes", "apple-appbrain", "fdroid", "izzyondroid"}
    for row in records:
        if has_value(row.get("icon_url")) or row.get("platform") != "ios":
            continue
        if row.get("source_key") in excluded:
            continue
        urls = (row.get("source_url"), row.get("app_url"))
        if not any(isinstance(value, str) and "apps.apple.com/" in value for value in urls):
            continue

        candidates = []
        for platform, external_id in store_ids(urls):
            if platform != "ios":
                continue
            provider = provider_index.get((platform, external_id))
            if provider:
                candidates.append((platform, external_id, provider["source_key"], provider["icon_url"]))
        unique_icons = {item[3] for item in candidates}
        counts["scanned"] += 1
        if not unique_icons:
            counts["unmatched"] += 1
            continue

        item = dict(row)
        metadata = metadata_object(item.get("metadata_json"))
        if len(unique_icons) == 1:
            platform, external_id, source, icon = sorted(candidates)[0]
            item["icon_url"] = icon
            metadata["icon_crossmatch"] = {
                "status": "matched",
                "method": "exact_apple_app_store_listing_id",
                "store_platform": platform,
                "store_external_id": external_id,
                "provider_source": source,
                "matched_at": now,
            }
            counts["matched"] += 1
        else:
            metadata["icon_crossmatch"] = {
                "status": "ambiguous",
                "method": "exact_apple_app_store_listing_id",
                "checked_at": now,
            }
            counts["ambiguous"] += 1
        metadata["icon_crossmatch_last_attempt_at"] = now
        item["metadata_json"] = metadata
        updates.append(item)
    return updates, counts

async def main():
    store = GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    records = list(store.iter_records())
    if not records:
        raise SystemExit("GitHub catalogue is empty; run catalogue sync before enrichment.")

    blue_limit = max(1, min(int(os.getenv("BLUEJETTY_ENRICH_BATCH_SIZE", "200")), 500))
    fedora_limit = max(1, min(int(os.getenv("FEDORA_ENRICH_BATCH_SIZE", "300")), 500))
    concurrency = max(1, min(int(os.getenv("ENRICH_CONCURRENCY", "8")), 12))
    max_batches = max(1, min(int(os.getenv("ENRICHMENT_MAX_BATCHES", "1000")), 1000))
    checkpoint_every = max(1, min(int(os.getenv("ENRICH_CHECKPOINT_BATCHES", "10")), 100))
    runtime = max(5, min(int(os.getenv("ENRICHMENT_MAX_RUNTIME_MINUTES", "300")), 330))
    deadline = time.monotonic() + runtime * 60

    totals = {
        "bluejetty": {"candidates": 0, "batches": 0, "selected": 0, "checked": 0, "enriched": 0, "failed": 0, "remaining": 0, "fields_filled": {}},
        "fedora": {"candidates": 0, "batches": 0, "selected": 0, "checked": 0, "enriched": 0, "failed": 0, "remaining": 0, "fields_filled": {}},
    }
    record_positions = {
        (row.get("source_key"), row.get("external_id")): index
        for index, row in enumerate(records)
    }

    providers = icon_provider_index(records)
    cross_updates, cross_counts = crossmatch_icons(records, providers, datetime.now(timezone.utc).isoformat())
    if cross_updates:
        store.upsert_many(cross_updates, rebuild_indexes=False)
        for replacement in cross_updates:
            index = record_positions.get((replacement.get("source_key"), replacement.get("external_id")))
            if index is not None:
                records[index] = replacement
        store.checkpoint_git("Checkpoint exact official-store icon crossmatch")
    log.info("Fast exact Apple icon cross-match: %s", cross_counts)

    # Build each source queue once. The previous loop rescanned all 366k records
    # for every batch and then rescanned them again to replace each updated row.
    blue_candidates = [
        row for row in records
        if row.get("source_key") == "bluejetty"
        and due(row, "bluejetty", 3, ("developer", "summary", "description", "category", "app_url", "icon_url"))
        and trusted_url(metadata_object(row.get("metadata_json")).get("record_json_url"), "bluejetty.ai")
    ]
    fedora_candidates = [
        row for row in records
        if row.get("source_key") == "fedora-packages"
        and due(row, "fedora", 7, ("summary", "description", "app_url", "icon_url"))
    ]
    fedora_candidates.sort(key=lambda row: (
        0 if str(row.get("name") or "").casefold() in PRIORITY else 1,
        str(metadata_object(row.get("metadata_json")).get("fedora_enrichment_last_attempt_at") or ""),
        str(row.get("name") or "").casefold(),
    ))
    queues = {"bluejetty": blue_candidates, "fedora": fedora_candidates}
    cursors = {"bluejetty": 0, "fedora": 0}
    totals["bluejetty"]["candidates"] = len(blue_candidates)
    totals["fedora"]["candidates"] = len(fedora_candidates)
    log.info("Source-specific candidate queues: Blue Jetty=%s, Fedora=%s", len(blue_candidates), len(fedora_candidates))

    source_specs = (
        ("bluejetty", "bluejetty", "bluejetty.ai", parse_bluejetty_detail, blue_limit),
        ("fedora", "fedora", "packages.fedoraproject.org", parse_fedora_detail, fedora_limit),
    )

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for batch in range(max_batches):
            if time.monotonic() >= deadline:
                log.warning("Enrichment runtime limit reached; saved progress is resumable.")
                break

            any_selected = False
            for source, prefix, hostname, parser, limit in source_specs:
                start_at = cursors[source]
                selected = queues[source][start_at:start_at + limit]
                cursors[source] += len(selected)
                if not selected:
                    continue

                any_selected = True
                results = await fetch_records(selected, client, parser, hostname, concurrency)
                now = datetime.now(timezone.utc).isoformat()
                updates, counts = apply_results(results, prefix, now)
                store.upsert_many(updates, rebuild_indexes=False)

                for replacement in updates:
                    index = record_positions.get((replacement.get("source_key"), replacement.get("external_id")))
                    if index is not None:
                        records[index] = replacement

                target = totals[source]
                target["batches"] += 1
                target["selected"] += len(selected)
                for key in ("checked", "enriched", "failed"):
                    target[key] += counts[key]
                for field, count in counts["fields_filled"].items():
                    target["fields_filled"][field] = target["fields_filled"].get(field, 0) + count

                log.info(
                    "%s batch %s: selected=%s checked=%s enriched=%s failed=%s fields=%s",
                    source, batch + 1, len(selected), counts["checked"], counts["enriched"],
                    counts["failed"], counts["fields_filled"],
                )

            if not any_selected:
                break

            queues_remaining = any(cursors[key] < len(queues[key]) for key in queues)
            if (batch + 1) % checkpoint_every == 0 or not queues_remaining:
                store.checkpoint_git(f"Checkpoint verified source enrichment batch {batch + 1}")

    for source in ("bluejetty", "fedora"):
        totals[source]["remaining"] = max(
            len(queues[source]) - cursors[source], 0
        )

    manifest = store.rebuild_indexes()
    store.checkpoint_git("Checkpoint verified Blue Jetty and Fedora enrichment")

    report = {
        "enrichment": totals,
        "icon_crossmatch": cross_counts,
        "catalogue": {"record_count": manifest["record_count"], "schema_version": manifest["schema_version"]},
        "runtime_limit_minutes": runtime,
        "checkpoint_interval_batches": checkpoint_every,
    }
    report_dir = Path(os.getenv("MTA_ENRICHMENT_REPORT_DIR", "run-reports"))
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "source-specific-summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
