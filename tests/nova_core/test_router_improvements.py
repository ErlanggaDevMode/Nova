import pytest
from unittest.mock import patch, MagicMock
from nova_core.db.store import DatabaseStore
from nova_core.router.hybrid_router import HybridRouter

def test_router_fast_local_match_and_urgency(tmp_path):
    db_file = tmp_path / "test_router.db"
    store = DatabaseStore(db_path=db_file)
    router = HybridRouter(store=store)
    
    # 1. Test local regex exact command matches instantly bypass cloud LLM
    # Command 'get battery' maps to a local match in IntentMatcher
    res = router.route_command("get battery", "device_1")
    assert res.path == "local"
    assert res.intent.confidence == 1.0
    assert res.action_request.action_type == "get_battery"
    
    # 2. Test urgent sentiment parsing adds urgency = 'high' parameter automatically
    res_urgent = router.route_command("get battery immediately!", "device_1")
    assert res_urgent.action_request.params.get("urgency") == "high"

    # 3. Verify metrics are written to DB
    metrics = store.get_router_performance_metrics()
    assert metrics["count"] == 2
    assert metrics["success_rate"] == 1.0
    assert metrics["avg_confidence"] >= 0.9

def test_router_cloud_latency_tracing_and_fallback(tmp_path):
    db_file = tmp_path / "test_router_cloud.db"
    store = DatabaseStore(db_path=db_file)
    
    mock_llm_client = MagicMock()
    # Mock exception on cloud LLM query to test router fallback resilience
    mock_llm_client.query.side_effect = Exception("Cloud rate limit exceeded")
    
    # Mock stub fallback to return action request
    mock_llm_client._mock_fallback.return_value = (None, "Mock text fallback")
    
    router = HybridRouter(llm_client=mock_llm_client, store=store)
    
    # Execute query that doesn't match locally -> falls back to cloud
    res = router.route_command("calculate complex formulas", "device_1")
    assert res.path == "cloud"
    assert res.response_text == "Mock text fallback"
    
    # Verify metrics logged it as unsuccess (success = False)
    metrics = store.get_router_performance_metrics()
    assert metrics["count"] == 1
    assert metrics["success_rate"] == 0.0
