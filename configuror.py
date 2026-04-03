#!/usr/bin/env -S uv run python
"""configuror — generic MCP proxy that adds persistent configure_<tool> companions.

For each upstream tool T, exposes:
  T              — merges stored defaults with call-time args, forwards to upstream
  configure_T    — same schema (all optional) + _mode, updates stored defaults

Hooks (--hook <import>) can intercept tool schemas, arguments, and results.

Usage:
    configuror.py [--hook <import>]... -- <upstream-mcp-command ...>
    configuror.py -- npx firecrawl-mcp
    configuror.py --hook firecrawl_hooks -- npx firecrawl-mcp
"""

from __future__ import annotations

import atexit
import copy
import importlib
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.tools.function_tool import FunctionTool


MergeMode = Literal["set", "merge", "deep_merge", "extend", "deep_extend"]

STATE_FILE = Path(__file__).parent / ".configuror-state.json"
_state: dict[str, dict[str, object]] = {}


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state() -> None:
    global _state
    if STATE_FILE.exists():
        _state = json.loads(STATE_FILE.read_text())


def save_state() -> None:
    STATE_FILE.write_text(json.dumps(_state, indent=2))


def get_defaults(tool_name: str) -> dict[str, object]:
    return dict(_state.get(tool_name, {}))


def update_defaults(
    tool_name: str,
    params: dict[str, object],
    mode: str,
) -> dict[str, object]:
    current = _state.get(tool_name, {})
    _state[tool_name] = apply_mode(current, params, mode)
    return dict(_state[tool_name])


# ---------------------------------------------------------------------------
# Merge engine
# ---------------------------------------------------------------------------


def apply_mode(
    current: dict[str, object],
    patch: dict[str, object],
    mode: str,
) -> dict[str, object]:
    match mode:
        case "set":
            return dict(patch)
        case "merge":
            return {**current, **patch}
        case "deep_merge":
            return _deep_merge(current, patch)
        case "extend":
            result = dict(current)
            for k, v in patch.items():
                existing = result.get(k)
                if isinstance(v, list) and isinstance(existing, list):
                    result[k] = existing + v
                else:
                    result[k] = v
            return result
        case "deep_extend":
            return _deep_extend(current, patch)
        case _:
            raise ValueError(f"Unknown merge mode: {mode}")


def _deep_merge(
    base: dict[str, object],
    override: dict[str, object],
) -> dict[str, object]:
    result = dict(base)
    for k, v in override.items():
        existing = result.get(k)
        if isinstance(v, dict) and isinstance(existing, dict):
            result[k] = _deep_merge(existing, v)
        else:
            result[k] = v
    return result


def _deep_extend(
    base: dict[str, object],
    patch: dict[str, object],
) -> dict[str, object]:
    result = dict(base)
    for k, v in patch.items():
        existing = result.get(k)
        if isinstance(v, dict) and isinstance(existing, dict):
            result[k] = _deep_extend(existing, v)
        elif isinstance(v, list) and isinstance(existing, list):
            result[k] = existing + v
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Schema derivation
# ---------------------------------------------------------------------------


def make_configure_schema(upstream_schema: dict[str, object]) -> dict[str, object]:
    """Derive configure tool schema: all upstream fields optional + _mode."""
    schema = copy.deepcopy(upstream_schema)
    schema.pop("required", None)
    props = schema.setdefault("properties", {})
    props["_mode"] = {
        "type": "string",
        "enum": ["set", "merge", "deep_merge", "extend", "deep_extend"],
        "default": "merge",
        "description": (
            "How to combine with existing defaults: "
            "set (replace all), merge (shallow override), "
            "deep_merge (recursive dict merge), "
            "extend (append to lists), "
            "deep_extend (recursive list append + dict merge)"
        ),
    }
    return schema


# ---------------------------------------------------------------------------
# Dynamic tool registration
# ---------------------------------------------------------------------------


