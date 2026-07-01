import pytest
import threading
import time
from fastapi.testclient import TestClient
from nova_core.main import app
from nova_core.db.store import DatabaseStore

def test_db_pool_concurrency(monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    store = DatabaseStore()
    
    # Run multiple execution threads sharing the same store to verify pool concurrency
    def execute_worker():
        for _ in range(5):
            with store.get_connection() as conn:
                res = conn.execute("SELECT 1").fetchone()
                assert list(res)[0] == 1
                time.sleep(0.05)

    threads = [threading.Thread(target=execute_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    metrics = store.get_metrics()
    assert metrics["query_count"] >= 25
    assert metrics["total_query_time_seconds"] > 0
    assert metrics["db_type"] == "sqlite"

def test_database_status_endpoint(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nayakacode")
    client = TestClient(app)

    # 1. Access without authorization -> 401
    response = client.get("/database/status")
    assert response.status_code == 401

    # 2. Access with token -> 200 containing status metrics
    response = client.post("/auth/token", json={"username": "admin", "password": "nayakacode"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/database/status", headers=headers)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True
    assert data["status"] == "healthy"
    assert "metrics" in data
    assert data["metrics"]["db_type"] == "sqlite"
