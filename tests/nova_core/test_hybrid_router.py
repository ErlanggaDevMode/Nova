from unittest.mock import MagicMock
from nova_core.router.hybrid_router import HybridRouter
from nova_core.router.llm_client import LLMClient
from nova_core.models import RoutedResult, ActionRequest

def test_hybrid_router_local_match():
    llm_mock = MagicMock(spec=LLMClient)
    router = HybridRouter(llm_client=llm_mock)
    
    result = router.route_command("show battery", "test_device")
    assert result.path == "local"
    assert result.action_request is not None
    assert result.action_request.action_type == "get_battery"
    assert result.action_request.category == "read_only_info"
    llm_mock.query.assert_not_called()

def test_hybrid_router_cloud_fallback():
    llm_mock = MagicMock(spec=LLMClient)
    llm_mock.query.return_value = (None, "Mock conversational response")
    
    router = HybridRouter(llm_client=llm_mock)
    
    result = router.route_command("summarize my email", "test_device")
    assert result.path == "cloud"
    assert result.action_request is None
    assert result.response_text == "Mock conversational response"
    llm_mock.query.assert_called_once_with("summarize my email", "test_device", None)
