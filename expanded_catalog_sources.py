"""Additional public software catalogues for the MTA discovery engine.

Only public metadata and official listing URLs are collected. No installer
binaries, private endpoints, login-gated data, or access-control bypasses.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import html as html_module
import re
import os
import tarfile
from typing import Any

import httpx

from catalog_sources import _record, _unique

log = logging.getLogger("mta.catalogs.expanded")
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
HEADERS = {"User-Agent": "MTA-Applications-Engine/0.4 (+https://github.com/MTAabraawi/MTA-Applications-Engine)"}
CONCURRENCY = max(1, min(int(os.getenv("CATALOG_CONCURRENCY", "5")), 10))


async def _json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response.json()


async def fetch_firefox_addons(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Collect public Firefox extensions from Mozilla's documented AMO API."""
    terms = [x.strip() for x in os.getenv(
        "FIREFOX_ADDON_TERMS",
        "privacy,security,ad blocker,productivity,developer,translator,video,download,accessibility,shopping,web tools,password,tab,theme",
    ).split(",") if x.strip()]
    pages = max(1, min(int(os.getenv("FIREFOX_ADDON_PAGES", "3")), 5))
    records: list[dict[str, Any]] = []
    for term in terms:
        for page in range(1, pages + 1):
            try:
                payload = await _json(
                    client, "https://addons.mozilla.org/api/v5/addons/search/",
                    params={"q": term, "app": "firefox", "type": "extension",
                            "page_size": 50, "page": page},
                )
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Mozilla AMO search failed for %r page %d: %s", term, page, exc)
                break
            items = payload.get("results", []) if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                slug = item.get("slug")
                name = item.get("name")
                if isinstance(name, dict):
                    name = name.get("en-US") or name.get("en") or next(iter(name.values()), None)
                summary = item.get("summary")
                if isinstance(summary, dict):
                    summary = summary.get("en-US") or summary.get("en") or next(iter(summary.values()), None)
                authors = item.get("authors") or []
                author = authors[0].get("name") if authors and isinstance(authors[0], dict) else None
                icons = item.get("icons") or {}
                icon = item.get("icon_url") or (icons.get("64") if isinstance(icons, dict) else None)
                url = f"https://addons.mozilla.org/firefox/addon/{slug}/" if slug else None
                record = _record(
                    "firefox-addons", item.get("id") or slug, "browser-extensions",
                    name, url, developer=author, summary=summary,
                    description=item.get("description") if isinstance(item.get("description"), str) else None,
                    category=", ".join(item.get("categories") or []) if isinstance(item.get("categories"), list) else None,
                    icon_url=icon,
                    metadata_json={"slug": slug, "weekly_downloads": item.get("weekly_downloads"),
                                   "average_daily_users": item.get("average_daily_users"),
                                   "ratings": item.get("ratings"), "created": item.get("created"),
                                   "last_updated": item.get("last_updated"), "source": "Mozilla Add-ons API"},
                )
                if record:
                    records.append(record)
            await asyncio.sleep(0.08)
    return _unique(records)


