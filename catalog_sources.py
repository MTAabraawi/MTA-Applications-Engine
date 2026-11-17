"""Independent metadata connectors for public software catalogues.

Only metadata and official catalogue/product URLs are stored. Installer binaries
are never downloaded, mirrored, or hosted by this service.
"""
from __future__ import annotations

import asyncio
import html as html_module
import json
import logging
import re
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
WINGET_PAGES = max(1, min(int(os.getenv("WINGET_PAGES", "3")), 10))
ARCH_SEARCH_TERMS = [x.strip() for x in os.getenv("ARCH_SEARCH_TERMS", "browser,office,photo,video,education,security,finance,communication,developer,utilities").split(",") if x.strip()]
ARCH_LIMIT = max(1, min(int(os.getenv("ARCH_LIMIT", "100")), 200))
DEBIAN_SEARCH_TERMS = [x.strip() for x in os.getenv(
    "DEBIAN_SEARCH_TERMS", "browser,office,graphics,video,audio,education,security,games,communication,utilities"
).split(",") if x.strip()]
DEBIAN_LIMIT_PER_TERM = max(1, min(int(os.getenv("DEBIAN_LIMIT_PER_TERM", "12")), 20))
FLATHUB_MAX_APPS = max(1, min(int(os.getenv("FLATHUB_MAX_APPS", "1000")), 5000))
CONCURRENCY = max(1, min(int(os.getenv("CATALOG_CONCURRENCY", "6")), 12))
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HEADERS = {"User-Agent": "MTA-Applications-Engine/0.2 (+https://github.com/MTAabraawi/MTA-Applications-Engine)"}


