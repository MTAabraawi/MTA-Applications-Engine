"""Independent metadata connectors for public software catalogues.

Only metadata and official catalogue/product URLs are stored. Installer binaries
are never downloaded, mirrored, or hosted by this service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("mta.catalogs")
DATA_DIR = Path(__file__).resolve().parent
WEB_APPS_PATH = DATA_DIR / "web_apps.json"

APPLE_COUNTRY = os.getenv("APPLE_COUNTRY", "us")
APPLE_SEARCH_TERMS = [x.strip() for x in os.getenv(
    "APPLE_SEARCH_TERMS",
    "browser,office,photo,video,education,security,finance,communication,developer,utilities,messaging,maps,health,weather",
).split(",") if x.strip()]
WINGET_SEARCH_TERMS = [x.strip() for x in os.getenv(
    "WINGET_SEARCH_TERMS",
    "browser,office,photo,video,education,security,finance,communication,developer,utilities",
).split(",") if x.strip()]
APPLE_LIMIT = max(1, min(int(os.getenv("APPLE_LIMIT", "100")), 200))
WINGET_LIMIT = max(1, min(int(os.getenv("WINGET_LIMIT", "24")), 24))
FLATHUB_MAX_APPS = max(1, min(int(os.getenv("FLATHUB_MAX_APPS", "1000")), 5000))
CONCURRENCY = max(1, min(int(os.getenv("CATALOG_CONCURRENCY", "6")), 12))
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HEADERS = {"User-Agent": "MTA-Applications-Engine/0.2 (+https://github.com/MTAabraawi/MTA-Applications-Engine)"}


def _https(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value.startswith("https://") else None


def _record(source_key: str, external_id: Any, platform: str, name: Any,
            source_url: Any, **kwargs: Any) -> dict[str, Any] | None:
    external_id = str(external_id or "").strip()
    name = str(name or "").strip()
    source_url = _https(source_url)
    if not external_id or not name or not source_url:
        return None
    result = {
        "source_key": source_key, "external_id": external_id,
        "platform": platform, "name": name[:500],
        "developer": kwargs.get("developer"),
        "summary": kwargs.get("summary"),
        "description": kwargs.get("description"),
        "category": kwargs.get("category"),
        "license": kwargs.get("license"),
        "source_url": source_url,
        "app_url": _https(kwargs.get("app_url")),
        "icon_url": _https(kwargs.get("icon_url")),
        "metadata_json": kwargs.get("metadata_json") or {},
    }
    for key in ("developer", "summary", "description", "category", "license"):
        if result[key] is not None:
            result[key] = str(result[key])[:10000]
    return result


def _unique(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output = []
    for record in records:
        key = (record["source_key"], record["external_id"])
        if key not in seen:
            seen.add(key)
            output.append(record)
    return output


async def _get_json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response.json()


async def fetch_flathub(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch Flathub AppStream entries; tolerate documented list/object envelopes."""
    try:
        payload = await _get_json(client, "https://flathub.org/api/v2/appstream",
                                  params={"filter": "apps", "sort": "alphabetical"})
        if isinstance(payload, dict):
            items = payload.get("items") or payload.get("apps") or payload.get("data") or payload.get("results") or []
            if isinstance(items, dict) and any(key in items for key in ("items", "apps", "results")):
                items = items.get("items") or items.get("apps") or items.get("results") or []
            if isinstance(items, dict):
                items = [
                    ({"id": key, **value} if isinstance(value, dict) else {"id": key})
                    for key, value in items.items()
                ]
        else:
            items = payload
        if not isinstance(items, list):
            log.warning("Flathub returned an unrecognised catalogue shape: %s", type(items).__name__)
            return []
        log.info("Flathub catalogue response: %d candidate entries; sample=%r", len(items), items[:1])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Flathub catalogue fetch failed: %s", exc)
        return []

    ids = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            app_id = item.get("id") or item.get("app_id") or item.get("appId") or item.get("flatpak_id")
            if app_id:
                ids.append(str(app_id))
    ids = list(dict.fromkeys(ids))[:FLATHUB_MAX_APPS]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def detail(app_id: str) -> dict[str, Any] | None:
        async with sem:
            try:
                data = await _get_json(client, f"https://flathub.org/api/v2/appstream/{app_id}",
                                       params={"locale": "en"})
                if not isinstance(data, dict):
                    return None
                categories = data.get("categories") or []
                icon = data.get("icon")
                if not icon:
                    icons = data.get("icons") or []
                    if isinstance(icons, list):
                        icon = next((item for item in icons if isinstance(item, str) and item.startswith("https://")), None)
                return _record(
                    "flathub", app_id, "linux", data.get("name") or app_id,
                    f"https://flathub.org/apps/{app_id}",
                    developer=data.get("developer_name") or data.get("developer"),
                    summary=data.get("summary"), description=data.get("description"),
                    category=", ".join(categories) if isinstance(categories, list) else None,
                    license=data.get("license"), app_url=data.get("project_url"),
                    icon_url=icon, metadata_json={"flatpak_id": app_id, "categories": categories},
                )
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("Flathub detail failed for %s: %s", app_id, exc)
                return None

    results = await asyncio.gather(*(detail(app_id) for app_id in ids), return_exceptions=True)
    return _unique([x for x in results if isinstance(x, dict)])


