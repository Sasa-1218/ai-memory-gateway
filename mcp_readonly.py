"""Small stateless read-only MCP JSON-RPC dispatcher.

No direct mutation tools. Transport/authentication handled by gateway route.
"""
import json


TOOLS = [
    {
        "name": "search_memories",
        "description": "Search a few active memories without modifying last_accessed timestamps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["query"], "additionalProperties": False,
        },
    },
    {
        "name": "memory_lineage",
        "description": "Read a memory merge lineage. Memory IDs are not verified conversation message IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "integer", "minimum": 1}},
            "required": ["memory_id"], "additionalProperties": False,
        },
    },
]


def make_reply(request_id, *, result=None, error=None):
    response = {"jsonrpc": "2.0", "id": request_id}
    response["error" if error is not None else "result"] = error if error is not None else result
    return response


def err(code, message):
    return {"code": code, "message": message}


async def dispatch(request, *, search, lineage):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return make_reply(None, error=err(-32600, "Invalid Request"))
    request_id = request.get("id")
    method = request.get("method")
    if method == "notifications/initialized" and request_id is None:
        return None
    if request_id is None or type(request_id) not in (int, str):
        return make_reply(None, error=err(-32600, "Invalid Request"))
    if method == "initialize":
        return make_reply(request_id, result={
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "rora-gateway-readonly", "version": "1.0"},
        })
    if method == "ping":
        return make_reply(request_id, result={})
    if method == "tools/list":
        return make_reply(request_id, result={"tools": TOOLS})
    if method != "tools/call":
        return make_reply(request_id, error=err(-32601, "Method not found"))
    params = request.get("params")
    if not isinstance(params, dict):
        return make_reply(request_id, error=err(-32602, "Invalid params"))
    name, args = params.get("name"), params.get("arguments", {})
    if not isinstance(args, dict):
        return make_reply(request_id, error=err(-32602, "Invalid arguments"))
    if name == "search_memories":
        query = args.get("query")
        limit = args.get("limit", 3)
        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or
                type(limit) is not int or not 1 <= limit <= 5):
            return make_reply(request_id, error=err(-32602, "Invalid search arguments"))
        rows = await search(query.strip(), limit=limit,
                            touch_accessed=False, log_diagnostics=False)
        records = []
        for row in list(rows or [])[:limit]:
            records.append({
                "id": row["id"],
                "content": str(row["content"] or "")[:900],
                "importance": row["importance"],
            })
        payload = {"memories": records}
    elif name == "memory_lineage":
        memory_id = args.get("memory_id")
        if type(memory_id) is not int or memory_id <= 0:
            return make_reply(request_id, error=err(-32602, "Invalid memory ID"))
        payload = await lineage(memory_id)
    else:
        return make_reply(request_id, error=err(-32602, "Unknown tool"))
    return make_reply(request_id, result={
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
        "isError": False,
    })
