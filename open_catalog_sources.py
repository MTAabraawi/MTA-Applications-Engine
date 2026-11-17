"""Free public catalogue connectors for Blue Jetty, OpenUse and Chocolatey.

Blue Jetty and OpenUse are read-only public APIs. Chocolatey is queried only
through its supported Chocolatey CLI (never by unsupported direct OData calls).
Only listing metadata and catalogue URLs are stored; installers are not fetched.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from catalog_sources import _https, _record, _unique

log = logging.getLogger("mta.catalogs.open")
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
HEADERS = {
    "User-Agent": "MTA-Applications-Engine/1.0 (+https://github.com/MTAabraawi/MTA-Applications-Engine)",
    "Accept": "application/json",
}


async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Retry transient upstream failures and respect Retry-After when supplied."""
    response = None
    for attempt in range(4):
        try:
            response = await client.get(url, **kwargs)
        except httpx.HTTPError:
            if attempt == 3:
                raise
            await asyncio.sleep(min(2 ** attempt, 20))
            continue
        status = getattr(response, "status_code", 200)
        if status == 429 or status in (500, 502, 503, 504):
            if attempt == 3:
                return response
            headers = getattr(response, "headers", {}) or {}
            retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
            try:
                delay = float(retry_after) if retry_after is not None else float(2 ** attempt)
            except (TypeError, ValueError):
                delay = float(2 ** attempt)
            await asyncio.sleep(min(max(delay, 1.0), 30.0))
            continue
        return response
    return response


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _platform(value: Any) -> str | None:
    raw = str(value or "").strip().casefold()
    aliases = {
        "ios": "ios", "iphone": "ios", "ipad": "ios", "ipados": "ios",
        "android": "android", "windows": "windows", "win": "windows",
        "macos": "macos", "mac": "macos", "osx": "macos",
        "linux": "linux", "web": "web", "browser": "web",
    }
    return aliases.get(raw)


