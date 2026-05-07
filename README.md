# hermes-scout

Scout messaging integration for [Hermes Agent](https://github.com/itsdanielg/hermes-agent) — send, ask, and broadcast to Scout agents directly from any Hermes chat session.

## What it does

Provides 7 Scout tools in Hermes:

| Tool | Description |
|------|-------------|
| `scout_whoami` | Inspect your default Scout identity and broker URL |
| `scout_agents_search` | Search the live Scout broker for agents by query |
| `scout_agents_resolve` | Resolve an exact agent handle |
| `scout_messages_send` | Post a Scout tell/update to an agent |
| `scout_invocations_ask` | Create an ask/work handoff (inline or async) |
| `scout_work_update` | Update a durable work item state |
| `scout_card_create` | Create a dedicated agent card with a reply address |

Also registers `on_session_start`, `on_session_end`, and `post_tool_call` hooks.

## How it works

Talks to `scout mcp` via JSON-RPC 2.0 over stdio using the Model Context Protocol. The MCP handshake (`initialize` → `notifications/initialized`) is performed at startup before any tool calls are sent. Tool invocations go through `tools/call` — the universal MCP invocation method.

## Requirements

- [Hermes Agent](https://github.com/itsdanielg/hermes-agent) v0.6.0+
- [Scout](https://scout.dev) installed and `scout mcp` available on PATH
- Python 3.9+

## Installation

### As a directory plugin (recommended for development)

```bash
ln -s ~/dev/hermes-scout ~/.hermes/plugins/scout
hermes tools list | grep scout
```

### As a pip package

```bash
pip install hermes-scout
hermes plugins install hermes-scout
```

## Configuration

No configuration required. The plugin starts a `scout mcp` subprocess per session and communicates over stdio.

## Architecture

```
Hermes plugin → ScoutBridge (stdio JSON-RPC bridge) → scout mcp → Scout broker
```

The `ScoutBridge` class:
1. Spawns `scout mcp` as a subprocess with unbuffered stdio
2. Sends `initialize` + `notifications/initialized` (MCP handshake)
3. Starts a reader thread that maps response IDs to waiting threads
4. Exposes `call(method, params)` which serializes requests, waits for responses

Tool handlers (`handle_scout_*`) wrap the bridge's `call()` method, forwarding parameters as `tools/call` arguments per the MCP spec.

## License

MIT