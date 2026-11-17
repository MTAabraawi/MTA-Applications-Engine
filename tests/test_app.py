from fastapi.testclient import TestClient
import app as app_module
from app import AppRecord, SessionLocal, app
from catalog_sources import _record, _unique

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_apps_endpoint_returns_list():
    response = client.get("/api/v1/apps")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_catalog_stats_shape():
    response = client.get("/api/v1/catalog-stats")
    assert response.status_code == 200
    payload = response.json()
    assert "total" in payload
    assert "platforms" in payload
    assert "sources" in payload
    assert "quality" in payload
    assert "fields" in payload["quality"]
    assert "sources" in payload["quality"]
    for field in ("icon_url", "description", "summary", "developer", "category", "license", "app_url", "source_url"):
        assert field in payload["quality"]["fields"]
    assert payload["metadata_only"] is True


def test_ingestion_requires_admin_token():
    assert client.post("/api/v1/ingest/fdroid").status_code == 403
    assert client.post("/api/v1/ingest/catalogs").status_code == 403


def test_catalog_ingestion_upserts_and_reports_counts(monkeypatch):
    record = {
        "source_key": "test-source", "external_id": "test-app-001",
        "platform": "web", "name": "Test Application",
        "source_url": "https://example.org/apps/test-app-001",
        "metadata_json": {"test": True},
    }
    monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", "test-token")

    async def fake_fetch():
        return {"test-source": [record]}

    async def fake_fetch_expanded():
        return {}

    monkeypatch.setattr(app_module, "fetch_all_catalogs", fake_fetch)
    monkeypatch.setattr(app_module, "fetch_expanded_catalogs", fake_fetch_expanded)
    try:
        first = client.post("/api/v1/ingest/catalogs", headers={"X-Admin-Token": "test-token"})
        assert first.status_code == 200
        assert first.json()["sources"]["test-source"]["inserted"] == 1
        second = client.post("/api/v1/ingest/catalogs", headers={"X-Admin-Token": "test-token"})
        assert second.status_code == 200
        assert second.json()["sources"]["test-source"]["unchanged"] == 1
    finally:
        db = SessionLocal()
        try:
            row = db.query(AppRecord).filter_by(source_key="test-source", external_id="test-app-001").one_or_none()
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()



def test_sparse_catalogue_refresh_preserves_enriched_fields_and_metadata():
    from app import upsert_records

    source = "enrichment-preservation-test"
    rich = {
        "source_key": source, "external_id": "sample-1", "platform": "ios",
        "name": "Sample App", "developer": "Example Studio",
        "summary": "Short description", "description": "Full rich description",
        "category": "Productivity", "source_url": "https://apps.example.org/sample",
        "app_url": "https://example.org", "icon_url": "https://cdn.example.org/icon.png",
        "metadata_json": {"record_json_url": "https://bluejetty.ai/v1/index/apps/123",
                          "bluejetty_detail": {"rating": 4.8, "rating_count": 120}},
    }
    sparse = dict(rich, developer=None, summary=None, description=None, category=None,
                  app_url=None, icon_url=None,
                  metadata_json={"record_json_url": "https://bluejetty.ai/v1/index/apps/123"})
    db = SessionLocal()
    try:
        first = upsert_records(db, [rich])
        assert first["inserted"] == 1
        second = upsert_records(db, [sparse])
        assert second["unchanged"] == 1
        row = db.query(AppRecord).filter_by(source_key=source, external_id="sample-1").one()
        assert row.developer == "Example Studio"
        assert row.description == "Full rich description"
        assert row.icon_url == "https://cdn.example.org/icon.png"
        assert row.metadata_json["bluejetty_detail"]["rating"] == 4.8
    finally:
        db.query(AppRecord).filter_by(source_key=source).delete(synchronize_session=False)
        db.commit()
        db.close()

def test_record_requires_https_source_and_deduplicates():
    assert _record("x", "1", "web", "Example", "http://example.org") is None
    item = _record("x", "1", "web", "Example", "https://example.org")
    assert item is not None
    assert len(_unique([item, item])) == 1


