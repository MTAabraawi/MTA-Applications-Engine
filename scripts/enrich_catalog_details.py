"""Rate-limited, resumable enrichment of existing Blue Jetty and Fedora records.

This job fetches public JSON/HTML metadata only. It never downloads installers.
Only empty database fields are filled; existing richer values are preserved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from sqlalchemy import DateTime, case, cast, func, or_, select

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


async def _get_response(client: httpx.AsyncClient, url: str):
    last_error = None
    for attempt in range(3):
        try:
            response = await client.get(url, timeout=TIMEOUT, headers=HEADERS)
            if response.status_code == 429 or response.status_code in {500, 502, 503, 504}:
                if attempt < 2:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = min(max(float(retry_after), 1.0), 20.0) if retry_after else 2 ** attempt
                    except ValueError:
                        delay = 2 ** attempt
                    await asyncio.sleep(delay)
                    continue
            response.raise_for_status()
            return response
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
                response = await _get_response(client, url)
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
            _due_for_retry("bluejetty_enrichment_last_attempt_at", 7),
        )
        .order_by(AppRecord.id)
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
            _missing_any(AppRecord.summary, AppRecord.description),
            _due_for_retry("fedora_enrichment_last_attempt_at", 30),
        )
        .order_by(priority_order, AppRecord.name, AppRecord.id)
        .limit(limit)
    ).scalars().all()
    if not rows:
        return {"selected": 0, "checked": 0, "enriched": 0, "failed": 0, "fields_filled": {}}
    results = await _fetch_many(rows, client, parse_fedora_detail, "packages.fedoraproject.org", concurrency)
    counts = _apply_enrichment(db, results, "fedora", datetime.now(timezone.utc).isoformat())
    return {"selected": len(rows), **counts}


async def main():
    db = SessionLocal()
    if db.get_bind().dialect.name != "postgresql":
        db.close()
        raise SystemExit("The scheduled enrichment job requires the shared PostgreSQL DATABASE_URL.")
    bluejetty_limit = max(1, min(int(os.getenv("BLUEJETTY_ENRICH_BATCH_SIZE", "200")), 500))
    fedora_limit = max(1, min(int(os.getenv("FEDORA_ENRICH_BATCH_SIZE", "25")), 100))
    concurrency = max(1, min(int(os.getenv("ENRICH_CONCURRENCY", "2")), 4))
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            bluejetty = await enrich_bluejetty(db, client, bluejetty_limit, concurrency)
            fedora = await enrich_fedora(db, client, fedora_limit, concurrency)
        print(json.dumps({"bluejetty": bluejetty, "fedora": fedora}, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
