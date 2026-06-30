import pytest
from nova_core.db.store import DatabaseStore
from nova_core.context.context_store import ContextStore
from nova_core.context.task_resolver import TaskResolver

def test_task_resolver(tmp_path):
    db_file = tmp_path / "test_resolver.db"
    store = DatabaseStore(db_path=db_file)
    context_store = ContextStore(store)
    resolver = TaskResolver(context_store)

    assert resolver.resolve_task_by_query("continue planning the trip") is None

    context_store.set_state("task:planning_trip", {"destination": "Bali"}, "dev1")
    context_store.set_state("task:writing_code", {"repo": "Nova"}, "dev1")

    assert resolver.resolve_task_by_query("continue planning that trip") == "task:planning_trip"
    assert resolver.resolve_task_by_query("resume coding work") == "task:writing_code"

    # Falls back to the most recently updated task
    assert resolver.resolve_task_by_query("continue please") == "task:writing_code"
