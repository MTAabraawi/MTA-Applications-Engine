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

    monkeypatch.setattr(app_module, "fetch_all_catalogs", fake_fetch)
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


def test_record_requires_https_source_and_deduplicates():
    assert _record("x", "1", "web", "Example", "http://example.org") is None
    item = _record("x", "1", "web", "Example", "https://example.org")
    assert item is not None
    assert len(_unique([item, item])) == 1
