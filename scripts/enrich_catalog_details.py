"""Rate-limited, resumable enrichment of existing Blue Jetty and Fedora records.

This job fetches public JSON/HTML metadata only. It never downloads installers.
Only empty database fields are filled; existing richer values are preserved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from sqlalchemy import DateTime, case, cast, func, or_, select

# Make the repository root importable when this file is executed directly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import AppRecord, SessionLocal
from fedora_catalog_source import parse_fedora_detail
from open_catalog_sources import parse_bluejetty_detail

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("mta.catalog_enrichment")
HEADERS = {"User-Agent": "MTA-Applications-Engine/1.0 (+https://github.com/MTAabraawi/MTA-Applications-Engine)"}
TIMEOUT = httpx.Timeout(35.0, connect=10.0)
FEDORA_PRIORITY_NAMES = [
    "firefox", "chromium", "google-chrome", "libreoffice", "gimp", "blender",
    "vlc", "inkscape", "krita", "thunderbird", "audacity", "obs-studio",
    "mpv", "code", "vscode", "git", "python3", "nodejs", "steam",
    "telegram-desktop", "signal-desktop", "discord", "qbittorrent",
    "transmission", "filezilla", "keepassxc", "shotcut", "kdenlive",
    "darktable", "handbrake", "wireshark", "neovim", "virt-manager",
]


def _has_value(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _merge_dict(previous, incoming):
    old = previous if isinstance(previous, dict) else {}
    new = incoming if isinstance(incoming, dict) else {}
    merged = dict(old)
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        elif isinstance(value, list) and not value and key in merged and merged[key]:
            continue
        elif _has_value(value) or key not in merged:
            merged[key] = value
    return merged


def _trusted_url(value: str | None, hostname: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def _due_for_retry(key: str, days: int):
    value = AppRecord.metadata_json[key].as_string()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return or_(value.is_(None), cast(value, DateTime(timezone=True)) < cutoff)


async def _get_response(client: httpx.AsyncClient, url: str, trusted_host: str):
    last_error = None
    for attempt in range(3):
        current_url = url
        try:
            for _redirect in range(4):
                response = await client.get(
                    current_url,
                    timeout=TIMEOUT,
                    headers=HEADERS,
                    follow_redirects=False,
                )
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        response.raise_for_status()
                    target_url = urljoin(current_url, location)
                    if not _trusted_url(target_url, trusted_host):
                        raise ValueError("Untrusted detail-page redirect target")
                    current_url = target_url
                    continue
                response.raise_for_status()
                if not _trusted_url(str(response.url), trusted_host):
                    raise ValueError("Untrusted detail-page response host")
                return response
            raise ValueError("Too many detail-page redirects")
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    raise last_error or RuntimeError("Upstream request failed")


def _missing_any(*columns):
    return or_(*(or_(column.is_(None), func.trim(column) == "") for column in columns))


async def _fetch_many(rows, client, parser, trusted_host: str, concurrency: int):
    semaphore = asyncio.Semaphore(concurrency)

    async def one(row):
        url = (row.metadata_json or {}).get("record_json_url") if trusted_host == "bluejetty.ai" else row.source_url
        if not _trusted_url(url, trusted_host):
            return row, None, ValueError("Untrusted or missing detail URL")
        async with semaphore:
            await asyncio.sleep(0.35)
            try:
                response = await _get_response(client, url, trusted_host)
                payload = response.json() if trusted_host == "bluejetty.ai" else response.text
                return row, parser(payload), None
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                return row, None, exc

    return await asyncio.gather(*(one(row) for row in rows))


def _apply_enrichment(db, results, prefix: str, now: str):
    counts = {"checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}}
    for row, data, error in results:
        counts["checked"] += 1
        metadata = dict(row.metadata_json or {})
        metadata[f"{prefix}_enrichment_last_attempt_at"] = now
        if error is not None or not isinstance(data, dict):
            metadata[f"{prefix}_enrichment_status"] = "failed"
            metadata[f"{prefix}_enrichment_error"] = type(error).__name__ if error else "invalid_payload"
            counts["failed"] += 1
            row.metadata_json = metadata
            continue

        filled = 0
        for field in ("developer", "summary", "description", "category", "license", "app_url", "icon_url"):
            value = data.get(field)
            if not _has_value(getattr(row, field)) and _has_value(value):
                setattr(row, field, value)
                counts["fields_filled"][field] = counts["fields_filled"].get(field, 0) + 1
                filled += 1

        metadata = _merge_dict(metadata, data.get("metadata_json") or {})
        metadata[f"{prefix}_enrichment_status"] = "enriched" if filled else "checked"
        metadata.pop(f"{prefix}_enrichment_error", None)
        row.metadata_json = metadata
        if filled:
            counts["enriched"] += 1
    db.commit()
    return counts


async def enrich_bluejetty(db, client, limit: int, concurrency: int):
    rows = db.execute(
        select(AppRecord)
        .where(
            AppRecord.source_key == "bluejetty",
            AppRecord.metadata_json["record_json_url"].as_string().is_not(None),
            _missing_any(
                AppRecord.developer, AppRecord.summary, AppRecord.description,
                AppRecord.category, AppRecord.app_url, AppRecord.icon_url,
            ),
            _due_for_retry("bluejetty_enrichment_last_attempt_at", 3),
        )
        .order_by(
            AppRecord.metadata_json["bluejetty_enrichment_last_attempt_at"].as_string().asc().nullsfirst(),
            AppRecord.id,
        )
        .limit(limit)
    ).scalars().all()
    if not rows:
        return {"selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}}
    results = await _fetch_many(rows, client, parse_bluejetty_detail, "bluejetty.ai", concurrency)
    counts = _apply_enrichment(db, results, "bluejetty", datetime.now(timezone.utc).isoformat())
    return {"selected": len(rows), **counts}


async def enrich_fedora(db, client, limit: int, concurrency: int):
    priority = [name.casefold() for name in FEDORA_PRIORITY_NAMES]
    priority_order = case((func.lower(AppRecord.name).in_(priority), 0), else_=1)
    rows = db.execute(
        select(AppRecord)
        .where(
            AppRecord.source_key == "fedora-packages",
            _missing_any(AppRecord.summary, AppRecord.description, AppRecord.app_url, AppRecord.icon_url),
            _due_for_retry("fedora_enrichment_last_attempt_at", 7),
        )
        .order_by(
            priority_order,
            AppRecord.metadata_json["fedora_enrichment_last_attempt_at"].as_string().asc().nullsfirst(),
            AppRecord.name,
            AppRecord.id,
        )
        .limit(limit)
    ).scalars().all()
    if not rows:
        return {"selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}}
    results = await _fetch_many(rows, client, parse_fedora_detail, "packages.fedoraproject.org", concurrency)
    counts = _apply_enrichment(db, results, "fedora", datetime.now(timezone.utc).isoformat())
    return {"selected": len(rows), **counts}



def _bluejetty_store_ids(value, depth: int = 0) -> set[tuple[str, str]]:
    """Extract exact Google Play package IDs and Apple App Store track IDs."""
    if depth > 12:
        return set()
    found: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(_bluejetty_store_ids(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.update(_bluejetty_store_ids(child, depth + 1))
    elif isinstance(value, str) and value.startswith("https://"):
        try:
            parsed = urlsplit(value)
            if parsed.hostname == "play.google.com" and parsed.path.rstrip("/") == "/store/apps/details":
                package = (parse_qs(parsed.query).get("id") or [""])[0].strip()
                if package:
                    found.add(("android", package))
            elif parsed.hostname == "apps.apple.com":
                import re
                match = re.search(r"/id(\d+)(?:/|$)", parsed.path)
                if match:
                    found.add(("ios", match.group(1)))
        except ValueError:
            pass
    return found


def _bluejetty_icon_candidates(metadata: dict, platform: str, provider_index: dict):
    candidates = []
    for store_platform, external_id in sorted(_bluejetty_store_ids(metadata)):
        if platform in {"android", "ios"} and store_platform != platform:
            continue
        provider = provider_index.get((store_platform, external_id))
        if provider and provider.get("icon_url"):
            candidates.append((store_platform, external_id, provider["source_key"], provider["icon_url"]))
    return candidates


def _resolve_bluejetty_icon(metadata: dict, platform: str, provider_index: dict):
    """Resolve an icon only when an official store listing ID matches exactly."""
    candidates = _bluejetty_icon_candidates(metadata, platform, provider_index)
    if len({item[3] for item in candidates}) != 1:
        return None
    return sorted(candidates)[0]


def _load_icon_provider_index(db):
    """Index existing official-store icon records; performs no external API calls."""
    rows = db.execute(
        select(AppRecord.source_key, AppRecord.external_id, AppRecord.platform, AppRecord.icon_url)
        .where(
            AppRecord.source_key.in_(("google-play", "apple-itunes", "apple-appbrain", "fdroid", "izzyondroid")),
            AppRecord.icon_url.is_not(None),
            func.trim(AppRecord.icon_url) != "",
        )
    ).all()
    index = {}
    for source_key, external_id, platform, icon_url in rows:
        if platform not in {"android", "ios"} or not external_id or not icon_url:
            continue
        index.setdefault((platform, str(external_id)), {"source_key": source_key, "icon_url": icon_url})
    return index


def _crossmatch_bluejetty_icons(db, provider_index: dict, limit: int, now: str):
    """Backfill icons from existing providers using exact official store IDs only."""
    rows = db.execute(
        select(AppRecord)
        .where(
            AppRecord.source_key == "bluejetty",
            _missing_any(AppRecord.icon_url),
            _due_for_retry("icon_crossmatch_last_attempt_at", 7),
        )
        .order_by(
            AppRecord.metadata_json["icon_crossmatch_last_attempt_at"].as_string().asc().nullsfirst(),
            AppRecord.id,
        )
        .limit(limit)
    ).scalars().all()
    counts = {"scanned": len(rows), "matched": 0, "unmatched": 0, "ambiguous": 0}
    for row in rows:
        metadata = dict(row.metadata_json or {})
        candidates = _bluejetty_icon_candidates(metadata, row.platform, provider_index)
        match = _resolve_bluejetty_icon(metadata, row.platform, provider_index)
        metadata["icon_crossmatch_last_attempt_at"] = now
        if match:
            store_platform, external_id, source_key, icon_url = match
            row.icon_url = icon_url
            metadata["icon_crossmatch"] = {
                "status": "matched",
                "method": "exact_official_store_listing_id",
                "store_platform": store_platform,
                "store_external_id": external_id,
                "provider_source": source_key,
                "matched_at": now,
            }
            counts["matched"] += 1
        elif len({item[3] for item in candidates}) > 1:
            metadata["icon_crossmatch"] = {
                "status": "ambiguous",
                "method": "exact_official_store_listing_id",
                "checked_at": now,
            }
            counts["ambiguous"] += 1
        else:
            metadata["icon_crossmatch"] = {
                "status": "unmatched",
                "method": "exact_official_store_listing_id",
                "checked_at": now,
            }
            counts["unmatched"] += 1
        row.metadata_json = metadata
    if rows:
        db.commit()
    return counts


def _quality_snapshot(db):
    """Return comparable catalogue completeness counts by source and field."""
    fields = ("icon_url", "description", "summary", "developer", "category", "license", "app_url", "source_url")
    expressions = []
    for field in fields:
        column = getattr(AppRecord, field)
        normalized = func.lower(func.trim(func.coalesce(column, "")))
        condition = normalized.like("https://%") if field.endswith("_url") else func.length(normalized) > 0
        expressions.append(func.sum(case((condition, 1), else_=0)).label(field))
    rows = db.execute(
        select(AppRecord.source_key, func.count().label("total"), *expressions)
        .group_by(AppRecord.source_key).order_by(AppRecord.source_key)
    ).all()
    result = {"total": 0, "sources": {}}
    for row in rows:
        source, total = row[0], int(row[1] or 0)
        counts = {field: int(row[index + 2] or 0) for index, field in enumerate(fields)}
        result["total"] += total
        result["sources"][source] = {
            "total": total,
            "fields": {
                field: {
                    "present": count,
                    "missing": max(total - count, 0),
                    "percent": round(count * 100.0 / total, 2) if total else 0.0,
                }
                for field, count in counts.items()
            },
        }
    return result


def _quality_delta(before, after):
    """Calculate absolute changes in source totals and populated fields."""
    delta = {"total": after["total"] - before["total"], "sources": {}}
    for source in sorted(set(before["sources"]) | set(after["sources"])):
        old = before["sources"].get(source, {"total": 0, "fields": {}})
        new = after["sources"].get(source, {"total": 0, "fields": {}})
        fields = {}
        for field in sorted(set(old["fields"]) | set(new["fields"])):
            old_count = old["fields"].get(field, {}).get("present", 0)
            new_count = new["fields"].get(field, {}).get("present", 0)
            fields[field] = new_count - old_count
        delta["sources"][source] = {
            "total": new["total"] - old["total"],
            "fields_filled": fields,
        }
    return delta

def _empty_totals():
    return {"batches": 0, "selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}}


def _add_totals(total: dict, batch: dict):
    total["batches"] += 1
    for key in ("selected", "checked", "enriched", "failed"):
        total[key] += int(batch.get(key, 0) or 0)
    for field, count in (batch.get("fields_filled") or {}).items():
        total["fields_filled"][field] = total["fields_filled"].get(field, 0) + int(count or 0)


async def main():
    db = SessionLocal()
    if db.get_bind().dialect.name != "postgresql":
        db.close()
        raise SystemExit("The scheduled enrichment job requires the shared PostgreSQL DATABASE_URL.")
    bluejetty_limit = max(1, min(int(os.getenv("BLUEJETTY_ENRICH_BATCH_SIZE", "200")), 500))
    fedora_limit = max(1, min(int(os.getenv("FEDORA_ENRICH_BATCH_SIZE", "25")), 100))
    concurrency = max(1, min(int(os.getenv("ENRICH_CONCURRENCY", "2")), 4))
    max_batches = max(1, min(int(os.getenv("ENRICHMENT_MAX_BATCHES", "4")), 10))
    totals = {"bluejetty": _empty_totals(), "fedora": _empty_totals()}
    icon_crossmatch_totals = {"scanned": 0, "matched": 0, "unmatched": 0, "ambiguous": 0}
    try:
        quality_before = _quality_snapshot(db)
        provider_index = _load_icon_provider_index(db)
        log.info("Exact store-ID icon providers available: %d", len(provider_index))
        stopped_early = False
        batches_run = 0
        async with httpx.AsyncClient(follow_redirects=False) as client:
            for batch_number in range(1, max_batches + 1):
                batches_run = batch_number
                bluejetty = await enrich_bluejetty(db, client, bluejetty_limit, concurrency)
                fedora = await enrich_fedora(db, client, fedora_limit, concurrency)
                crossmatch = _crossmatch_bluejetty_icons(
                    db, provider_index,
                    max(1, min(int(os.getenv("BLUEJETTY_ICON_CROSSMATCH_BATCH_SIZE", "5000")), 10000)),
                    datetime.now(timezone.utc).isoformat(),
                )
                for key in icon_crossmatch_totals:
                    icon_crossmatch_totals[key] += crossmatch.get(key, 0)
                _add_totals(totals["bluejetty"], bluejetty)
                _add_totals(totals["fedora"], fedora)
                log.info(
                    "Enrichment batch %s/%s: Blue Jetty selected=%s, Fedora selected=%s, exact icon crossmatch scanned=%s matched=%s ambiguous=%s",
                    batch_number, max_batches, bluejetty.get("selected", 0), fedora.get("selected", 0),
                    crossmatch.get("scanned", 0), crossmatch.get("matched", 0), crossmatch.get("ambiguous", 0),
                )
                if not bluejetty.get("selected") and not fedora.get("selected") and not crossmatch.get("scanned"):
                    stopped_early = True
                    break
        totals["max_batches_reached"] = not stopped_early and batches_run >= max_batches
        quality_after = _quality_snapshot(db)
        report = {
            "enrichment": totals,
            "exact_store_icon_crossmatch": icon_crossmatch_totals,
            "catalog_quality_before": quality_before,
            "catalog_quality_after": quality_after,
            "catalog_quality_delta": _quality_delta(quality_before, quality_after),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
