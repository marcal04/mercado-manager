from fastapi.testclient import TestClient
from app.main import app

def test_health_and_approval_flow():
    with TestClient(app) as client:
        assert client.get("/health").json()["marketplace_write_enabled"] is False
        chat = client.post("/api/v1/chat", json={"message": "Sugira um preço para iPhone"}).json()
        assert chat["approval_id"]
        decision = client.post(f"/api/v1/approvals/{chat['approval_id']}/decision", json={"approved": True, "actor": "test"})
        assert decision.status_code == 200
        assert decision.json()["status"] == "approved"

def test_product_creation():
    with TestClient(app) as client:
        response = client.post("/api/v1/products", json={"sku": "TEST-001", "name": "Smartphone Test", "brand": "Test", "reference_price": 999.9, "stock": 1})
        assert response.status_code in (201, 409)
