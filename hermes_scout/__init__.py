"""
Scout plugin for Hermes — integrates Scout messaging via `scout mcp` stdio MCP server.

Architecture:
  Hermes plugin → ScoutBridge (subprocess bridge to `scout mcp`) → Scout MCP server → Scout broker

The ScoutBridge speaks JSON-RPC 2.0 over stdio using the Model Context Protocol.
MCP requires a handshake (initialize → notifications/initialized) before any tool calls.
Tool invocations use the `tools/call` method, not direct tool-name method calls.
"""

import json
import subprocess
import threading
import uuid
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Subprocess bridge to `scout mcp`
# ---------------------------------------------------------------------------

class ScoutBridge:
    """Speaks JSON-RPC 2.0 over stdio to the Scout MCP server.

    MCP enforces sequential request/response — each call waits for its response
    before sending the next request on this connection.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._responses: dict[str, dict] = {}
        self._reader_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the subprocess, perform MCP handshake, then start the reader."""
        self._proc = subprocess.Popen(
            ["scout", "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,  # unbuffered for stdio
        )

        # MCP requires initialize before any tool calls
        init_resp = self._call_raw("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "hermes-scout-plugin", "version": "1.0"},
        })
        if init_resp.get("error"):
            raise RuntimeError(f"Scout MCP initialize failed: {init_resp['error']}")

        # Tell server we're done with handshake (required by MCP spec)
        self._proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
        self._proc.stdin.flush()

        # NOW start the reader thread — handshake is done, no more races
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        """Call a Scout MCP method and return the parsed result."""
        if not self._proc:
            raise RuntimeError("ScoutBridge not started")

        id_ = str(uuid.uuid4())
        request = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}

        event = threading.Event()
        with self._lock:
            self._pending[id_] = event

        try:
            raw = json.dumps(request) + "\n"
            self._proc.stdin.write(raw.encode("utf-8"))
            self._proc.stdin.flush()
            event.wait(timeout=60)
            with self._lock:
                resp = self._responses.pop(id_, None)
            if resp is None:
                raise TimeoutError(f"Scout MCP call '{method}' timed out")
            return resp
        finally:
            with self._lock:
                self._pending.pop(id_, None)

    def _call_raw(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a JSON-RPC request and read the matching response by ID.

        Used for the initial handshake (initialize, initialized) where the
        normal pending-map flow is not yet set up.
        """
        if not self._proc:
            raise RuntimeError("ScoutBridge not started")
        id_ = str(uuid.uuid4())
        request = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
        raw = json.dumps(request) + "\n"
        self._proc.stdin.write(raw.encode("utf-8"))
        self._proc.stdin.flush()
        # Read until we get the response with matching id (ignore server notifications)
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("Scout MCP closed during handshake")
            msg = json.loads(line.decode("utf-8"))
            if msg.get("id") == id_:
                return msg

    def _read_loop(self) -> None:
        """Read JSON-RPC responses from stdout, unblock waiters by ID."""
        for line in self._proc.stdout:  # type: ignore
            if not line.strip():
                continue
            msg = json.loads(line.decode("utf-8"))
            # Server notifications have no `id` — skip them
            id_ = msg.get("id")
            if id_:
                with self._lock:
                    if id_ in self._pending:
                        self._responses[id_] = msg.get("result", msg.get("error"))
                        self._pending[id_].set()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_bridge: Optional[ScoutBridge] = None


def _get_bridge() -> ScoutBridge:
    global _bridge
    if _bridge is None:
        _bridge = ScoutBridge()
        _bridge.start()
    return _bridge


def _close_bridge() -> None:
    global _bridge
    if _bridge is not None:
        _bridge.close()
        _bridge = None


# ---------------------------------------------------------------------------
# Tool schemas — derived from `scout mcp --json` / tools/list
# ---------------------------------------------------------------------------

SCHEMAS = {
    "scout_whoami": {
        "name": "scout_whoami",
        "description": "Inspect the default Scout sender identity and broker URL for a "
        "working directory. Use when host or workspace context is unclear.",
        "parameters": {
            "type": "object",
            "properties": {
                "currentDirectory": {"type": "string"},
            },
        },
    },
    "scout_agents_search": {
        "name": "scout_agents_search",
        "description": "Search the live Scout broker and discovered agent inventory "
        "for routing candidates.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "currentDirectory": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    "scout_agents_resolve": {
        "name": "scout_agents_resolve",
        "description": "Resolve one exact Scout agent handle or return ambiguity details.",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "minLength": 1},
                "currentDirectory": {"type": "string"},
            },
            "required": ["label"],
        },
    },
    "scout_messages_send": {
        "name": "scout_messages_send",
        "description": "Post a broker-backed Scout tell/update.",
        "parameters": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "minLength": 1},
                "targetLabel": {"type": "string"},
                "currentDirectory": {"type": "string"},
                "channel": {"type": "string"},
                "shouldSpeak": {"type": "boolean"},
            },
            "required": ["body"],
        },
    },
    "scout_invocations_ask": {
        "name": "scout_invocations_ask",
        "description": "Create a broker-backed Scout ask/work handoff. "
        "replyMode='inline' blocks until flight completes. "
        "replyMode='none' returns IDs immediately. "
        "replyMode='notify' returns immediately and notifies on completion.",
        "parameters": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "minLength": 1},
                "targetLabel": {"type": "string"},
                "targetAgentId": {"type": "string"},
                "currentDirectory": {"type": "string"},
                "replyMode": {"type": "string", "enum": ["none", "inline", "notify"]},
                "timeoutSeconds": {"type": "integer", "minimum": 1},
            },
            "required": ["body", "targetLabel"],
        },
    },
    "scout_work_update": {
        "name": "scout_work_update",
        "description": "Update a durable Scout work item state "
        "(open → working → waiting → review → done → cancelled).",
        "parameters": {
            "type": "object",
            "properties": {
                "workId": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "working", "waiting", "review", "done", "cancelled"]},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "currentDirectory": {"type": "string"},
            },
            "required": ["workId"],
        },
    },
    "scout_card_create": {
        "name": "scout_card_create",
        "description": "Create a dedicated Scout agent card with a reply-ready return address.",
        "parameters": {
            "type": "object",
            "properties": {
                "currentDirectory": {"type": "string"},
                "agentName": {"type": "string"},
                "displayName": {"type": "string"},
                "harness": {"type": "string", "enum": ["claude", "codex"]},
                "model": {"type": "string"},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Tool handlers — all use tools/call
# ---------------------------------------------------------------------------

def handle_scout_whoami(params: dict, **kwargs) -> dict:
    cwd = params.get("currentDirectory")
    result = _get_bridge().call("tools/call", {
        "name": "whoami",
        "arguments": {"currentDirectory": cwd} if cwd else {},
    })
    return {"success": True, "result": result}


def handle_scout_agents_search(params: dict, **kwargs) -> dict:
    query = params.get("query", "")
    cwd = params.get("currentDirectory")
    limit = params.get("limit", 20)
    args = {"query": query, "currentDirectory": cwd, "limit": limit}
    result = _get_bridge().call("tools/call", {
        "name": "agents_search",
        "arguments": args,
    })
    return {"success": True, "result": result}


def handle_scout_agents_resolve(params: dict, **kwargs) -> dict:
    label = params.get("label", "")
    cwd = params.get("currentDirectory")
    args = {"label": label}
    if cwd:
        args["currentDirectory"] = cwd
    result = _get_bridge().call("tools/call", {
        "name": "agents_resolve",
        "arguments": args,
    })
    return {"success": True, "result": result}


def handle_scout_messages_send(params: dict, **kwargs) -> dict:
    body = params.get("body", "")
    targetLabel = params.get("targetLabel", "")
    cwd = params.get("currentDirectory")
    channel = params.get("channel")
    shouldSpeak = params.get("shouldSpeak", False)

    args: dict[str, Any] = {"body": body, "targetLabel": targetLabel}
    if cwd:
        args["currentDirectory"] = cwd
    if channel:
        args["channel"] = channel
    if shouldSpeak:
        args["shouldSpeak"] = shouldSpeak

    result = _get_bridge().call("tools/call", {
        "name": "messages_send",
        "arguments": args,
    })
    return {"success": True, "result": result}


def handle_scout_invocations_ask(params: dict, **kwargs) -> dict:
    body = params.get("body", "")
    targetLabel = params.get("targetLabel", "")
    targetAgentId = params.get("targetAgentId")
    cwd = params.get("currentDirectory")
    replyMode = params.get("replyMode", "inline")
    timeoutSeconds = params.get("timeoutSeconds", 120)

    args: dict[str, Any] = {
        "body": body,
        "targetLabel": targetLabel,
        "replyMode": replyMode,
    }
    if cwd:
        args["currentDirectory"] = cwd
    if targetAgentId:
        args["targetAgentId"] = targetAgentId
    if replyMode == "inline":
        args["timeoutSeconds"] = timeoutSeconds

    result = _get_bridge().call("tools/call", {
        "name": "invocations_ask",
        "arguments": args,
    })
    return {"success": True, "result": result}


def handle_scout_work_update(params: dict, **kwargs) -> dict:
    workId = params.get("workId", "")
    state = params.get("state")
    title = params.get("title")
    summary = params.get("summary")
    cwd = params.get("currentDirectory")

    work: dict[str, Any] = {"workId": workId}
    if state:
        work["state"] = state
    if title:
        work["title"] = title
    if summary:
        work["summary"] = summary

    args: dict[str, Any] = {"work": work}
    if cwd:
        args["currentDirectory"] = cwd

    result = _get_bridge().call("tools/call", {
        "name": "work_update",
        "arguments": args,
    })
    return {"success": True, "result": result}


def handle_scout_card_create(params: dict, **kwargs) -> dict:
    cwd = params.get("currentDirectory")
    agentName = params.get("agentName")
    displayName = params.get("displayName")
    harness = params.get("harness")
    model = params.get("model")

    args: dict[str, Any] = {}
    if cwd:
        args["currentDirectory"] = cwd
    if agentName:
        args["agentName"] = agentName
    if displayName:
        args["displayName"] = displayName
    if harness:
        args["harness"] = harness
    if model:
        args["model"] = model

    result = _get_bridge().call("tools/call", {
        "name": "card_create",
        "arguments": args,
    })
    return {"success": True, "result": result}


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def on_session_start(session_id: str, **kwargs) -> None:
    try:
        identity = _get_bridge().call("tools/call", {
            "name": "whoami",
            "arguments": {},
        })
        print(f"[scout] session started — identity: {identity}")
    except Exception as e:
        print(f"[scout] whoami failed: {e}")


def on_session_end(session_id: str, **kwargs) -> None:
    print("[scout] session ended")


def post_tool_call(tool_name: str, params: dict, result: Any, **kwargs) -> None:
    if tool_name.startswith("scout_"):
        print(f"[scout] tool: {tool_name}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(ctx):
    """Register all Scout tools, hooks, and slash commands with Hermes."""

    tools = [
        ("scout_whoami", SCHEMAS["scout_whoami"], handle_scout_whoami),
        ("scout_agents_search", SCHEMAS["scout_agents_search"], handle_scout_agents_search),
        ("scout_agents_resolve", SCHEMAS["scout_agents_resolve"], handle_scout_agents_resolve),
        ("scout_messages_send", SCHEMAS["scout_messages_send"], handle_scout_messages_send),
        ("scout_invocations_ask", SCHEMAS["scout_invocations_ask"], handle_scout_invocations_ask),
        ("scout_work_update", SCHEMAS["scout_work_update"], handle_scout_work_update),
        ("scout_card_create", SCHEMAS["scout_card_create"], handle_scout_card_create),
    ]

    for name, schema, handler in tools:
        ctx.register_tool(
            name=name,
            toolset="scout",
            schema=schema,
            handler=handler,
            description=schema["description"],
        )

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("post_tool_call", post_tool_call)
