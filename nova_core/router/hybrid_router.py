import time
from datetime import datetime, timezone
from desktop_agent.intent_matcher import IntentMatcher
from nova_core.router.llm_client import LLMClient
from nova_core.models import RoutedResult, IntentMatch, ActionRequest

class HybridRouter:
    def __init__(self, llm_client: LLMClient | None = None, store = None):
        self.intent_matcher = IntentMatcher()
        self.llm_client = llm_client or LLMClient()
        self.store = store

    def _determine_sentiment(self, command: str) -> str:
        cmd_lower = command.lower()
        if any(w in cmd_lower for w in ["urgent", "immediately", "quick", "asap", "run", "fast", "now"]):
            return "urgent"
        elif any(w in cmd_lower for w in ["please", "thank you", "thanks", "hello", "hi"]):
            return "conversational"
        return "neutral"

    def route_command(self, command: str, source_device_id: str, context_store = None) -> RoutedResult:
        """
        Routes the command to local regex patterns or falls back to LLM, timing latency and logging trace performance.
        """
        start_time = time.perf_counter()
        sentiment = self._determine_sentiment(command)
        path = "local"
        confidence = 0.0
        success = True
        
        try:
            # 1. Fast heuristic check: Local regex rule matcher
            local_match = self.intent_matcher.try_match(command)
            if local_match:
                confidence = 1.0
                intent = IntentMatch(
                    action_type=local_match.action_type,
                    category=local_match.category,
                    params=local_match.params,
                    confidence=confidence
                )
                action_req = ActionRequest(
                    action_type=local_match.action_type,
                    category=local_match.category,
                    params=local_match.params,
                    source_device_id=source_device_id,
                    origin="local_match"
                )
                
                if sentiment == "urgent":
                    action_req.params["urgency"] = "high"
                    
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                self._log_performance(command, "local", confidence, duration_ms, sentiment, True)
                
                return RoutedResult(
                    path="local",
                    intent=intent,
                    action_request=action_req,
                    response_text=None
                )

            # 2. Cloud LLM Fallback (wraps retry exception logic)
            path = "cloud"
            try:
                action_request, response_text = self.llm_client.query(command, source_device_id, context_store)
                confidence = 0.9 if action_request else 0.8
            except Exception:
                action_request, response_text = self.llm_client._mock_fallback(command, source_device_id, context_store)
                confidence = 0.5
                success = False

            if action_request and sentiment == "urgent":
                action_request.params["urgency"] = "high"

            duration_ms = (time.perf_counter() - start_time) * 1000.0
            self._log_performance(command, path, confidence, duration_ms, sentiment, success)

            return RoutedResult(
                path=path,
                intent=None,
                action_request=action_request,
                response_text=response_text
            )
        except Exception as ex:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            self._log_performance(command, "local", 0.1, duration_ms, sentiment, False)
            return RoutedResult(
                path="local",
                intent=None,
                action_request=None,
                response_text=f"Routing exception encountered: {str(ex)}"
            )

    def _log_performance(self, command: str, path: str, confidence: float, duration_ms: float, sentiment: str, success: bool):
        if self.store:
            try:
                self.store.log_router_performance(command, path, confidence, duration_ms, sentiment, success)
            except Exception:
                pass