def register_proxy_pair(
    server: FastMCP,
    upstream_tool: object,
    transport: StdioTransport,
    hooks: list[object],
) -> None:
    name: str = upstream_tool.name
    desc: str = upstream_tool.description or ""
    schema: dict[str, object] = copy.deepcopy(upstream_tool.inputSchema)

    # --- Hook: transform_tool (pipeline) ---
    for mod in hooks:
        fn = getattr(mod, "transform_tool", None)
        if fn is None:
            continue
        result = fn(name, desc, schema)
        if result is None:
            return
        name, desc, schema = result

    # --- Proxy tool: same schema as upstream (post-hook) ---

    async def proxy_handler(*, _name: str = name, **kwargs: object) -> str:
        defaults = get_defaults(_name)
        merged = {**defaults, **kwargs}

        # Hook: before_call — strip custom params, transform args
        forwarded = dict(merged)
        for mod in hooks:
            fn = getattr(mod, "before_call", None)
            if fn:
                forwarded = fn(_name, forwarded)

        async with Client(transport) as client:
            result = await client.call_tool(_name, forwarded)
        output = "\n".join(block.text for block in result.content if hasattr(block, "text"))

        # Hook: after_call — post-process results
        for mod in hooks:
            fn = getattr(mod, "after_call", None)
            if fn:
                output = fn(_name, merged, output)

        return output

    server.add_tool(
        FunctionTool(
            fn=proxy_handler,
            name=name,
            description=desc,
            parameters=schema,
        )
    )

    # --- Configure tool: same schema, all optional, + _mode ---

    configure_schema = make_configure_schema(schema)

    async def configure_handler(*, _name: str = name, **kwargs: object) -> str:
        merge_mode = str(kwargs.pop("_mode", "merge"))
        updated = update_defaults(_name, kwargs, merge_mode)
        return json.dumps(updated, indent=2)

    server.add_tool(
        FunctionTool(
            fn=configure_handler,
            name=f"configure_{name}",
            description=(
                f"Set persistent defaults for '{name}'. "
                f"Same parameters as '{name}' (all optional). "
                f"Use _mode to control how values combine with existing defaults."
            ),
            parameters=configure_schema,
        )
    )


# ---------------------------------------------------------------------------
# CLI parsing + hook loading
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Parse --hook flags and upstream command from argv.

    Returns (hook_imports, upstream_cmd).
    """
    if "--" not in argv:
        print(
            "Usage: configuror.py [--hook <import>]... -- <upstream-mcp-command>",
            file=sys.stderr,
        )
        sys.exit(1)
    idx = argv.index("--")
    our_args = argv[1:idx]
    upstream_cmd = argv[idx + 1 :]
    if not upstream_cmd:
        print("Error: no upstream command after '--'", file=sys.stderr)
        sys.exit(1)

    hooks: list[str] = []
    i = 0
    while i < len(our_args):
        if our_args[i] == "--hook" and i + 1 < len(our_args):
            hooks.append(our_args[i + 1])
            i += 2
        else:
            i += 1

    return hooks, upstream_cmd


def load_hooks(import_paths: list[str]) -> list[object]:
    """Import hook modules by dotted path."""
    return [importlib.import_module(path) for path in import_paths]


def build_transport(upstream_cmd: list[str]) -> StdioTransport:
    return StdioTransport(command=upstream_cmd[0], args=upstream_cmd[1:], env=dict(os.environ))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP):
    load_state()
    atexit.register(save_state)

    hook_imports, upstream_cmd = parse_args(sys.argv)
    hooks = load_hooks(hook_imports)
    transport = build_transport(upstream_cmd)

    async with Client(transport) as client:
        upstream_tools = await client.list_tools()

    for tool in upstream_tools:
        register_proxy_pair(server, tool, transport, hooks)

    yield

    save_state()


proxy = FastMCP("configuror", lifespan=lifespan)


if __name__ == "__main__":
    proxy.run()
