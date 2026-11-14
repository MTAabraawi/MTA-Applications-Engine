from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_apps_endpoint_returns_empty_list_initially():
    response = client.get("/api/v1/apps")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_ingestion_requires_admin_token():
    response = client.post("/api/v1/ingest/fdroid")
    assert response.status_code == 403
