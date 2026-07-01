import pytest
import os
import json
from pathlib import Path
from fastapi.testclient import TestClient
from nova_core.main import app
from nova_core.db.store import DatabaseStore

def test_migrations_tracking_and_rollback(tmp_path, monkeypatch):
    # Set up custom sqlite db path to avoid dirtying local active databases
    db_file = tmp_path / "test_mig.db"
    store = DatabaseStore(db_path=db_file)
    
    # 1. Verify schema_migrations table is created and version tracking logs are clean
    with store.get_connection() as conn:
        res = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
        # Should have run existing project migrations (e.g. 0003_automation_rules)
        assert list(res)[0] > 0

    # 2. Write a dummy test migration and rollback script
    migrations_dir = Path(__file__).parent.parent.parent / "nova_core" / "db" / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    
    migration_file = migrations_dir / "9999_test_dummy.sql"
    rollback_file = migrations_dir / "9999_test_dummy.rollback.sql"
    
    # Create test table
    migration_file.write_text("CREATE TABLE test_dummy (id INTEGER PRIMARY KEY);", encoding="utf-8")
    # Rollback drop table
    rollback_file.write_text("DROP TABLE test_dummy;", encoding="utf-8")
    
    try:
        # Run init to apply the new migration
        store.init_db()
        
        # Verify table exists
        with store.get_connection() as conn:
            conn.execute("INSERT INTO test_dummy (id) VALUES (42)")
            res = conn.execute("SELECT id FROM test_dummy").fetchone()
            assert list(res)[0] == 42
            
        # Verify migration is registered in schema_migrations
        with store.get_connection() as conn:
            row = conn.execute("SELECT 1 FROM schema_migrations WHERE version='9999_test_dummy'").fetchone()
            assert row is not None

        # 3. Rollback the migration
        success = store.rollback_migration("9999_test_dummy")
        assert success is True
        
        # Verify table is dropped (should raise error)
        with pytest.raises(Exception):
            with store.get_connection() as conn:
                conn.execute("SELECT * FROM test_dummy")

        # Verify entry deleted from tracking
        with store.get_connection() as conn:
            row = conn.execute("SELECT 1 FROM schema_migrations WHERE version='9999_test_dummy'").fetchone()
            assert row is None
            
    finally:
        # Clean up files
        if migration_file.exists():
            migration_file.unlink()
        if rollback_file.exists():
            rollback_file.unlink()

def test_db_validation_backup_and_vacuum(tmp_path):
    db_file = tmp_path / "test_maintenance.db"
    store = DatabaseStore(db_path=db_file)
    
    # 1. Test database integrity validate
    val_res = store.validate_database_integrity()
    assert val_res["success"] is True
    assert val_res["status"] == "healthy"
    
    # Register device to ensure some data is in tables
    store.register_device("dev_1", "Test Dev", "test", {"cap": True})
    
    # 2. Test JSON export backup
    backup_file = tmp_path / "backup.json"
    exp_res = store.export_data_to_json(backup_file)
    assert exp_res["success"] is True
    assert backup_file.exists()
    
    # 3. Test JSON import restore seeding
    imp_res = store.import_data_from_json(backup_file)
    assert imp_res["success"] is True
    dev = store.get_device("dev_1")
    assert dev is not None
    assert dev["name"] == "Test Dev"
    
    # 4. Test database vacuum optimization runs
    vac_res = store.vacuum_database()
    assert vac_res["success"] is True
    assert vac_res["duration_seconds"] >= 0

def test_migration_endpoints(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nayakacode")
    client = TestClient(app)

    response = client.post("/auth/token", json={"username": "admin", "password": "nayakacode"})
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # Test validate API
    response = client.get("/db/validate", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    
    # Test vacuum API
    response = client.post("/db/vacuum", headers=headers)
    assert response.status_code == 200
    assert response.json()["success"] is True
    
    # Test backup API
    response = client.post("/db/backup", headers=headers)
    assert response.status_code == 200
    assert response.json()["success"] is True
    backup_file = Path("nova_backup.json")
    assert backup_file.exists()
    backup_file.unlink()