def _clean_text(value: Any, max_length: int = 10000) -> str | None:
    """Normalize HTML fragments, entities, control characters, and whitespace."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = re.sub(r"<(script|style)[^>]*>[\\s\\S]*?</\\1>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<(?:br|/p|/li|/h[1-6]|/div)[^>]*>", "\\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_module.unescape(value)
    value = re.sub(r"[\\u200b-\\u200d\\ufeff]", "", value)
    value = re.sub(r"[\\t\\r ]+", " ", value)
    value = re.sub(r"\\n\\s*\\n+", "\\n", value).strip()
    return value[:max_length] if value else None


def _https(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        _ = parsed.port  # Reject malformed ports.
    except ValueError:
        return None
    return value


def _record(source_key: str, external_id: Any, platform: str, name: Any,
            source_url: Any, **kwargs: Any) -> dict[str, Any] | None:
    external_id = str(external_id or "").strip()
    name = _clean_text(name, 500)
    source_url = _https(source_url)
    if not external_id or not name or not source_url:
        return None
    result = {
        "source_key": source_key, "external_id": external_id,
        "platform": platform, "name": name,
        "developer": _clean_text(kwargs.get("developer")),
        "summary": _clean_text(kwargs.get("summary")),
        "description": _clean_text(kwargs.get("description")),
        "category": _clean_text(kwargs.get("category")),
        "license": _clean_text(kwargs.get("license")),
        "source_url": source_url,
        "app_url": _https(kwargs.get("app_url")),
        "icon_url": _https(kwargs.get("icon_url")),
        "metadata_json": kwargs.get("metadata_json") or {},
    }
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


async def fetch_apple_itunes(
    client: httpx.AsyncClient, *, entity: str | None = None,
    source_key: str = "apple-itunes", platform: str = "ios",
) -> list[dict[str, Any]]:
    """Search Apple's public Search API for iOS or Mac software listings."""
    records = []
    for term in APPLE_SEARCH_TERMS:
        try:
            payload = await _get_json(
                client, "https://itunes.apple.com/search",
                params={"term": term, "country": APPLE_COUNTRY, "limit": APPLE_LIMIT, "media": "software",
                        **({"entity": entity} if entity else {})},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Apple Search API failed for %r: %s", term, exc)
            continue
        items = payload.get("results", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Apple's Search API uses kind=software for app results.
            if item.get("kind") not in (None, "software", "software-package", "mac-software"):
                continue
            track_url = item.get("trackViewUrl")
            record = _record(
                source_key, item.get("trackId"), platform, item.get("trackName"),
                track_url, developer=item.get("artistName"),
                summary=item.get("shortDescription"), description=item.get("description"),
                category=(item.get("genres") or [None])[0],
                app_url=track_url, icon_url=item.get("artworkUrl512") or item.get("artworkUrl100"),
                metadata_json={"bundle_id": item.get("bundleId"), "version": item.get("version"),
                               "price": item.get("price"), "genres": item.get("genres"), "size_bytes": item.get("fileSizeBytes")},
            )
            if record:
                records.append(record)
    return _unique(records)


async def fetch_apple_macos(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search Apple's public Search API for Mac App Store listings."""
    return await fetch_apple_itunes(client, entity="macSoftware", source_key="apple-macos", platform="macos")


SNAP_SEARCH_TERMS = [x.strip() for x in os.getenv(
    "SNAP_SEARCH_TERMS", "browser,office,photo,video,education,security,finance,communication,developer,utilities"
).split(",") if x.strip()]
SNAP_LIMIT = max(1, min(int(os.getenv("SNAP_LIMIT", "50")), 100))


async def fetch_snap_store(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search the public Snap Store using snapd's documented /v2/find API."""
    records = []
    for term in SNAP_SEARCH_TERMS:
        try:
            payload = await _get_json(
                client, "https://api.snapcraft.io/v2/snaps/find",
                params={"q": term},
                headers={"Snap-Device-Series": "16", "Accept": "application/json"},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Snap Store search failed for %r: %s", term, exc)
            continue
        items = payload.get("results", []) if isinstance(payload, dict) else []
        for item in items[:SNAP_LIMIT] if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            title = item.get("title") or name
            publisher = item.get("publisher") or {}
            media = item.get("media") or []
            icon = next((m.get("url") for m in media if isinstance(m, dict) and m.get("type") == "icon"), None)
            record = _record(
                "snap-store", name, "linux", title,
                f"https://snapcraft.io/{name}" if name else None,
                developer=publisher.get("display-name") if isinstance(publisher, dict) else None,
                summary=item.get("summary"), description=item.get("description"),
                category=", ".join(item.get("categories") or []) if isinstance(item.get("categories"), list) else None,
                icon_url=icon,
                metadata_json={"snap_name": name, "snap_id": item.get("id"), "publisher": publisher},
            )
            if record:
                records.append(record)
    return _unique(records)


async def fetch_arch_linux(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search Arch Linux's documented public package JSON interface."""
    records = []
    for term in ARCH_SEARCH_TERMS:
        try:
            payload = await _get_json(client, "https://archlinux.org/packages/search/json/", params={"q": term})
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Arch Linux package search failed for %r: %s", term, exc)
            continue
        items = payload if isinstance(payload, list) else (payload.get("results") or payload.get("packages") or [] if isinstance(payload, dict) else [])
        for item in items[:ARCH_LIMIT] if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            package = item.get("pkgname") or item.get("name")
            repo = item.get("repo") or "extra"
            arch = item.get("arch") or "x86_64"
            if not package:
                continue
            raw_size = item.get("compressed_size") or item.get("pkg_size") or item.get("download_size")
            try:
                size = int(raw_size) if raw_size is not None else None
            except (TypeError, ValueError):
                size = None
            record = _record(
                "arch-linux", f"{repo}/{arch}/{package}", "linux", package,
                f"https://archlinux.org/packages/{repo}/{arch}/{package}/",
                developer=item.get("packager"), summary=item.get("pkgdesc") or item.get("description"),
                description=item.get("pkgdesc") or item.get("description"),
                category=", ".join(item.get("groups") or []) if isinstance(item.get("groups"), list) else None,
                license=", ".join(item.get("licenses") or []) if isinstance(item.get("licenses"), list) else None,
                metadata_json={"version": item.get("pkgver"), "repository": repo, "architecture": arch,
                               "size_bytes": size, "last_updated": item.get("last_update"), "homepage": item.get("url")},
            )
            if record:
                records.append(record)
    return _unique(records)




def _debian_text(value: str) -> str:
    import html as html_module
    value = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<br\s*/?>|</(p|pre|li|h[1-6])>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_module.unescape(value)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", value)).strip()


async def fetch_debian_packages(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search Debian's public package index and extract official package-page metadata."""
    from urllib.parse import quote

    names: list[str] = []
    for term in DEBIAN_SEARCH_TERMS:
        try:
            payload = await _get_json(client, f"https://sources.debian.org/api/search/{quote(term, safe='')}/")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Debian package search failed for %r: %s", term, exc)
            continue
        results = payload.get("results", {}) if isinstance(payload, dict) else {}
        exact = results.get("exact") if isinstance(results, dict) else None
        other = results.get("other", []) if isinstance(results, dict) else []
        if isinstance(exact, dict) and exact.get("name"):
            names.append(str(exact["name"]))
        if isinstance(other, list):
            names.extend(str(item["name"]) for item in other[:DEBIAN_LIMIT_PER_TERM]
                         if isinstance(item, dict) and item.get("name"))
    names = list(dict.fromkeys(names))
    sem = asyncio.Semaphore(CONCURRENCY)

    async def detail(package: str) -> dict[str, Any] | None:
        async with sem:
            try:
                url = f"https://packages.debian.org/sid/amd64/{quote(package, safe='')}"
                response = await client.get(url)
                if response.status_code != 200:
                    return None
                page = response.text
                if "role::program" not in page.lower():
                    return None
                heading = re.search(
                    r"<h2[^>]*>\s*Links for [^<]*</h2>[\s\S]*?<h2[^>]*>(.*?)</h2>",
                    page, re.I | re.S,
                )
                if not heading:
                    return None
                summary = _debian_text(heading.group(1))
                start = heading.end()
                end_match = re.search(r"<h2[^>]*>\s*Other Packages Related", page[start:], re.I)
                body = page[start:start + end_match.start()] if end_match else page[start:]
                body = re.split(r"<p[^>]*>\s*Tags:", body, maxsplit=1, flags=re.I)[0]
                description = _debian_text(body)[:6000] or summary
                size_match = re.search(
                    r"<th>\s*amd64\s*</th>\s*<td>\s*([\d,.]+)\s*(kB|MB|B)",
                    page, re.I | re.S,
                )
                size_bytes = None
                if size_match:
                    amount = float(size_match.group(1).replace(",", ""))
                    factor = {"b": 1, "kb": 1024, "mb": 1024 * 1024}[size_match.group(2).lower()]
                    size_bytes = int(amount * factor)
                version_match = re.search(r"<h1>\s*Package:\s*[^<(]+\(([^)]+)\)", page, re.I)
                version = version_match.group(1).strip() if version_match else None
                return _record(
                    "debian-packages", package, "linux", package, url,
                    summary=summary, description=description, app_url=url,
                    metadata_json={"version": version, "architecture": "amd64",
                                   "size_bytes": size_bytes, "suite": "sid"},
                )
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("Debian package detail failed for %s: %s", package, exc)
                return None

    results = await asyncio.gather(*(detail(name) for name in names), return_exceptions=True)
    return _unique([item for item in results if isinstance(item, dict)])


async def fetch_winget_community(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Query the public winget.run index backed by WinGet community manifests.

    This is a search index rather than a Microsoft-operated bulk API. We use
    bounded pagination per broad query and keep the original WinGet package ID.
    """
    records = []
    for term in WINGET_SEARCH_TERMS:
        for page in range(WINGET_PAGES):
            try:
                payload = await _get_json(
                    client, "https://api.winget.run/v2/packages",
                    params={"query": term, "take": WINGET_LIMIT, "page": page},
                )
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("WinGet community search failed for %r page %d: %s", term, page, exc)
                break
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                items = payload.get("Packages") or payload.get("packages") or payload.get("value") or []
            else:
                items = []
            if not isinstance(items, list) or not items:
                break
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
                                   "tags": latest.get("Tags") or item.get("tags"),
                                   "catalog_source": "WinGet community manifests via winget.run"},
                )
                if record:
                    records.append(record)
            if len(items) < WINGET_LIMIT:
                break
            await asyncio.sleep(0.05)
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



async def fetch_fdroid(
    client: httpx.AsyncClient,
    *,
    index_url: str = "https://f-droid.org/repo/index-v2.json",
    source_key: str = "fdroid",
    app_page_base: str = "https://f-droid.org/packages/",
    app_page_suffix: str = "/",
    icon_base: str = "https://f-droid.org/repo/",
) -> list[dict[str, Any]]:
    """Fetch an F-Droid-compatible index without downloading installer files."""
    try:
        payload = await _get_json(client, index_url)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("%s index fetch failed: %s", source_key, exc)
        return []
    raw_apps = (payload.get("apps") or payload.get("packages") or []) if isinstance(payload, dict) else []
    if isinstance(raw_apps, dict):
        entries = list(raw_apps.items())
    elif isinstance(raw_apps, list):
        entries = [(None, item) for item in raw_apps]
    else:
        log.warning("%s returned an unrecognised apps collection: %s", source_key, type(raw_apps).__name__)
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
            icon_url = icon_name if icon_name.startswith("https://") else f"{icon_base}{icon_name.lstrip('/')}"

        record = _record(
            source_key, package_id, "android",
            localized_text("name") or localized_text("title") or plain_text(metadata.get("name")) or package_id,
            f"{app_page_base}{package_id}{app_page_suffix}",
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
    log.info("%s index: %d app records from %d app entries", source_key, len(records), len(entries))
    return _unique(records)

async def fetch_izzyondroid(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch the official IzzyOnDroid F-Droid-compatible repository index."""
    return await fetch_fdroid(
        client,
        index_url="https://apt.izzysoft.de/fdroid/repo/index-v2.json",
        source_key="izzyondroid",
        app_page_base="https://apt.izzysoft.de/fdroid/index/apk/",
        app_page_suffix="",
        icon_base="https://apt.izzysoft.de/fdroid/repo/",
    )


STEAM_SEARCH_TERMS = [x.strip() for x in os.getenv(
    "STEAM_SEARCH_TERMS",
    "action,adventure,strategy,simulation,rpg,indie,free to play,multiplayer",
).split(",") if x.strip()]


async def fetch_steam(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Search Steam's public storefront JSON endpoint; results are query-limited, not exhaustive."""
    records = []
    for term in STEAM_SEARCH_TERMS:
        try:
            payload = await _get_json(
                client, "https://store.steampowered.com/api/storesearch/",
                params={"term": term, "l": "english", "cc": "us"}, timeout=10,
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Steam storefront search failed for %r: %s", term, exc)
            continue
        items = payload.get("items", []) if isinstance(payload, dict) else []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            app_id = item.get("id")
            name = item.get("name")
            record = _record(
                "steam", app_id, "pc-games", name,
                f"https://store.steampowered.com/app/{app_id}/" if app_id else None,
                icon_url=item.get("tiny_image"),
                metadata_json={"steam_appid": app_id, "item_type": item.get("type"),
                               "price": item.get("price"), "source_scope": "storefront_search"},
            )
            if record:
                records.append(record)
        await asyncio.sleep(0.15)
    log.info("Steam storefront search: %d unique records across %d terms",
             len(_unique(records)), len(STEAM_SEARCH_TERMS))
    return _unique(records)


async def fetch_all_catalogs(on_source=None) -> dict[str, list[dict[str, Any]]]:
    """Fetch independent sources concurrently; isolate source failures."""
    async def collect(name: str, job):
        try:
            records = await job
        except Exception as exc:
            log.exception("Catalogue %s failed: %s", name, exc)
            if on_source:
                on_source(name, [], exc)
            return []
        if on_source:
            on_source(name, records)
        return records

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as client:
        jobs = {
            "fdroid": fetch_fdroid(client),
            "izzyondroid": fetch_izzyondroid(client),
            "snap-store": fetch_snap_store(client),
            "flathub": fetch_flathub(client),
            "homebrew-cask": fetch_homebrew_cask(client),
            "apple-itunes": fetch_apple_itunes(client),
            "apple-macos": fetch_apple_macos(client),
            "winget-run-community": fetch_winget_community(client),
            "arch-linux": fetch_arch_linux(client),
            "debian-packages": fetch_debian_packages(client),
            "steam": fetch_steam(client),
        }
        names = list(jobs)
        jobs = {name: collect(name, job) for name, job in jobs.items()}
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    output: dict[str, list[dict[str, Any]]] = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            log.exception("Catalogue %s failed: %s", name, result)
            output[name] = []
        else:
            output[name] = result
    curated = fetch_curated_web_apps()
    output["mta-curated-web"] = curated
    if on_source:
        on_source("mta-curated-web", curated)
    return output