def test_cloudflare_hyperdrive_binding_builds_postgres_session():
    from types import SimpleNamespace
    from starlette.requests import Request
    from app import get_db

    hyperdrive = SimpleNamespace(
        host="db.example.internal",
        port="5432",
        user="mta_worker",
        password="not-a-real-password",
        database="mta_catalogue",
    )
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "env": SimpleNamespace(HYPERDRIVE=hyperdrive),
    })

    dependency = get_db(request)
    session = next(dependency)
    try:
        bind = session.get_bind()
        assert bind.dialect.name == "postgresql"
        assert bind.dialect.driver == "psycopg"
        assert bind.url.host == "db.example.internal"
        assert bind.url.database == "mta_catalogue"
    finally:
        dependency.close()


def test_cloudflare_worker_admin_secret_is_read_from_request_scope():
    from types import SimpleNamespace
    from starlette.requests import Request
    from fastapi import HTTPException
    from app import require_admin

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v1/ingest/catalogs",
        "headers": [],
        "query_string": b"",
        "env": SimpleNamespace(ADMIN_API_TOKEN="worker-secret"),
    })
    require_admin("worker-secret", request)
    try:
        require_admin("wrong-token", request)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Invalid Worker admin token was accepted")


def test_fdroid_endpoint_uses_shared_normalizer(monkeypatch):
    record = {
        "source_key": "fdroid", "external_id": "org.example.endpoint-test",
        "platform": "android", "name": "F-Droid Endpoint Test",
        "source_url": "https://f-droid.org/packages/org.example.endpoint-test/",
        "metadata_json": {},
    }
    monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", "test-token")

    async def fake_fetch(client):
        return [record]

    monkeypatch.setattr(app_module, "fetch_fdroid", fake_fetch)
    try:
        response = client.post(
            "/api/v1/ingest/fdroid",
            headers={"X-Admin-Token": "test-token"},
        )
        assert response.status_code == 200
        assert response.json()["discovered"] == 1
        assert response.json()["inserted"] == 1
    finally:
        db = SessionLocal()
        try:
            row = db.query(AppRecord).filter_by(
                source_key="fdroid",
                external_id="org.example.endpoint-test",
            ).one_or_none()
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()



def test_bulk_upsert_records_batches_and_is_idempotent():
    from app import upsert_records

    source = "bulk-upsert-regression"
    records = [
        {
            "source_key": source,
            "external_id": f"sample-{index:04d}",
            "platform": "web",
            "name": f"Sample Application {index}",
            "summary": "initial summary",
            "source_url": f"https://example.org/apps/{index}",
            "metadata_json": {"index": index},
        }
        for index in range(501)
    ]
    db = SessionLocal()
    try:
        first = upsert_records(db, records)
        assert first == {"inserted": 501, "updated": 0, "unchanged": 0, "skipped": 0}

        second = upsert_records(db, records)
        assert second == {"inserted": 0, "updated": 0, "unchanged": 501, "skipped": 0}

        revised = dict(records[0], summary="revised summary")
        third = upsert_records(db, [revised, *records[1:]])
        assert third == {"inserted": 0, "updated": 1, "unchanged": 500, "skipped": 0}
    finally:
        db.query(AppRecord).filter_by(source_key=source).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_chocolatey_page_size_is_capped_to_supported_cli_limit(monkeypatch):
    import asyncio
    import open_catalog_sources

    monkeypatch.setenv("CHOCOLATEY_PAGE_SIZE", "1000")
    monkeypatch.setenv("CHOCOLATEY_MAX_PAGES", "10")
    monkeypatch.setattr(open_catalog_sources.shutil, "which", lambda _: "choco.exe")
    requested = []

    def fake_search(choco, page, page_size):
        requested.append(page_size)
        return []

    monkeypatch.setattr(open_catalog_sources, "_chocolatey_search_page", fake_search)
    assert asyncio.run(open_catalog_sources.fetch_chocolatey()) == []
    assert requested == [100]
