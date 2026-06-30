import pytest
from nova_core.db.store import DatabaseStore
from nova_core.context.context_store import ContextStore

def test_context_store_crud(tmp_path):
    db_file = tmp_path / "test_context.db"
    store = DatabaseStore(db_path=db_file)
    context_store = ContextStore(store)

    assert context_store.get_state("non_existent") is None

    context_store.set_state("task:plan_trip", {"status": "planning", "destination": "Bali"}, "device_1")
    state = context_store.get_state("task:plan_trip")
    assert state == {"status": "planning", "destination": "Bali"}

    states = context_store.list_states()
    assert len(states) == 1
    assert states[0]["key"] == "task:plan_trip"
    assert states[0]["value"] == {"status": "planning", "destination": "Bali"}
    assert states[0]["updated_by_device_id"] == "device_1"

    prompt_str = context_store.get_system_prompt_context()
    assert "ACTIVE TASK CONTEXT" in prompt_str
    assert "Bali" in prompt_str

    context_store.delete_state("task:plan_trip")
    assert context_store.get_state("task:plan_trip") is None
