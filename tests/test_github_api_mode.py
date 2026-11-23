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

    rows = api.list_apps(q="browser", platform="android", source="fdroid", limit=10, offset=0, db=None)
    assert len(rows) == 1
    assert rows[0]["id"] == "fdroid:org.example.browser"
    assert rows[0]["source"] == "fdroid"

    stats = api.catalog_stats(db=None)
    assert stats["total"] == 1
    assert stats["platforms"] == {"android": 1}
    assert stats["sources"] == {"fdroid": 1}



def test_fastapi_exact_source_id_lookup_bypasses_catalogue_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "GITHUB_STORAGE", True)
    monkeypatch.setattr(api, "GITHUB_CATALOGUE_DIR", str(tmp_path / "catalogue_data"))
    store = GitHubCatalogueStore(tmp_path / "catalogue_data", shard_count=8)
    store.upsert_many([
        {
            "source_key": "fdroid", "external_id": "org.example.alpha",
            "platform": "android", "name": "Alpha App",
            "source_url": "https://example.org/alpha",
        },
        {
            "source_key": "fdroid", "external_id": "org.example.target",
            "platform": "android", "name": "Target App",
            "source_url": "https://example.org/target",
        },
    ])

    rows = api.list_apps(q=None, platform=None, source="fdroid", external_id="org.example.target", limit=1, offset=0, db=None)
    assert len(rows) == 1
    assert rows[0]["external_id"] == "org.example.target"
    assert rows[0]["name"] == "Target App"

    assert api.list_apps(q=None, platform=None, source="wrong-source", external_id="org.example.target", limit=1, offset=0, db=None) == []
    assert api.list_apps(q=None, platform=None, source=None, external_id="org.example.target", limit=1, offset=0, db=None) == []
