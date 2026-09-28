"""Administrator-owned MCP stdio catalog and agent allowlist validation."""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent_runtime_platform.providers._manifest import supports_tool_ids

_ID = re.compile(r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+$")


class MCPConfigurationError(ValueError):
    pass


def load_mcp_servers() -> dict[str, dict[str, Any]]:
    """Read stdio-only definitions; trust is granted explicitly by server config."""
    raw = os.getenv("AGENT_RUNTIME_MCP_SERVERS", "{}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MCPConfigurationError("AGENT_RUNTIME_MCP_SERVERS must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise MCPConfigurationError("MCP server configuration must be an object.")
    result: dict[str, dict[str, Any]] = {}
    for server, config in value.items():
        if not isinstance(server, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", server):
            raise MCPConfigurationError("MCP server names must use letters, digits, dot, underscore, or dash.")
        if not isinstance(config, dict) or not isinstance(config.get("command"), str) or not config["command"].strip():
            raise MCPConfigurationError(f"MCP server '{server}' requires an administrator configured stdio command.")
        if set(config) - {"command", "args", "cwd", "env_vars", "read_only_tools"}:
            raise MCPConfigurationError(f"MCP server '{server}' contains unsupported configuration fields.")
        if "url" in config or not isinstance(config.get("args", []), list) or not all(isinstance(x, str) for x in config.get("args", [])):
            raise MCPConfigurationError(f"MCP server '{server}' must use stdio command and string arguments.")
        trusted = config.get("read_only_tools", [])
        env_vars = config.get("env_vars", [])
        if not isinstance(trusted, list) or not all(isinstance(x, str) and x for x in trusted):
            raise MCPConfigurationError(f"MCP server '{server}' read_only_tools must be a list of tool names.")
        if not isinstance(env_vars, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) for x in env_vars):
            raise MCPConfigurationError(f"MCP server '{server}' env_vars must list environment variable names.")
        cwd = config.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise MCPConfigurationError(f"MCP server '{server}' cwd must be a string.")
        result[server] = {**config, "read_only_tools": sorted(set(trusted)), "env_vars": sorted(set(env_vars))}
    return result


async def _discover_server(server: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    env = {"PATH": os.getenv("PATH", "")}
    env.update({name: os.environ[name] for name in config["env_vars"] if name in os.environ})
    parameters = StdioServerParameters(
        command=config["command"],
        args=config.get("args", []),
        env={name: env[name] for name in config["env_vars"] if name in env},
        cwd=config.get("cwd"),
    )
    with open(os.devnull, "w") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.list_tools()
    trusted = set(config["read_only_tools"])
    return [
        {
            "id": f"{server}/{tool.name}",
            "server": server,
            "name": tool.name,
            "description": tool.description or "",
            "read_only_hint": bool(getattr(tool, "annotations", None) and getattr(tool.annotations, "readOnlyHint", False)),
            "trusted_read_only": tool.name in trusted,
        }
        for tool in response.tools
    ]


def discover_tools() -> list[dict[str, Any]]:
    """Connect only to administrator-defined stdio servers and list their tools."""
    async def discover() -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for server, config in load_mcp_servers().items():
            entries.extend(await _discover_server(server, config))
        return entries

    try:
        return asyncio.run(discover())
    except MCPConfigurationError:
        raise
    except Exception as exc:
        # Do not echo stdio, server output, or environment values to API clients.
        raise MCPConfigurationError("An administrator-configured MCP server could not be discovered.") from exc


def trusted_tool_ids(servers: dict[str, dict[str, Any]] | None = None) -> set[str]:
    discovered = discover_tools() if servers is None else None
    if discovered is not None:
        return {item["id"] for item in discovered if item["trusted_read_only"]}
    return {
        f"{server}/{name}"
        for server, config in (servers or {}).items()
        for name in config["read_only_tools"]
    }


def validate_tool_ids(tool_ids: list[str], provider: str) -> list[str]:
    if tool_ids and not supports_tool_ids(provider):
        raise ValueError(f"MCP tools are not supported by provider '{provider}'.")
    if len(tool_ids) > 50 or len(set(tool_ids)) != len(tool_ids):
        raise ValueError("tool_ids must contain at most 50 unique MCP tool IDs.")
    for tool_id in tool_ids:
        if not isinstance(tool_id, str) or not _ID.fullmatch(tool_id):
            raise ValueError("Invalid MCP tool ID; use server/tool.")
    catalog = trusted_tool_ids() if tool_ids else set()
    for tool_id in tool_ids:
        if tool_id not in catalog:
            raise ValueError(f"MCP tool '{tool_id}' is unavailable or is not administrator-approved as read-only.")
    return sorted(tool_ids)


def codex_mcp_config(tool_ids: list[str]) -> dict[str, Any]:
    """Build Codex config for only discovered, explicitly trusted tools."""
    servers = load_mcp_servers()
    grouped: dict[str, list[str]] = {}
    for tool_id in tool_ids:
        server, tool = tool_id.split("/", 1)
        definition = servers.get(server)
        if definition is None or tool not in definition["read_only_tools"]:
            raise ValueError(f"MCP tool '{tool_id}' is no longer administrator-approved.")
        grouped.setdefault(server, []).append(tool)
    return {
        server: {
            "command": servers[server]["command"],
            "args": servers[server].get("args", []),
            "env_vars": servers[server]["env_vars"],
            "cwd": servers[server].get("cwd"),
            "enabled_tools": sorted(names),
            "default_tools_approval_mode": "approve",
            "required": True,
        }
        for server, names in grouped.items()
    }


def public_tool_catalog() -> list[dict[str, Any]]:
    return discover_tools()



def codex_mcp_overrides(tool_ids: list[str]) -> tuple[str, ...]:
    """Serialize this run's MCP server catalog as app-server CLI config overrides."""
    import json

    servers = codex_mcp_config(tool_ids)
    overrides: list[str] = []
    for server, config in sorted(servers.items()):
        prefix = f"mcp_servers.{server}."
        for field in ("command", "args", "env_vars", "enabled_tools", "default_tools_approval_mode", "required"):
            overrides.append(f"{prefix}{field}={json.dumps(config[field])}")
        if config.get("cwd") is not None:
            overrides.append(f"{prefix}cwd={json.dumps(config['cwd'])}")
    return tuple(overrides)
