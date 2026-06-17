# configuror

MCP proxy that adds persistent, configurable defaults to any MCP server's tools.

## What it does

configuror sits between an MCP client (like Claude Code) and any upstream MCP server. It discovers the upstream's tools at startup and re-exposes each one with two additions:

- **Stored defaults** — parameters you set via `configure_<tool>` persist across calls and merge with call-time arguments (call-time wins).
- **A paired `configure_<tool>`** — mirrors the original tool's schema with all fields made optional, plus a `_mode` parameter that controls how new values combine with existing defaults.

The upstream server is specified after `--`. configuror connects via stdio (for a command) or streamable HTTP / SSE (for a URL), discovers tools, and proxies everything through.

## Quick start

```sh
git clone https://github.com/starbaser/configuror
cd configuror
uv sync
```

Run configuror as an MCP server in your client config (`.mcp.json`):

```json
{
  "mcpServers": {
    "configuror": {
      "command": "uv",
      "args": ["run", "python", "configuror.py", "--", "npx", "firecrawl-mcp"]
    }
  }
}
```

This wraps the Firecrawl MCP server. Every Firecrawl tool appears as-is, plus a `configure_<tool>` companion for each one.

## Usage

```
configuror.py [--hook <import>]... -- <upstream-command-or-url>
```

The spec after `--` is either a subprocess command (stdio upstream) or a single URL (streamable HTTP upstream, or SSE if the URL ends in `/sse`):

```
configuror.py -- npx firecrawl-mcp                  # stdio upstream
configuror.py -- https://mcp.example.com/mcp        # streamable HTTP upstream
configuror.py -- https://mcp.example.com/events/sse # SSE upstream
```

### Setting defaults

```
configure_firecrawl_search(limit=20, tbs="qdr:w")
```

Now every `firecrawl_search` call uses `limit=20` and `tbs="qdr:w"` unless overridden at call time. Call-time arguments take precedence over stored defaults via a shallow merge at the proxy handler level.

### Resetting

```
configure_firecrawl_search(_mode="set")
```

With no other parameters, `set` mode replaces the defaults dict with an empty dict.

## Merge modes

The `_mode` parameter on every `configure_<tool>` controls how values combine with existing defaults. The default mode is `merge`.

| Mode | Behavior |
|------|----------|
| `set` | Replace the entire defaults dict with the provided values |
| `merge` | Shallow update — top-level keys from the new values override old ones |
| `deep_merge` | Recursive dict merge — nested dicts are merged rather than replaced |
| `extend` | Append to existing lists; set non-list values directly |
| `deep_extend` | Recursive — lists are appended and dicts are merged at any nesting depth |

### Examples

```python
# Build up a list over multiple calls
configure_search(sources=["web"], _mode="extend")
configure_search(sources=["news"], _mode="extend")
# defaults: {"sources": ["web", "news"]}

# Update a nested object without clobbering siblings
configure_search(scrapeOptions={"formats": ["html"]}, _mode="deep_merge")
# If existing: {"scrapeOptions": {"onlyMainContent": true}}
# Result:      {"scrapeOptions": {"onlyMainContent": true, "formats": ["html"]}}
```

## State persistence

Defaults live in memory during runtime and auto-serialize to `$XDG_STATE_HOME/configuror/state.json` (defaulting to `~/.local/state/configuror/state.json`) on exit. Override the path with the `CONFIGUROR_STATE_FILE` environment variable. Persistence is triggered by both an `atexit` handler registered at startup and the lifespan teardown, ensuring state is saved whether the process exits normally or is terminated. On next startup, the file is loaded and all previously configured defaults are restored.

## Hooks

Hooks let you intercept tool schemas, arguments, and results. Pass one or more Python import paths before `--`:

```
configuror.py --hook myproject.search_hooks -- npx firecrawl-mcp
```

Multiple hooks are applied in pipeline order (left to right).

### Protocol

A hook module can export any subset of three functions. Missing functions are skipped silently.

```python
def transform_tool(
    name: str,
    description: str,
    schema: dict[str, object],
) -> tuple[str, str, dict[str, object]] | None:
    """Modify or filter a tool's schema before registration.
    Return None to exclude the tool entirely."""
    ...

def before_call(
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    """Transform arguments after default-merge, before forwarding to upstream."""
    ...

def after_call(
    name: str,
    arguments: dict[str, object],
    result: str,
) -> str:
    """Post-process the result string.
    'arguments' includes the full merged dict (defaults + call-time)."""
    ...
```

- **`transform_tool`** runs once per tool at startup. Returning `None` excludes that tool entirely — neither the proxy nor its `configure_` companion will be registered.
- **`before_call`** runs on each invocation of a proxy tool, after defaults are merged but before the call is forwarded to upstream.
- **`after_call`** runs on each invocation after the upstream result is received, before it is returned to the client.

### Example: domain filtering for search

```python
# firecrawl_hooks.py

def transform_tool(name, description, schema):
    if name == "firecrawl_search":
        schema["properties"]["exclude_domains"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Domains to filter from results",
        }
    return name, description, schema

def before_call(name, arguments):
    arguments.pop("exclude_domains", None)  # don't forward to upstream
    return arguments

def after_call(name, arguments, result):
    import json, urllib.parse
    exclude = arguments.get("exclude_domains", [])
    if not exclude or name != "firecrawl_search":
        return result
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        return result
    exclude_set = {d.lower().removeprefix("www.") for d in exclude}
    for key in ("web", "news", "images"):
        items = data.get(key)
        if isinstance(items, list):
            data[key] = [
                item for item in items
                if urllib.parse.urlparse(item.get("url", "")).netloc
                    .lower().removeprefix("www.") not in exclude_set
            ]
    return json.dumps(data)
```

Agents can then build up a domain blacklist incrementally:

```python
configure_firecrawl_search(exclude_domains=["reddit.com"], _mode="extend")
```

## How it works

configuror uses FastMCP 3.x. At startup, a lifespan context manager runs before the server accepts connections:

1. Loads persisted state from `.configuror-state.json` if it exists.
2. Parses CLI arguments to extract hook imports and the upstream command.
3. Connects to the upstream MCP server via the inferred transport — `StdioTransport` for a command (passing through the parent process environment), or `StreamableHttpTransport` / `SSETransport` for a URL — selected automatically from whether the spec after `--` starts with `http://`/`https://`.
4. Calls `list_tools()` on the upstream client to discover all available tools.
5. For each upstream tool, constructs and registers two `FunctionTool` instances with hand-crafted JSON schemas (bypassing FastMCP's annotation-based schema generation).

Each proxy tool's handler merges stored defaults with call-time arguments, runs `before_call` hooks, opens a fresh `Client` session against the upstream transport, forwards the call, collects text content blocks from the result, runs `after_call` hooks, and returns the final output. The configure tool's handler extracts `_mode`, applies the merge operation via the `apply_mode` engine, and returns the updated defaults as JSON.

## Requirements

- Python >= 3.13
- fastmcp >= 3.2.0
