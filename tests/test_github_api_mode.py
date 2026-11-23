import app as api
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
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

    exact = api.list_apps(source="fdroid", external_id="org.example.browser", limit=1, offset=0, db=None)
    assert len(exact) == 1
    assert exact[0]["external_id"] == "org.example.browser"
    assert exact[0]["name"] == "Example Browser"
    assert api.list_apps(source="fdroid", external_id="missing", limit=1, offset=0, db=None) == []

    stats = api.catalog_stats(db=None)
    assert stats["total"] == 1
    assert stats["platforms"] == {"android": 1}
    assert stats["sources"] == {"fdroid": 1}


def test_fastapi_exact_lookup_works_with_sql_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "GITHUB_STORAGE", False)
    engine = create_engine("sqlite:///" + str(tmp_path / "exact-lookup.db"))
    api.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        api.upsert_records(db, [{
            "source_key": "fdroid",
            "external_id": "org.example.browser",
            "platform": "android",
            "name": "Example Browser",
            "source_url": "https://example.org/browser",
        }])
        exact = api.list_apps(source="fdroid", external_id="org.example.browser", limit=1, offset=0, db=db)
        assert len(exact) == 1
        assert exact[0]["external_id"] == "org.example.browser"
        assert api.list_apps(source="fdroid", external_id="missing", limit=1, offset=0, db=db) == []
