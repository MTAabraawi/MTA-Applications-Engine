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
