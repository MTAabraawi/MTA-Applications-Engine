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
    browse = json.loads((indexes / "browse.json").read_text())
    stats = json.loads((indexes / "stats.json").read_text())
    assert "fir" in search
    short_bucket = hashlib.sha256(b"fi").hexdigest()[:4]
    short_search = json.loads((indexes / "search" / f"{short_bucket}.json").read_text())
    assert "fi" in short_search
    assert "firefox" not in search
    assert not stale_bucket.exists()
    manifest = json.loads((tmp_path / "catalogue_data" / "manifest.json").read_text())
    assert manifest["search_index_format"] == "prefix2-3-v2"
    assert len(browse) == 2
    assert stats["total"] == 2
    assert stats["quality"]["fields"]["source_url"]["present"] == 2
    assert browse[0].endswith(":browser")


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
