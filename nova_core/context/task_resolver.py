from typing import Optional
from nova_core.context.context_store import ContextStore

class TaskResolver:
    def __init__(self, context_store: ContextStore):
        self.context_store = context_store

    def resolve_task_by_query(self, query: str) -> Optional[str]:
        """
        Resolves the active task context key from context_state matching a user query.
        Matches keywords in the query against active context keys, falling back
        to the most recently updated task if the trigger implies resumption.
        """
        q = query.strip().lower()
        states = self.context_store.list_states()
        
        # Filter for keys starting with "task:"
        task_states = [s for s in states if s["key"].startswith("task:")]
        if not task_states:
            return None

        # Extract potential keyword tokens (length > 3)
        words = [w for w in q.split() if len(w) > 3]
        
        # Try matching keyword tokens to key suffixes (e.g. "trip" in "task:planning_trip")
        for word in words:
            for s in task_states:
                key_suffix = s["key"].split("task:", 1)[1].lower()
                if word in key_suffix or key_suffix in word:
                    return s["key"]

        # Fallback to the most recently updated active task on generic resume phrasing
        if any(trigger in q for trigger in ("continue", "resume", "go on", "finish", "active task")):
            return task_states[0]["key"]

        return None