async def fetch_homebrew_cask(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch the official Homebrew Cask JSON catalogue."""
    try:
        payload = await _get_json(client, "https://formulae.brew.sh/api/cask.json")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Homebrew Cask fetch failed: %s", exc)
        return []
    # The public feed is normally a JSON array; accept an envelope defensively.
    items = payload if isinstance(payload, list) else (
        payload.get("casks", []) if isinstance(payload, dict) else []
    )
    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        token = item.get("token")
        names = item.get("name") or []
        name = names[0] if isinstance(names, list) and names else token
        artwork = item.get("artwork") or {}
        record = _record(
            "homebrew-cask", token, "macos", name,
            f"https://formulae.brew.sh/cask/{token}" if token else None,
            summary=item.get("desc"), description=item.get("desc"),
            category=item.get("category"), license=item.get("license"),
            app_url=item.get("homepage"),
            icon_url=artwork.get("icon") if isinstance(artwork, dict) else None,
            metadata_json={"version": item.get("version"), "homepage": item.get("homepage"),
                           "caveats": item.get("caveats")},
        )
        if record:
            records.append(record)
    return _unique(records)


async def fetch_apple_itunes(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search Apple’s public Search API; store the app's own listing URL."""
    records = []
    for term in APPLE_SEARCH_TERMS:
        try:
            payload = await _get_json(
                client, "https://itunes.apple.com/search",
                params={"term": term, "country": APPLE_COUNTRY, "limit": APPLE_LIMIT, "media": "software"},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Apple Search API failed for %r: %s", term, exc)
            continue
        items = payload.get("results", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Apple's Search API uses kind=software for app results.
            if item.get("kind") not in (None, "software", "software-package"):
                continue
            track_url = item.get("trackViewUrl")
            record = _record(
                "apple-itunes", item.get("trackId"), "ios", item.get("trackName"),
                track_url, developer=item.get("artistName"),
                summary=item.get("shortDescription"), description=item.get("description"),
                category=(item.get("genres") or [None])[0],
                app_url=track_url, icon_url=item.get("artworkUrl512") or item.get("artworkUrl100"),
                metadata_json={"bundle_id": item.get("bundleId"), "version": item.get("version"),
                               "price": item.get("price"), "genres": item.get("genres")},
            )
            if record:
                records.append(record)
    return _unique(records)


async def fetch_winget_community(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Query the winget.run community index. Installer URLs are intentionally discarded."""
    records = []
    for term in WINGET_SEARCH_TERMS:
        try:
            payload = await _get_json(
                client, "https://api.winget.run/v2/packages",
                params={"query": term, "take": WINGET_LIMIT, "page": 0},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("WinGet community search failed for %r: %s", term, exc)
            continue
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("Packages") or payload.get("packages") or payload.get("value") or []
        else:
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            latest = item.get("Latest") or item.get("latest") or {}
            package_id = item.get("Id") or item.get("id")
            name = latest.get("Name") or item.get("name") or package_id
            homepage = latest.get("Homepage") or item.get("homePageUrl")
            description = latest.get("Description") or item.get("description")
            publisher = latest.get("Publisher") or item.get("publisher")
            license_name = latest.get("License") or item.get("license")
            record = _record(
                "winget-run-community", package_id, "windows", name,
                f"https://winget.run/pkg/{package_id}" if package_id else None,
                developer=publisher, summary=description, description=description,
                license=license_name, app_url=homepage,
                icon_url=item.get("IconUrl") or item.get("iconUrl"),
                metadata_json={"version": latest.get("Version") or item.get("version"),
                               "tags": latest.get("Tags") or item.get("tags")},
            )
            if record:
                records.append(record)
    return _unique(records)


def fetch_curated_web_apps() -> list[dict[str, Any]]:
    """Load manually curated web applications."""
    try:
        payload = json.loads(WEB_APPS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read curated web catalogue: %s", exc)
        return []
    if not isinstance(payload, list):
        return []
    records = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        record = _record(
            "mta-curated-web", item.get("id") or item.get("name"), "web",
            item.get("name"), item.get("app_url") or item.get("homepage"),
            developer=item.get("developer"), summary=item.get("summary"),
            description=item.get("description"), category=item.get("category"),
            app_url=item.get("app_url") or item.get("homepage"),
            icon_url=item.get("icon_url"),
            metadata_json={"homepage": item.get("homepage"), "curated": True},
        )
        if record:
            records.append(record)
    return _unique(records)



SNAP_SEARCH_TERMS = [x.strip() for x in os.getenv(
    "SNAP_SEARCH_TERMS",
    "browser,office,photo,video,education,security,finance,communication,developer,utilities,messaging,maps,health,weather",
).split(",") if x.strip()]
SNAP_LIMIT = max(1, min(int(os.getenv("SNAP_LIMIT", "100")), 100))


async def fetch_snap_store(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search the official public Snap Store API for stable public application metadata.

    This is search-term indexing, not a complete export of the entire Snap Store.
    """
    records: list[dict[str, Any]] = []
    headers = {
        "Snap-Device-Series": "16",
        "Snap-Device-Architecture": os.getenv("SNAP_DEVICE_ARCHITECTURE", "amd64"),
        "Snap-Device-Store": "ubuntu",
        "Accept": "application/json",
    }
    for term in SNAP_SEARCH_TERMS:
        try:
            response = await client.get(
                "https://api.snapcraft.io/v2/snaps/find",
                params={"q": term, "architecture": headers["Snap-Device-Architecture"],
                        "fields": "title,summary,description,media,publisher,store-url,version,channel,license,website,common-ids,confinement"},
                headers=headers,
            )
            if response.is_error:
                log.warning("Snap Store API error for %r: HTTP %s %s", term, response.status_code, response.text[:800])
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Snap Store search failed for %r: %s", term, exc)
            continue
        items = payload if isinstance(payload, list) else (
            payload.get("results") or payload.get("snaps") or payload.get("data") or []
            if isinstance(payload, dict) else []
        )
        for item in items:
            if not isinstance(item, dict) or item.get("status", "available") != "available":
                continue
            publisher = item.get("publisher") or {}
            publisher_name = publisher.get("display-name") or publisher.get("username") if isinstance(publisher, dict) else None
            name = item.get("title") or item.get("name")
            snap_name = item.get("name")
            store_url = _https(item.get("store-url")) or (f"https://snapcraft.io/{snap_name}" if snap_name else None)
            media = item.get("media") or []
            icon = item.get("icon") or next((entry.get("url") for entry in media if isinstance(entry, dict) and entry.get("type") == "icon"), None)
            record = _record(
                "snap-store", item.get("id") or snap_name, "linux", name,
                store_url, developer=publisher_name or item.get("developer"),
                summary=item.get("summary"), description=item.get("description"),
                license=item.get("license"), app_url=item.get("website"),
                icon_url=icon,
                metadata_json={"snap_name": snap_name, "version": item.get("version"),
                               "channel": item.get("channel"), "publisher": publisher,
                               "common_ids": item.get("common-ids"), "confinement": item.get("confinement")},
            )
            if record:
                records.append(record)
    return _unique(records)


async def fetch_fdroid(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch F-Droid index metadata; support array and keyed app catalogues."""
    try:
        payload = await _get_json(client, "https://f-droid.org/repo/index-v2.json")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("F-Droid index fetch failed: %s", exc)
        return []
    raw_apps = (payload.get("apps") or payload.get("packages") or []) if isinstance(payload, dict) else []
    if isinstance(raw_apps, dict):
        entries = list(raw_apps.items())
    elif isinstance(raw_apps, list):
        entries = [(None, item) for item in raw_apps]
    else:
        log.warning("F-Droid returned an unrecognised apps collection: %s", type(raw_apps).__name__)
        return []

    records = []
    for keyed_id, entry in entries:
        if not isinstance(entry, dict):
            continue
        package_id = keyed_id or entry.get("packageName") or entry.get("package_name") or entry.get("id")
        if not package_id:
            continue
        # Some index variants nest descriptive fields beneath "metadata".
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else entry
        localized = metadata.get("localized") or {}
        if not isinstance(localized, dict):
            localized = {}
        # Index-v2 represents localized fields as field -> locale -> text;
        # older variants may represent locale -> field -> text.
        locale_record = localized.get("en-US") or localized.get("en")
        if not isinstance(locale_record, dict):
            locale_record = {}

        def plain_text(value: Any) -> Any:
            # Metadata values can themselves be locale dictionaries.
            while isinstance(value, dict):
                value = value.get("en-US") or value.get("en") or next(iter(value.values()), None)
            return value

        def localized_text(field: str) -> Any:
            value = locale_record.get(field) if field in locale_record else localized.get(field)
            return plain_text(value)

        def find_icon_path(value: Any, depth: int = 0) -> str | None:
            if depth > 8 or value is None:
                return None
            if isinstance(value, str):
                candidate = value.strip()
                return candidate if candidate.lower().split("?", 1)[0].endswith((".png", ".jpg", ".jpeg", ".webp", ".svg")) else None
            if isinstance(value, list):
                for item in value:
                    found = find_icon_path(item, depth + 1)
                    if found:
                        return found
            if isinstance(value, dict):
                name = value.get("name")
                if isinstance(name, str) and name.lower().split("?", 1)[0].endswith((".png", ".jpg", ".jpeg", ".webp", ".svg")):
                    return name
                for key in ("en-US", "en", "icon", "icons", "name"):
                    if key in value:
                        found = find_icon_path(value[key], depth + 1)
                        if found:
                            return found
                for item in value.values():
                    found = find_icon_path(item, depth + 1)
                    if found:
                        return found
            return None

        categories = metadata.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]
        if not isinstance(categories, list):
            categories = []

        summary = localized_text("summary") or localized_text("short_description") or plain_text(metadata.get("summary"))
        description = localized_text("description") or localized_text("full_description") or plain_text(metadata.get("description"))
        icon = locale_record.get("icon") or localized.get("icon") or metadata.get("icon") or entry.get("icon")
        icon_name = find_icon_path(icon)
        icon_url = None
        if icon_name:
            icon_url = icon_name if icon_name.startswith("https://") else f"https://f-droid.org/repo/{icon_name.lstrip('/')}"

        record = _record(
            "fdroid", package_id, "android",
            localized_text("name") or localized_text("title") or plain_text(metadata.get("name")) or package_id,
            f"https://f-droid.org/packages/{package_id}/",
            developer=metadata.get("authorName") or metadata.get("author"),
            summary=summary, description=description,
            category=", ".join(str(x) for x in categories),
            license=metadata.get("license"),
            app_url=metadata.get("webSite") or metadata.get("sourceCode"),
            icon_url=icon_url,
            metadata_json={
                "package_name": package_id,
                "source_code": metadata.get("sourceCode"),
                "issue_tracker": metadata.get("issueTracker"),
                "website": metadata.get("webSite"),
                "added": metadata.get("added") or entry.get("added"),
                "last_updated": metadata.get("lastUpdated") or entry.get("lastUpdated"),
            },
        )
        if record:
            records.append(record)
    log.info("F-Droid index: %d app records from %d app entries", len(records), len(entries))
    return _unique(records)

async def fetch_all_catalogs() -> dict[str, list[dict[str, Any]]]:
    """Fetch independent sources concurrently; isolate source failures."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as client:
        jobs = {
            "fdroid": fetch_fdroid(client),
            "snap-store": fetch_snap_store(client),
            "flathub": fetch_flathub(client),
            "homebrew-cask": fetch_homebrew_cask(client),
            "apple-itunes": fetch_apple_itunes(client),
            "winget-run-community": fetch_winget_community(client),
        }
        names = list(jobs)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    output: dict[str, list[dict[str, Any]]] = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            log.exception("Catalogue %s failed: %s", name, result)
            output[name] = []
        else:
            output[name] = result
    output["mta-curated-web"] = fetch_curated_web_apps()
    return output
