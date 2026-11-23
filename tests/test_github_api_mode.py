import app as api
from github_catalog_store import GitHubCatalogueStore


def test_fastapi_reads_github_catalogue_without_database(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "GITHUB_STORAGE", True)
    monkeypatch.setattr(api, "GITHUB_CATALOGUE_DIR", str(tmp_path / "catalogue_data"))
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    store.upsert_many([{
        "source_key": "fdroid",
        "external_id": "org.example.browser",
        "platform": "android",
        "name": "Example Browser",
        "summary": "A sample browser",
        "source_url": "https://example.org/browser",
    }])

    rows = api.list_apps(q="browser", platform="android", source="fdroid", external_id=None, limit=10, offset=0, db=None)
    assert len(rows) == 1
    assert rows[0]["id"] == "fdroid:org.example.browser"
    assert rows[0]["source"] == "fdroid"

    stats = api.catalog_stats(db=None)
    assert stats["total"] == 1
    assert stats["platforms"] == {"android": 1}
    assert stats["sources"] == {"fdroid": 1}



def test_fastapi_supports_exact_source_external_id_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "GITHUB_STORAGE", True)
    monkeypatch.setattr(api, "GITHUB_CATALOGUE_DIR", str(tmp_path / "catalogue_data"))
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    store.upsert_many([
        {"source_key": "fdroid", "external_id": "org.example.same", "platform": "android", "name": "F-Droid record", "source_url": "https://example.org/fdroid"},
        {"source_key": "izzyondroid", "external_id": "org.example.same", "platform": "android", "name": "Izzy record", "source_url": "https://example.org/izzy"},
    ])

    rows = api.list_apps(q=None, platform=None, source="izzyondroid", external_id="org.example.same", limit=50, offset=0, db=None)
    assert len(rows) == 1
    assert rows[0]["name"] == "Izzy record"
    assert rows[0]["source"] == "izzyondroid"
    assert api.list_apps(q=None, platform=None, source=None, external_id="org.example.same", limit=50, offset=0, db=None) == []
    assert api.list_apps(q=None, platform=None, source="fdroid", external_id="missing", limit=50, offset=0, db=None) == []