async def fetch_wordpress_plugins(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Collect WordPress plugins through the official WordPress.org directory API."""
    terms = [x.strip() for x in os.getenv(
        "WORDPRESS_PLUGIN_TERMS",
        "security,seo,backup,forms,commerce,performance,editor,analytics,media,accessibility,translation,blocks,cache,developer",
    ).split(",") if x.strip()]
    pages = max(1, min(int(os.getenv("WORDPRESS_PLUGIN_PAGES", "3")), 5))
    records: list[dict[str, Any]] = []
    for term in terms:
        for page in range(1, pages + 1):
            try:
                payload = await _json(
                    client, "https://api.wordpress.org/plugins/info/1.2/",
                    params={"action": "query_plugins", "request[search]": term,
                            "request[page]": page, "request[per_page]": 100,
                            "request[fields][short_description]": 1,
                            "request[fields][icons]": 1},
                )
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("WordPress plugin search failed for %r page %d: %s", term, page, exc)
                break
            items = payload.get("plugins", []) if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                slug = item.get("slug")
                name = item.get("name")
                if isinstance(name, str):
                    name = html_module.unescape(re.sub(r"<[^>]+>", " ", name)).strip()
                if not slug or not name:
                    continue
                short_description = item.get("short_description")
                if isinstance(short_description, str):
                    short_description = html_module.unescape(re.sub(r"<[^>]+>", " ", short_description)).strip()
                author = item.get("author")
                if isinstance(author, str):
                    author = html_module.unescape(re.sub(r"<[^>]+>", " ", author)).strip()
                icons = item.get("icons") or {}
                icon = icons.get("2x") or icons.get("1x") or icons.get("default") if isinstance(icons, dict) else None
                record = _record(
                    "wordpress-plugins", slug, "web-plugins", name,
                    f"https://wordpress.org/plugins/{slug}/",
                    developer=author, summary=short_description,
                    description=short_description,
                    category="WordPress plugin", app_url=item.get("homepage"),
                    icon_url=icon,
                    metadata_json={"version": item.get("version"), "rating": item.get("rating"),
                                   "num_ratings": item.get("num_ratings"),
                                   "active_installs": item.get("active_installs"),
                                   "last_updated": item.get("last_updated"),
                                   "requires": item.get("requires"), "tested": item.get("tested")},
                )
                if record:
                    records.append(record)
            await asyncio.sleep(0.08)
    return _unique(records)


async def fetch_homebrew_formulae(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch the complete public Homebrew/core formula metadata feed."""
    try:
        payload = await _json(client, "https://formulae.brew.sh/api/formula.json")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Homebrew formula feed failed: %s", exc)
        return []
    if not isinstance(payload, list):
        return []
    records = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        versions = item.get("versions") or {}
        record = _record(
            "homebrew-formulae", name, "macos", item.get("full_name") or name,
            f"https://formulae.brew.sh/formula/{name}",
            developer=item.get("tap"), summary=item.get("desc"), description=item.get("desc"),
            category=None,
            license=item.get("license"), app_url=item.get("homepage"),
            metadata_json={"version": versions.get("stable"), "tap": item.get("tap"),
                           "bottle": bool(item.get("bottle")),
                           "also_available_on_linux": True, "dependencies": item.get("dependencies", [])},
        )
        if record:
            records.append(record)
    return _unique(records)


async def fetch_scoop_buckets(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Read public Scoop bucket manifests from the official GitHub bucket archives."""
    buckets = [
        ("main", "ScoopInstaller/Main"),
        ("extras", "ScoopInstaller/Extras"),
        ("games", "ScoopInstaller/Games"),
        ("versions", "ScoopInstaller/Versions"),
    ]
    sem = asyncio.Semaphore(3)

    async def one_bucket(bucket: str, repository: str) -> list[dict[str, Any]]:
        async with sem:
            url = f"https://codeload.github.com/{repository}/tar.gz/refs/heads/master"
            try:
                response = await client.get(url, timeout=TIMEOUT)
                response.raise_for_status()
                archive = tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz")
            except (httpx.HTTPError, tarfile.TarError, OSError) as exc:
                log.warning("Scoop bucket %s download failed: %s", bucket, exc)
                return []
            records = []
            with archive:
                for member in archive.getmembers():
                    if not member.isfile() or "/bucket/" not in member.name or not member.name.endswith(".json"):
                        continue
                    if member.size > 512_000:
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    try:
                        manifest = json.loads(handle.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(manifest, dict):
                        continue
                    package = member.name.rsplit("/", 1)[-1][:-5]
                    name = manifest.get("name") or package
                    description = manifest.get("description")
                    homepage = manifest.get("homepage")
                    license_value = manifest.get("license")
                    source_path = member.name.split("/bucket/", 1)[-1]
                    source_url = f"https://github.com/{repository}/blob/master/bucket/{source_path}"
                    record = _record(
                        "scoop", package.casefold(), "windows", name, source_url,
                        developer=None, summary=description, description=description,
                        category=f"Scoop {bucket}", license=license_value if isinstance(license_value, str) else None,
                        app_url=homepage if isinstance(homepage, str) else None,
                        metadata_json={"version": manifest.get("version"), "bucket": bucket,
                                       "homepage": homepage, "architecture": list((manifest.get("architecture") or {}).keys())
                                       if isinstance(manifest.get("architecture"), dict) else [],
                                       "manifest_path": source_path},
                    )
                    if record:
                        records.append(record)
            return records

    results = await asyncio.gather(*(one_bucket(name, repo) for name, repo in buckets), return_exceptions=True)
    combined = []
    for result in results:
        if isinstance(result, list):
            combined.extend(result)
    # Same package can occur in multiple buckets; keep the first (main > extras > games > versions).
    return _unique(combined)


async def fetch_alpine_packages(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Parse official Alpine APKINDEX archives for x86_64 main and community packages."""
    indexes = [
        ("main", "https://dl-cdn.alpinelinux.org/alpine/edge/main/x86_64/APKINDEX.tar.gz"),
        ("community", "https://dl-cdn.alpinelinux.org/alpine/edge/community/x86_64/APKINDEX.tar.gz"),
    ]
    records: list[dict[str, Any]] = []
    for repository, url in indexes:
        try:
            response = await client.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            archive = tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz")
            with archive:
                member = next((x for x in archive.getmembers() if x.name.endswith("APKINDEX")), None)
                if member is None:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                raw = handle.read().decode("utf-8", errors="replace")
        except (httpx.HTTPError, tarfile.TarError, OSError) as exc:
            log.warning("Alpine %s APKINDEX failed: %s", repository, exc)
            continue
        for block in raw.split("\n\n"):
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if len(line) > 2 and line[1] == ":":
                    fields[line[0]] = line[2:]
            package = fields.get("P")
            if not package:
                continue
            record = _record(
                "alpine-linux", f"{repository}/{package}", "linux", package,
                f"https://pkgs.alpinelinux.org/package/edge/{repository}/x86_64/{package}",
                developer=fields.get("m"), summary=fields.get("T"), description=fields.get("T"),
                license=fields.get("L"),
                app_url=fields.get("U"),
                metadata_json={"version": fields.get("V"), "architecture": fields.get("A"),
                               "origin": fields.get("o"), "size_bytes": int(fields["S"]) if fields.get("S", "").isdigit() else None,
                               "repository": repository, "branch": "edge"},
            )
            if record:
                records.append(record)
    return _unique(records)


async def fetch_google_play_appbrain(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Optional Google Play connector using AppBrain's documented API; requires a key."""
    api_key = os.getenv("APPBRAIN_API_KEY", "").strip()
    if not api_key:
        log.info("Google Play/AppBrain connector skipped: APPBRAIN_API_KEY is not configured")
        return []
    categories = [x.strip().upper() for x in os.getenv(
        "APPBRAIN_CATEGORIES",
        "BOOKS,BUSINESS,EDUCATION,ENTERTAINMENT,FINANCE,FOOD_AND_DRINK,GAMES,HEALTH_AND_FITNESS,LIFESTYLE,MAGAZINES_AND_NEWSPAPERS,MEDICAL,MUSIC,NAVIGATION,NEWS,PHOTO_AND_VIDEO,PRODUCTIVITY,REFERENCE,SHOPPING,SOCIAL_NETWORKING,SPORTS,TRAVEL,UTILITIES,WEATHER",
    ).split(",") if x.strip()]
    records: list[dict[str, Any]] = []
    for category in categories:
        try:
            payload = await _json(
                client, "https://api.appbrain.com/v2/info/browse",
                params={"apikey": api_key, "category": category, "offset": 0, "limit": 50, "platform": "ANDROID"},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("AppBrain Google Play category %s failed: %s", category, exc)
            continue
        items = payload.get("apps", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            package = item.get("package")
            name = item.get("name")
            record = _record(
                "google-play", package, "android", name,
                f"https://play.google.com/store/apps/details?id={package}" if package else None,
                developer=item.get("developer") or item.get("publisher"),
                summary=item.get("shortDescription"), description=item.get("description"),
                category=item.get("marketCategory") or category, icon_url=item.get("iconUrl") or item.get("icon"),
                metadata_json={"version": item.get("versionString"), "version_code": item.get("versionCode"),
                               "downloads_category": item.get("downloadsCategory"),
                               "estimated_downloads": item.get("estimatedDownloads"),
                               "last_updated": item.get("lastAppUpdateTime"),
                               "price": item.get("price"), "min_sdk": item.get("minSdkVersion"),
                               "source_provider": "AppBrain"},
            )
            if record:
                records.append(record)
    return _unique(records)


async def fetch_jetbrains_plugins(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Collect public JetBrains Marketplace plugin listings from its website search API."""
    terms = [x.strip() for x in os.getenv(
        "JETBRAINS_PLUGIN_TERMS",
        "python,kotlin,java,web,ide,git,database,theme,editor,testing,ai,markdown,docker,cloud,security",
    ).split(",") if x.strip()]
    limit = max(1, min(int(os.getenv("JETBRAINS_PLUGIN_LIMIT", "20")), 20))
    records: list[dict[str, Any]] = []
    for term in terms:
        try:
            payload = await _json(
                client, "https://plugins.jetbrains.com/api/searchPlugins",
                params={"search": term, "max": limit},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("JetBrains Marketplace search failed for %r: %s", term, exc)
            continue
        items = payload.get("plugins", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            plugin_id = item.get("id")
            name = item.get("name")
            link = item.get("link")
            if not plugin_id or not name:
                continue
            icon = item.get("icon")
            if isinstance(icon, str) and icon.startswith("/"):
                icon = "https://plugins.jetbrains.com" + icon
            vendor = item.get("vendor") or {}
            listing = "https://plugins.jetbrains.com" + link if isinstance(link, str) and link.startswith("/") else (
                f"https://plugins.jetbrains.com/plugin/{plugin_id}" if plugin_id else None
            )
            record = _record(
                "jetbrains-marketplace", plugin_id, "ide-plugins", name, listing,
                developer=vendor.get("name") if isinstance(vendor, dict) else None,
                summary=item.get("preview"), description=item.get("preview"),
                category=", ".join(item.get("tags") or []) if isinstance(item.get("tags"), list) else None,
                icon_url=icon,
                metadata_json={"xml_id": item.get("xmlId"), "downloads": item.get("downloads"),
                               "rating": item.get("rating"), "pricing_model": item.get("pricingModel"),
                               "vendor_verified": vendor.get("isVerified") if isinstance(vendor, dict) else None},
            )
            if record:
                records.append(record)
        await asyncio.sleep(0.08)
    return _unique(records)


async def fetch_gnome_extensions(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Collect GNOME Shell extensions from the public extensions.gnome.org search endpoint."""
    terms = [x.strip() for x in os.getenv(
        "GNOME_EXTENSION_TERMS",
        "system,desktop,appearance,weather,productivity,network,clipboard,window,monitor,media,keyboard,menu,privacy,accessibility",
    ).split(",") if x.strip()]
    records: list[dict[str, Any]] = []
    for term in terms:
        try:
            payload = await _json(
                client, "https://extensions.gnome.org/extension-query/",
                params={"search": term},
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("GNOME Extensions search failed for %r: %s", term, exc)
            continue
        items = payload.get("extensions", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            extension_id = item.get("uuid") or item.get("pk")
            name = item.get("name")
            link = item.get("link")
            if not extension_id or not name:
                continue
            listing = "https://extensions.gnome.org" + link if isinstance(link, str) and link.startswith("/") else None
            icon = item.get("icon")
            if isinstance(icon, str) and icon.startswith("/"):
                icon = "https://extensions.gnome.org" + icon
            record = _record(
                "gnome-extensions", extension_id, "desktop-extensions", name, listing,
                developer=item.get("creator"), summary=item.get("description"),
                description=item.get("description"), icon_url=icon,
                app_url=item.get("url"),
                metadata_json={"downloads": item.get("downloads"), "shell_version_map": item.get("shell_version_map"),
                               "extension_id": item.get("pk")},
            )
            if record:
                records.append(record)
        await asyncio.sleep(0.08)
    return _unique(records)



async def fetch_expanded_catalogs() -> dict[str, list[dict[str, Any]]]:
    """Fetch all new public catalogues independently; one source failure is isolated."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as client:
        jobs = {
            "firefox-addons": fetch_firefox_addons(client),
            "wordpress-plugins": fetch_wordpress_plugins(client),
            "homebrew-formulae": fetch_homebrew_formulae(client),
            "scoop": fetch_scoop_buckets(client),
            "alpine-linux": fetch_alpine_packages(client),
            "google-play": fetch_google_play_appbrain(client),
            "jetbrains-marketplace": fetch_jetbrains_plugins(client),
            "gnome-extensions": fetch_gnome_extensions(client),
        }
        names = list(jobs)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    output: dict[str, list[dict[str, Any]]] = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            log.exception("Expanded catalogue %s failed: %s", name, result)
            output[name] = []
        else:
            output[name] = result
            log.info("Expanded catalogue %s: %d records", name, len(result))
    return output
