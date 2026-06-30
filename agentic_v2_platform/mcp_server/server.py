# =========================================================
# MCP Server — V2 DBA Operations Platform
#
# Exposes DBA diagnostic and remediation capabilities as
# MCP tools over a JSON-RPC 2.0 / SSE transport so that
# any MCP-compatible AI client (Claude Desktop, VS Code
# Copilot, etc.) can invoke them directly.
#
# Transport: FastAPI + Server-Sent Events (SSE)
# Spec:      https://modelcontextprotocol.io
# =========================================================

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agentic_v2_platform.config.settings import get_settings
from agentic_v2_platform.database.dba_queries import (
    check_blocking_sessions,
    check_cpu_pressure,
    check_long_running_queries,
    check_backup_status,
    check_database_space,
    check_failed_jobs,
    check_index_fragmentation,
    get_active_spids,
)
from agentic_v2_platform.database.multi_db_router import get_router


# =========================================================
# MCP TOOL REGISTRY
# Declared as pure data so the AI can introspect them.
# =========================================================

MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_database_targets",
        "description": "List all registered SQL Server targets (aliases, servers, databases).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "check_blocking_sessions",
        "description": "Detect blocking session chains on a target SQL Server. Returns blocker SPID, blocked SPID, wait time, and SQL text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Database alias (e.g. PROD_01)"}
            },
            "required": ["alias"],
        },
    },
    {
        "name": "check_cpu_pressure",
        "description": "Return top CPU-consuming sessions on a target SQL Server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string"}
            },
            "required": ["alias"],
        },
    },
    {
        "name": "check_long_running_queries",
        "description": "Find queries running longer than threshold_minutes on a target.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string"},
                "threshold_minutes": {"type": "integer", "default": 5},
            },
            "required": ["alias"],
        },
    },
    {
        "name": "check_backup_status",
        "description": "Return the latest backup timestamp per database on a target.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string"}
            },
            "required": ["alias"],
        },
    },
    {
        "name": "check_database_space",
        "description": "Return file sizes and free space on a target database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string"}
            },
            "required": ["alias"],
        },
    },
    {
        "name": "check_failed_jobs",
        "description": "List recently failed SQL Agent jobs on a target.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string"}
            },
            "required": ["alias"],
        },
    },
    {
        "name": "check_index_fragmentation",
        "description": "Return fragmented indexes above threshold_pct on a target.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string"},
                "threshold_pct": {"type": "number", "default": 30.0},
            },
            "required": ["alias"],
        },
    },
    {
        "name": "get_active_spids",
        "description": "List all active user sessions (SPIDs) on a target.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string"}
            },
            "required": ["alias"],
        },
    },
]


# =========================================================
# TOOL DISPATCHER
# Maps tool name → handler.  Returns serialisable result.
# =========================================================

def _dispatch(name: str, args: Dict[str, Any]) -> Any:
    alias = args.get("alias", "DEFAULT")

    if name == "list_database_targets":
        return get_router().list_targets()
    if name == "check_blocking_sessions":
        return check_blocking_sessions(alias)
    if name == "check_cpu_pressure":
        return check_cpu_pressure(alias)
    if name == "check_long_running_queries":
        return check_long_running_queries(alias, args.get("threshold_minutes", 5))
    if name == "check_backup_status":
        return check_backup_status(alias)
    if name == "check_database_space":
        return check_database_space(alias)
    if name == "check_failed_jobs":
        return check_failed_jobs(alias)
    if name == "check_index_fragmentation":
        return check_index_fragmentation(alias, args.get("threshold_pct", 30.0))
    if name == "get_active_spids":
        return get_active_spids(alias)

    raise ValueError(f"Unknown MCP tool: {name}")


# =========================================================
# FASTAPI APP
# =========================================================

app = FastAPI(title="DBA-Ops MCP Server V2", version="2.0.0")


# ----------------------------------------------------------
# MCP: initialize / capabilities handshake
# ----------------------------------------------------------

class RpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str
    params: Optional[Dict[str, Any]] = None


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    rpc = RpcRequest(**body)
    req_id = rpc.id or str(uuid.uuid4())

    if rpc.method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "dba-ops-mcp-server",
                    "version": "2.0.0",
                },
            },
        }

    if rpc.method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS},
        }

    if rpc.method == "tools/call":
        params = rpc.params or {}
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        try:
            data = _dispatch(tool_name, tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(data, indent=2, default=str),
                        }
                    ],
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {rpc.method}"},
    }


# ----------------------------------------------------------
# SSE endpoint — optional streaming transport
# ----------------------------------------------------------

async def _event_stream(tool_name: str, tool_args: Dict) -> AsyncGenerator[str, None]:
    try:
        data = _dispatch(tool_name, tool_args)
        payload = json.dumps({"result": data, "timestamp": datetime.utcnow().isoformat()}, default=str)
        yield f"data: {payload}\n\n"
    except Exception as exc:
        error_payload = json.dumps({"error": str(exc)})
        yield f"data: {error_payload}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/mcp/stream/{tool_name}")
async def mcp_stream(tool_name: str, request: Request):
    args = dict(request.query_params)
    return StreamingResponse(
        _event_stream(tool_name, args),
        media_type="text/event-stream",
    )


# ----------------------------------------------------------
# Health check
# ----------------------------------------------------------

@app.get("/health")
async def health():
    settings = get_settings()
    return {
        "status": "ok",
        "server": "dba-ops-mcp-server",
        "version": "2.0.0",
        "registered_databases": list(settings.databases.keys()),
        "tool_count": len(MCP_TOOLS),
    }


# =========================================================
# ENTRYPOINT
# =========================================================

if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "agentic_v2_platform.mcp_server.server:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        reload=False,
    )
