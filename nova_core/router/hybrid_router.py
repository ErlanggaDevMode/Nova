from desktop_agent.intent_matcher import IntentMatcher
from nova_core.router.llm_client import LLMClient
from nova_core.models import RoutedResult, IntentMatch, ActionRequest

class HybridRouter:
    def __init__(self, llm_client: LLMClient | None = None):
        self.intent_matcher = IntentMatcher()
        self.llm_client = llm_client or LLMClient()

    def route_command(self, command: str, source_device_id: str, context_store = None) -> RoutedResult:
        """
        Routes the command to local regex patterns or falls back to LLM.
        """
        # Try local rule-based match first
        local_match = self.intent_matcher.try_match(command)
        if local_match:
            intent = IntentMatch(
                action_type=local_match.action_type,
                category=local_match.category,
                params=local_match.params,
                confidence=1.0
            )
            action_req = ActionRequest(
                action_type=local_match.action_type,
                category=local_match.category,
                params=local_match.params,
                source_device_id=source_device_id,
                origin="local_match"
            )
            return RoutedResult(
                path="local",
                intent=intent,
                action_request=action_req,
                response_text=None
            )

        # Fallback to Cloud LLM
        action_request, response_text = self.llm_client.query(command, source_device_id, context_store)
        return RoutedResult(
            path="cloud",
            intent=None,
            action_request=action_request,
            response_text=response_text
        )
