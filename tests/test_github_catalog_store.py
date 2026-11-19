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
        record(source="winget", external="c", platform="windows", name="Alpha Desktop"),
    ])
    found = store.search(query="alpha", limit=1, offset=0)
    assert len(found) == 1
    assert found[0]["name"] == "Alpha Browser"
    assert len(store.search(platform="windows")) == 1


def test_invalid_records_are_skipped(tmp_path):
    store = GitHubCatalogueStore(tmp_path / "catalogue_data")
    result = store.upsert_many([{"source_key": "x", "external_id": "y"}])
    assert result["skipped"] == 1
