from fastapi import APIRouter, Request
import json

router = APIRouter()

@router.get("/history")
async def get_history(request: Request, limit: int = 50):
    store = request.app.state.store
    
    sql = """
    SELECT 
        c.id as command_id,
        c.raw_text,
        c.source_device_id,
        c.routed_path,
        c.created_at as command_created_at,
        a.id as action_id,
        a.action_type,
        a.category,
        a.params,
        a.permission_decision,
        a.executed,
        a.executed_at,
        a.result
    FROM commands c
    LEFT JOIN actions a ON a.command_id = c.id
    ORDER BY c.created_at DESC
    LIMIT ?
    """
    
    with store.get_connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
        history = []
        for r in rows:
            row_dict = dict(r)
            if row_dict.get("params"):
                row_dict["params"] = json.loads(row_dict["params"])
            if row_dict.get("permission_decision"):
                row_dict["permission_decision"] = json.loads(row_dict["permission_decision"])
            if row_dict.get("result"):
                row_dict["result"] = json.loads(row_dict["result"])
            history.append(row_dict)
            
        return {"success": True, "history": history}
