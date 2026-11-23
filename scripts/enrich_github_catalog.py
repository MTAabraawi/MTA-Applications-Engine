"""Git-backed, resumable catalogue enrichment.

This mirrors the existing SQL enrichment policy for Blue Jetty and Fedora
records while writing only to the checked-out Git JSON catalogue.
"""
from __future__ import annotations

import asyncio
import json
import logging
from html import unescape
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


def valid_https_icon(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlsplit(value.strip())
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def trusted_url(value, hostname):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and parsed.hostname == hostname and not parsed.username and not parsed.password
    except ValueError:
        return False


def parse_steam_detail(payload, row):
    app_id = str(row.get("external_id") or "")
    item = payload.get(app_id) if isinstance(payload, dict) else None
    if not isinstance(item, dict) or item.get("success") is not True or not isinstance(item.get("data"), dict):
        return {}
    data = item["data"]

    def clean(value, maximum):
        if not isinstance(value, str):
            return None
        value = unescape(re.sub(r"<[^>]*>", " ", value))
        value = " ".join(value.split()).strip()
        value = re.sub(r"\s+([,.;:!?])", r"\1", value)
        return value[:maximum] if value else None

    developer_values = data.get("developers") if isinstance(data.get("developers"), list) else []
    publisher_values = data.get("publishers") if isinstance(data.get("publishers"), list) else []
    genre_values = data.get("genres") if isinstance(data.get("genres"), list) else []
    category_values = data.get("categories") if isinstance(data.get("categories"), list) else []
    developers = [clean(value, 180) for value in developer_values if isinstance(value, str)]
    publishers = [clean(value, 180) for value in publisher_values if isinstance(value, str)]
    genres = [clean(value.get("description"), 100) for value in genre_values
              if isinstance(value, dict) and isinstance(value.get("description"), str)]
    website = data.get("website")
    app_url = None
    if isinstance(website, str):
        try:
            parsed_website = urlsplit(website.strip())
            if parsed_website.scheme == "https" and parsed_website.hostname and not parsed_website.username and not parsed_website.password:
                app_url = website.strip()
        except ValueError:
            app_url = None
    if app_url and urlsplit(app_url).hostname == "store.steampowered.com":
        app_url = None

    detail = {
        "app_id": app_id,
        "type": clean(data.get("type"), 60),
        "developers": developers,
        "publishers": publishers,
        "genres": genres,
        "categories": [clean(value.get("description"), 100) for value in category_values
                       if isinstance(value, dict) and isinstance(value.get("description"), str)],
        "platforms": data.get("platforms") if isinstance(data.get("platforms"), dict) else {},
        "release_date": data.get("release_date") if isinstance(data.get("release_date"), dict) else {},
        "website": website if isinstance(website, str) else None,
        "required_age": data.get("required_age"),
    }
    result = {
        "developer": " / ".join(value for value in developers if value) or None,
        "summary": clean(data.get("short_description"), 500),
        "description": clean(data.get("detailed_description") or data.get("about_the_game"), 5000),
        "category": ", ".join(value for value in genres if value) or None,
        "app_url": app_url,
        "metadata_json": {"steam_detail": detail},
    }
    return result


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
    metadata = record.get("metadata_json") or {}
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
        metadata = row.get("metadata_json") or {}
        if hostname == "bluejetty.ai":
            url = metadata.get("record_json_url")
        elif hostname == "store.steampowered.com":
            app_id = str(row.get("external_id") or "")
            if not app_id.isdigit():
                return row, None, ValueError("Invalid Steam app id")
            url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=us&l=en"
        else:
            url = row.get("source_url")
        if not trusted_url(url, hostname):
            return row, None, ValueError("Untrusted or missing detail URL")
        async with semaphore:
            await asyncio.sleep(0.35)
            try:
                response = await get_response(client, url, hostname)
                payload = response.json() if hostname in {"bluejetty.ai", "store.steampowered.com"} else response.text
                parsed = parser(payload, row) if hostname == "store.steampowered.com" else parser(payload)
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
        metadata = dict(item.get("metadata_json") or {})
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
        if row.get("source_key") not in official or row.get("platform") not in {"android", "ios"} or not valid_https_icon(row.get("icon_url")):
            continue
        providers.setdefault((row["platform"], str(row.get("external_id"))), {
            "source_key": row["source_key"], "icon_url": row["icon_url"]
        })
    return providers


def crossmatch_icons(records, provider_index, now):
    counts = {"scanned": 0, "matched": 0, "unmatched": 0, "ambiguous": 0}
    updates = []
    for row in records:
        if row.get("source_key") != "bluejetty" or has_value(row.get("icon_url")):
            continue
        metadata = dict(row.get("metadata_json") or {})
        candidates = []
        for platform, external_id in store_ids(metadata):
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
            # Keep ambiguous matches in the aggregate report. Do not write
            # per-record "unmatched" markers across the entire 200k catalogue.
            counts["ambiguous"] += 1
            counts["scanned"] += 1
            continue
        else:
            counts["unmatched"] += 1
            counts["scanned"] += 1
            continue
        item["metadata_json"] = metadata
        updates.append(item)
        counts["scanned"] += 1
    return updates, counts


async def main():
    store = GitHubCatalogueStore(os.getenv("GITHUB_CATALOGUE_DIR", "catalogue_data"))
    records = list(store.iter_records())
    if not records:
        raise SystemExit("GitHub catalogue is empty; run catalogue sync before enrichment.")
    blue_limit = max(1, min(int(os.getenv("BLUEJETTY_ENRICH_BATCH_SIZE", "200")), 500))
    steam_limit = max(1, min(int(os.getenv("STEAM_ENRICH_BATCH_SIZE", "100")), 100))
    fedora_limit = max(1, min(int(os.getenv("FEDORA_ENRICH_BATCH_SIZE", "25")), 100))
    concurrency = max(1, min(int(os.getenv("ENRICH_CONCURRENCY", "2")), 4))
    max_batches = max(1, min(int(os.getenv("ENRICHMENT_MAX_BATCHES", "1000")), 1000))
    runtime = max(5, min(int(os.getenv("ENRICHMENT_MAX_RUNTIME_MINUTES", "300")), 330))
    deadline = time.monotonic() + runtime * 60
    totals = {
        "bluejetty": {"batches": 0, "selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}},
        "fedora": {"batches": 0, "selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}},
        "steam": {"batches": 0, "selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}},
    }
    providers = icon_provider_index(records)
    cross_updates, cross_counts = crossmatch_icons(records, providers, datetime.now(timezone.utc).isoformat())
    if cross_updates:
        store.upsert_many(cross_updates, rebuild_indexes=False)
        cross_by_key = {(row["source_key"], row["external_id"]): row for row in cross_updates}
        for index, row in enumerate(records):
            replacement = cross_by_key.get((row.get("source_key"), row.get("external_id")))
            if replacement is not None:
                records[index] = replacement
        log.info("Exact official-store icon cross-match: %s", cross_counts)

    async with httpx.AsyncClient(follow_redirects=False) as client:
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
                ("steam", "steam", "store.steampowered.com", parse_steam_detail, steam_limit, 7,
                 ("developer", "summary", "description", "category", "app_url")),
            ):
                source_key = {"bluejetty": "bluejetty", "fedora": "fedora-packages", "steam": "steam"}[source]
                eligible = [row for row in records if row.get("source_key") == source_key
                            and due(row, prefix, days, fields)]
                if source == "bluejetty":
                    eligible = [row for row in eligible if trusted_url((row.get("metadata_json") or {}).get("record_json_url"), hostname)]
                else:
                    eligible.sort(key=lambda row: (0 if str(row.get("name") or "").casefold() in PRIORITY else 1,
                                                   str((row.get("metadata_json") or {}).get("fedora_enrichment_last_attempt_at") or ""),
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
            if not any_selected:
                break

    manifest = store.rebuild_indexes()
    print(json.dumps({
        "enrichment": totals,
        "icon_crossmatch": cross_counts,
        "catalogue": {"record_count": manifest["record_count"], "schema_version": manifest["schema_version"]},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