def _walk_store_urls(value: Any, depth: int = 0) -> list[tuple[str, str]]:
    """Find explicitly supplied store listing URLs without guessing destinations."""
    if depth > 8:
        return []
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and child.startswith("https://"):
                host = (urlparse(child).hostname or "").casefold()
                platform = None
                if host == "play.google.com" or host.endswith(".play.google.com"):
                    platform = "android"
                elif host == "apps.apple.com" or host.endswith(".apps.apple.com"):
                    platform = "ios"
                elif any(token in key.casefold() for token in ("store", "listing", "destination", "trackview")) or key.casefold() in {"url", "href", "link"}:
                    platform = _platform(value.get("platform") or value.get("os"))
                if platform:
                    found.append((platform, child))
            elif isinstance(child, (dict, list)):
                found.extend(_walk_store_urls(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_store_urls(child, depth + 1))
    return list(dict.fromkeys(found))


def parse_bluejetty_detail(payload: Any) -> dict[str, Any]:
    """Map the public Blue Jetty detail envelope into safe catalogue fields."""
    detail = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(detail, dict):
        return {}
    observations = detail.get("storeObservations") or []
    if not isinstance(observations, list):
        observations = []
    observation = next((item for item in observations if isinstance(item, dict)), {})
    description = detail.get("description") or observation.get("description")
    summary = detail.get("summary") or detail.get("shortDescription") or detail.get("tagline")
    if not summary and isinstance(detail.get("description"), str):
        summary = detail["description"][:240]
    website = detail.get("websiteUrl") or detail.get("website") or detail.get("homepage")
    icon = detail.get("iconUrl") or detail.get("icon_url") or detail.get("icon") or detail.get("artworkUrl512")
    metadata = {
        "bluejetty_detail": {
            "record_modified_at": detail.get("recordModifiedAt"),
            "canonical_page_url": _https(detail.get("canonicalPageUrl")),
            "packet_url": _https(detail.get("packetUrl")),
            "publisher_authorization": detail.get("publisherAuthorization"),
            "platforms": detail.get("platforms") if isinstance(detail.get("platforms"), list) else [],
            "store_observations": observations,
            "provenance": detail.get("provenance"),
        }
    }
    return {
        "developer": detail.get("developerName") or detail.get("developer") or detail.get("publisher"),
        "summary": summary,
        "description": description,
        "category": detail.get("category") or observation.get("category"),
        "app_url": _https(website),
        "icon_url": _https(icon),
        "metadata_json": metadata,
    }


async def fetch_bluejetty(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Traverse Blue Jetty's public compact app index, following meta.next verbatim.

    The index is a focused mobile-app corpus, not a complete copy of either app
    store. Each source record retains Blue Jetty provenance and any explicit
    official store destinations found in the public index entry.
    """
    max_apps = max(100, min(int(os.getenv("BLUEJETTY_MAX_APPS", "200000")), 300000))
    page_size = 100
    page_delay = max(0.25, min(float(os.getenv("BLUEJETTY_PAGE_DELAY", "0.55")), 5.0))
    url = "https://bluejetty.ai/v1/apps?limit=100"
    seen_apps: set[str] = set()
    records: list[dict[str, Any]] = []
    pages = 0
    restart_used = False

    while url and len(seen_apps) < max_apps:
        try:
            response = await _get_with_retry(client, url, timeout=TIMEOUT, headers=HEADERS)
            if response.status_code == 409 and not restart_used:
                restart_used = True
                url = "https://bluejetty.ai/v1/apps?limit=100"
                continue
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Blue Jetty documents BJ_INDEX_RESTART_REQUIRED for an expired
            # snapshot cursor. Restart once; other failures remain source-local.
            if not restart_used and "BJ_INDEX_RESTART_REQUIRED" in str(exc):
                restart_used = True
                url = "https://bluejetty.ai/v1/apps?limit=100"
                continue
            log.warning("Blue Jetty index stopped after %d pages: %s", pages, exc)
            break

        items = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            log.warning("Blue Jetty returned an unrecognised index page at %s", url)
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            app_id = str(item.get("blueJettyId") or item.get("blue_jetty_id") or item.get("id") or item.get("appId") or "").strip()
            name = _text(item.get("name") or item.get("title") or item.get("appName"))
            if not app_id or not name or app_id in seen_apps:
                continue
            seen_apps.add(app_id)
            json_url = _text(item.get("jsonUrl") or item.get("json_url"))
            if json_url and json_url.startswith("/"):
                json_url = urljoin("https://bluejetty.ai", json_url)
            destinations = _walk_store_urls(item)
            platform_values = item.get("platforms") or item.get("platform") or []
            if isinstance(platform_values, str):
                platform_values = [platform_values]
            platforms = list(dict.fromkeys(
                p for p in (_platform(x.get("platform") or x.get("os") if isinstance(x, dict) else x)
                            for x in platform_values if x is not None) if p
            )) if isinstance(platform_values, list) else []
            for p, _ in destinations:
                if p not in platforms:
                    platforms.append(p)
            if not platforms:
                platforms = ["mobile"]
            # Create one record per explicitly evidenced store destination; if
            # no store URL is present, retain a provenance-linked index record.
            targets = destinations or [(p, json_url or f"https://bluejetty.ai/v1/index/apps/{app_id}") for p in platforms]
            for index, (platform, listing_url) in enumerate(targets):
                if not listing_url or not listing_url.startswith("https://"):
                    continue
                source_url = listing_url
                external_id = f"{app_id}:{platform}:{index}" if len(targets) > 1 else f"{app_id}:{platform}"
                records.append(_record(
                    "bluejetty", external_id, platform, name, source_url,
                    developer=item.get("developer") or item.get("publisher"),
                    summary=item.get("summary") or item.get("tagline"),
                    description=item.get("description"),
                    category=item.get("category") or item.get("primaryCategory"),
                    app_url=item.get("websiteUrl") or item.get("website") or (listing_url if destinations else None),
                    icon_url=item.get("iconUrl") or item.get("icon_url") or item.get("icon"),
                    metadata_json={
                        "catalog_type": "mobile-app-index",
                        "bluejetty_id": app_id,
                        "record_json_url": json_url,
                        "packet_url": item.get("packetUrl") or item.get("packet_url"),
                        "platforms": platforms,
                        "store_destinations": [{"platform": p, "url": u} for p, u in destinations],
                        "source_observation": item.get("lastSuccessfulCheckAt") or item.get("sourceObservedAt") or item.get("updatedAt") or item.get("recordModifiedAt"),
                        "source_provider": "Blue Jetty public app index",
                        "bluejetty_html_url": item.get("htmlUrl"),
                        "publisher_website": item.get("websiteUrl"),
                        "provenance": item.get("provenance"),
                        "attribution": item.get("attribution") or [],
                        "source_observed_at": item.get("sourceObservedAt"),
                        "record_updated_at": item.get("updatedAt"),
                        "last_successful_check_at": item.get("lastSuccessfulCheckAt"),
                        "record_format": (payload.get("meta") or {}).get("format") if isinstance(payload.get("meta"), dict) else None,
                    },
                ))
        pages += 1
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        next_url = meta.get("next") if isinstance(meta, dict) else None
        if isinstance(next_url, str) and next_url.startswith("/"):
            next_url = urljoin("https://bluejetty.ai", next_url)
        url = next_url if isinstance(next_url, str) and next_url.startswith("https://") else None
        if pages % 25 == 0:
            log.info("Blue Jetty index progress: pages=%d apps=%d records=%d", pages, len(seen_apps), len(records))
        await asyncio.sleep(0.20)

    log.info("Blue Jetty index complete/limited: pages=%d apps=%d records=%d max_apps=%d",
             pages, len(seen_apps), len(records), max_apps)
    return _unique(records)


async def fetch_openuse(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Import OpenUse's complete paginated public app catalogue (no API key)."""
    max_apps = max(100, min(int(os.getenv("OPENUSE_MAX_APPS", "50000")), 200000))
    page_size = 100
    offset = 0
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    while offset < max_apps:
        try:
            response = await _get_with_retry(
                client, "https://api.openuse.app/v1/public/apps",
                params={"limit": page_size, "offset": offset},
                timeout=TIMEOUT,
                headers=HEADERS,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("OpenUse catalogue stopped at offset %d: %s", offset, exc)
            break
        items = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            slug = _text(item.get("slug") or item.get("id"))
            name = _text(item.get("name") or item.get("title"))
            if not slug or not name:
                continue
            platforms = item.get("platforms") or []
            if isinstance(platforms, str):
                platforms = [platforms]
            mapped = list(dict.fromkeys(p for p in (_platform(x) for x in platforms) if p))
            if not mapped:
                mapped = ["multi"]
            page_url = _text(item.get("url")) or f"https://openuse.app/apps/{slug}"
            if page_url.startswith("/"):
                page_url = urljoin("https://openuse.app", page_url)
            if not page_url.startswith("https://"):
                page_url = f"https://openuse.app/apps/{slug}"
            for platform in mapped:
                key = (slug, platform)
                if key in seen:
                    continue
                seen.add(key)
                records.append(_record(
                    "openuse", f"{slug}:{platform}", platform, name, page_url,
                    developer=item.get("author") or item.get("developer"),
                    summary=item.get("tagline") or item.get("summary"),
                    description=item.get("description"),
                    category=", ".join(item.get("categories", [])) if isinstance(item.get("categories"), list) else item.get("category"),
                    license=item.get("license"),
                    app_url=item.get("homepage") or item.get("website") or item.get("repository") or item.get("sourceUrl"),
                    icon_url=item.get("icon") or item.get("iconUrl"),
                    metadata_json={
                        "catalog_type": item.get("kind") or "app",
                        "slug": slug,
                        "platforms": mapped,
                        "open_source": item.get("openSource"),
                        "latest_version": item.get("latestVersion"),
                        "stars": item.get("stars"),
                        "alternative_to": item.get("alternativeTo") or [],
                        "official_source": item.get("source") or item.get("repository"),
                        "openuse_page": page_url,
                    },
                ))
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        total = meta.get("total") if isinstance(meta, dict) else None
        offset += len(items)
        if len(items) < page_size or (isinstance(total, int) and offset >= total):
            break
        await asyncio.sleep(0.55)

    log.info("OpenUse catalogue: %d platform records from %d app entries (cap=%d)",
             len(records), len({slug for slug, _ in seen}), max_apps)
    return _unique(records)


def _chocolatey_search_page(choco: str, page: int, page_size: int) -> list[tuple[str, str]]:
    command = [
        choco, "search",
        f"--page={page}",
        f"--page-size={page_size}",
        "--limit-output",
        "--approved-only",
        "--no-color",
        "--source=https://community.chocolatey.org/api/v2",
        "--execution-timeout=180",
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=210, check=False,
        encoding="utf-8", errors="replace",
    )
    if completed.returncode not in (0, 2):
        message = (completed.stderr or completed.stdout or "Chocolatey CLI failed").strip()
        raise RuntimeError(message[-1000:])
    results = []
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        package_id, version = line.split("|", 1)
        package_id = package_id.strip()
        version = version.strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}", package_id):
            results.append((package_id, version))
    return results


async def fetch_chocolatey(client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    """Use the supported Chocolatey CLI search command on Windows runners.

    Chocolatey documents CLI/client querying as the supported way to query its
    Community Repository. Direct custom OData scraping is intentionally avoided.
    """
    choco = shutil.which("choco") or shutil.which("choco.exe")
    if not choco:
        log.info("Chocolatey connector skipped: Chocolatey CLI is not installed on this runner")
        return []
    page_size = max(25, min(int(os.getenv("CHOCOLATEY_PAGE_SIZE", "100")), 100))
    max_pages = max(1, min(int(os.getenv("CHOCOLATEY_MAX_PAGES", "10")), 10))
    found: dict[str, tuple[str, str]] = {}
    for page in range(max_pages):
        try:
            items = await asyncio.to_thread(_chocolatey_search_page, choco, page, page_size)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            log.warning("Chocolatey CLI search failed on page %d: %s", page, exc)
            break
        for package_id, version in items:
            found[package_id.casefold()] = (package_id, version)
        if len(items) < page_size:
            break
    records = []
    for package_id, version in found.values():
        display_id = package_id
        records.append(_record(
            "chocolatey", package_id, "windows", display_id,
            f"https://community.chocolatey.org/packages/{package_id}",
            app_url=f"https://community.chocolatey.org/packages/{package_id}",
            metadata_json={
                "catalog_type": "chocolatey-community-package",
                "version": version,
                "approved_only": True,
                "source_method": "supported Chocolatey CLI search",
            },
        ))
    log.info("Chocolatey Community Repository: %d packages", len(records))
    return _unique(records)
