import hashlib
import json

from github_catalog_store import GitHubCatalogueStore


def record(source="fdroid", external="org.example.app", **extra):
    return {
        "source_key": source,
        "external_id": external,
        "platform": "android",
        "name": "Example App",
        "source_url": "https://example.org/app",
        "metadata_json": {"upstream": {"version": "1"}},
        **extra,
    }


def test_upsert_roundtrip_and_indexes(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    result = store.upsert_many([record()])
    assert result["inserted"] == 1
    assert store.get("fdroid", "org.example.app")["name"] == "Example App"
    stats = store.stats()
    assert stats["total"] == 1
    assert stats["platforms"] == {"android": 1}
    assert stats["sources"] == {"fdroid": 1}
    assert json.loads((tmp_path / "catalogue_data" / "manifest.json").read_text())["schema_version"] == 1


def test_sparse_refresh_preserves_enriched_values(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    store.upsert_many([record(description="Rich description", metadata_json={"enrichment": {"status": "done"}})])
    store.upsert_many([record(description=None, metadata_json={"upstream": {"version": "2"}})])
    saved = store.get("fdroid", "org.example.app")
    assert saved["description"] == "Rich description"
    assert saved["metadata_json"]["enrichment"]["status"] == "done"
    assert saved["metadata_json"]["upstream"]["version"] == "2"


def test_search_filters_and_paginates(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    store.upsert_many([
        record(external="a", name="Alpha Browser"),
        record(external="b", name="Beta Editor"),
        record(external="firefox", name="Mozilla Firefox"),
        record(source="winget", external="c", platform="windows", name="Alpha Desktop"),
    ])
    found = store.search(query="alpha", limit=1, offset=0)
    assert len(found) == 1
    assert found[0]["name"] == "Alpha Browser"
    assert len(store.search(platform="windows")) == 1
    # Partial token-prefix queries remain supported by compact prefix postings.
    assert [row["name"] for row in store.search(query="firef")] == ["Mozilla Firefox"]
    assert [row["name"] for row in store.search(query="fi")] == ["Mozilla Firefox"]


def test_invalid_records_are_skipped(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data")
    result = store.upsert_many([{"source_key": "x", "external_id": "y"}])
    assert result["skipped"] == 1


def test_rebuild_indexes_creates_browse_search_and_quality_stats(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    store.upsert_many([
        record(external="firefox", name="Mozilla Firefox", developer="Mozilla"),
        record(external="browser", name="Browser Tools", developer="Example"),
    ])
    indexes = tmp_path / "catalogue_data" / "indexes"
    stale_bucket = indexes / "search" / "obsolete.json"
    stale_bucket.write_text("{}", encoding="utf-8")
    store.rebuild_indexes()
    bucket = hashlib.sha256(b"fir").hexdigest()[:4]
    search = json.loads((indexes / "search" / f"{bucket}.json").read_text())
    manifest = json.loads((tmp_path / "catalogue_data" / "manifest.json").read_text())
    source_id = hashlib.sha256(b"fdroid").hexdigest()[:16]
    platform_id = hashlib.sha256(b"android").hexdigest()[:16]
    browse = json.loads((indexes / "browse" / "source-platform" / source_id / platform_id / "000000.json").read_text())
    stats = json.loads((indexes / "stats.json").read_text())
    assert "fir" in search
    short_bucket = hashlib.sha256(b"fi").hexdigest()[:4]
    short_search = json.loads((indexes / "search" / f"{short_bucket}.json").read_text())
    assert "fi" in short_search
    assert "firefox" not in search
    assert not stale_bucket.exists()
    manifest = json.loads((tmp_path / "catalogue_data" / "manifest.json").read_text())
    assert manifest["search_index_format"] == "prefix2-3-v2"
    assert manifest["browse_index_format"] == "paged-v1"
    assert manifest["browse_page_size"] == 1000
    assert len(browse) == 2
    assert not (indexes / "browse.json").exists()
    assert not (indexes / "source.json").exists()
    assert not (indexes / "platform.json").exists()
    assert stats["total"] == 2
    assert stats["quality"]["fields"]["source_url"]["present"] == 2
    assert browse[0].endswith(":browser")
    catalogue = json.loads((indexes / "catalogue.json").read_text())
    assert catalogue["sources"]["fdroid"] == source_id
    assert catalogue["platforms"]["android"] == platform_id
    assert json.loads((indexes / "stats.json").read_text())["source_platforms"]["fdroid"]["android"] == 2



def test_rebuild_indexes_reports_global_ai_discovery_coverage(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    store.upsert_many([
        record(source="huggingface-ai-spaces", external="cn-space", platform="web",
               metadata_json={"discovery_language": "zh-CN", "origin_country": None,
                              "catalog_type": "hosted-ai-web-application"}),
        record(source="github-ai-applications", external="ru-app", platform="source-code",
               metadata_json={"discovery_language": "ru", "origin_country": None,
                              "catalog_type": "open-source-ai-application-project"}),
        record(source="verified-publisher", external="country-app",
               metadata_json={"discovery_language": "en", "origin_country": "CN",
                              "origin_country_verified": True, "catalog_type": "verified-app"}),
    ])
    store.rebuild_indexes()
    stats = json.loads((tmp_path / "catalogue_data" / "indexes" / "stats.json").read_text())
    assert stats["ai_discovery"]["discovery_languages"] == {"zh-CN": 1, "ru": 1, "en": 1}
    assert stats["ai_discovery"]["verified_origin_countries"] == {"CN": 1}
    assert stats["ai_discovery"]["catalog_types"]["hosted-ai-web-application"] == 1

def test_search_normalization_handles_underscores_unicode_and_arabic(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    store.upsert_many([
        record(external="underscore", name="MTA_Net_Blocker"),
        record(external="german", name="Straße Browser"),
        record(external="arabic", name="مكتبة التطبيقات"),
    ])

    assert [row["name"] for row in store.search(query="mta_net")] == ["MTA_Net_Blocker"]
    assert [row["name"] for row in store.search(query="straße")] == ["Straße Browser"]
    assert [row["name"] for row in store.search(query="مكتبة")] == ["مكتبة التطبيقات"]


def test_browse_indexes_split_into_pages_of_one_thousand(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=16)
    store.upsert_many(
        [record(external=str(index), name=f"App {index:04d}") for index in range(1001)],
        rebuild_indexes=False,
    )
    manifest = store.rebuild_indexes()
    source_id = hashlib.sha256(b"fdroid").hexdigest()[:16]
    platform_id = hashlib.sha256(b"android").hexdigest()[:16]
    browse = tmp_path / "catalogue_data" / "indexes" / "browse" / "source-platform" / source_id / platform_id
    first_page = json.loads((browse / "000000.json").read_text())
    second_page = json.loads((browse / "000001.json").read_text())

    assert manifest["browse_page_size"] == 1000
    assert len(first_page) == 1000
    assert len(second_page) == 1
    assert first_page[0].endswith(":0")
    assert second_page[0].endswith(":1000")


def test_bluejetty_icon_crossmatch_reads_official_store_urls_from_record():
    from scripts.enrich_github_catalog import crossmatch_icons

    records = [{
        "source_key": "bluejetty",
        "external_id": "bluejetty-ios-app",
        "platform": "ios",
        "source_url": "https://apps.apple.com/us/app/example/id123456789",
        "app_url": "https://example.org",
        "icon_url": None,
        "metadata_json": {},
    }]
    providers = {
        ("ios", "123456789"): {
            "source_key": "apple-itunes",
            "icon_url": "https://cdn.example.org/app-icon.png",
        }
    }

    updates, counts = crossmatch_icons(records, providers, "2026-11-25T00:00:00+00:00")

    assert counts == {"scanned": 1, "matched": 1, "unmatched": 0, "ambiguous": 0}
    assert updates[0]["icon_url"] == "https://cdn.example.org/app-icon.png"
    assert updates[0]["metadata_json"]["icon_crossmatch"]["method"] == "exact_official_store_listing_id"


def test_icon_crossmatch_covers_mobile_sources_beyond_bluejetty():
    from scripts.enrich_github_catalog import crossmatch_icons

    records = [{
        "source_key": "openuse",
        "external_id": "openuse-android-123",
        "platform": "android",
        "source_url": "https://example.org/catalogue/openuse-android-123",
        "app_url": "https://play.google.com/store/apps/details?id=org.example.mobile",
        "icon_url": None,
        "metadata_json": {},
    }]
    providers = {
        ("android", "org.example.mobile"): {
            "source_key": "fdroid",
            "icon_url": "https://cdn.example.org/org-example-mobile.png",
        }
    }

    updates, counts = crossmatch_icons(records, providers, "2026-11-25T00:00:00+00:00")

    assert counts == {"scanned": 1, "matched": 1, "unmatched": 0, "ambiguous": 0}
    assert updates[0]["source_key"] == "openuse"
    assert updates[0]["icon_url"] == "https://cdn.example.org/org-example-mobile.png"
    assert updates[0]["metadata_json"]["icon_crossmatch"]["provider_source"] == "fdroid"
