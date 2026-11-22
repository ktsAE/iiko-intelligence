from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from . import config
from .database import write_lock
from .seed import seed_demo

SERVER_NAME = "iiko_intelligence"


def _server_parameters(database: Path) -> dict[str, Any]:
    return {
        "command": str(Path(sys.executable).absolute()),
        "args": ["-m", "iiko_intelligence.mcp_server"],
        "env": {"IIKO_INTELLIGENCE_DB": str(database.absolute())},
    }


async def _verify_connection(database: Path) -> dict[str, Any]:
    params = _server_parameters(database)
    params["args"] = ["-B", *params["args"]]
    params["env"]["IIKO_NO_UPDATE_CHECK"] = "1"
    async with Client(StdioServerParameters(**params), read_timeout_seconds=10) as client:
        tools = await client.list_tools()
        result = await client.call_tool("get_data_status", {})
        if result.is_error or not isinstance(result.structured_content, dict):
            raise ValueError("the MCP server did not return a valid dataset status")
        if (not result.structured_content["reconciliation"]["passed"]
                or not result.structured_content.get("stock_snapshot", {}).get("passed", True)
                or not all(s["passed"] for s in result.structured_content.get("document_sources", {}).values())):
            raise ValueError("the stored dataset failed reconciliation")
        if not all(tool.annotations and tool.annotations.read_only_hint for tool in tools.tools):
            raise ValueError("the MCP server exposed an unexpected writable tool")
        return {"protocol": client.protocol_version, "tools": sorted(tool.name for tool in tools.tools),
                "dataset_mode": result.structured_content["dataset_mode"]}


def verify_connection(database: Path) -> dict[str, Any]:
    try:
        return asyncio.run(_verify_connection(database))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("could not launch the local MCP server or read its dataset") from exc


def _config_block(parameters: dict[str, Any]) -> str:
    command = json.dumps(parameters["command"], ensure_ascii=False)
    database = json.dumps(parameters["env"]["IIKO_INTELLIGENCE_DB"], ensure_ascii=False)
    return (f"[mcp_servers.{SERVER_NAME}]\ncommand = {command}\n"
            'args = ["-m", "iiko_intelligence.mcp_server"]\n'
            f"[mcp_servers.{SERVER_NAME}.env]\nIIKO_INTELLIGENCE_DB = {database}\n")


def _updated_settings(current: str, parameters: dict[str, Any]) -> str:
    entry = tomllib.loads(current).get("mcp_servers", {}).get(SERVER_NAME)
    if entry is None:
        return current + ("\n\n" if current else "") + _config_block(parameters)
    if all(entry.get(k) == v for k, v in parameters.items()):
        return current
    # Repair paths only when they match the exact block emitted by this or the old installer.
    if isinstance(entry.get("env"), dict) and isinstance(entry["env"].get("IIKO_INTELLIGENCE_DB"), str):
        old_db = Path(entry["env"]["IIKO_INTELLIGENCE_DB"])
        old_root = old_db.parent.parent
        interpreters = [old_root / p for p in ("runtime/python.exe", ".venv/Scripts/python.exe",
                                                ".venv/bin/python", ".venv/bin/python3",
                                                f".venv/bin/python{sys.version_info.major}.{sys.version_info.minor}")]
        if (entry.get("args") == parameters["args"] and old_db == old_root / "data/manager.sqlite"
                and isinstance(entry.get("command"), str) and Path(entry["command"]) in interpreters
                and current.count(_config_block(entry)) == 1):
            return current.replace(_config_block(entry), _config_block(parameters), 1)
    raise ValueError("custom iiko_intelligence paths in .codex/config.toml need manual review")


def setup_local() -> dict[str, Any]:
    database = config.MANAGER_DATABASE
    if not database.exists():
        seed_demo(database, preserve_existing=True)
    result = verify_connection(database)
    settings = config.PROJECT_ROOT / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    with write_lock(settings):
        current = settings.read_text(encoding="utf-8") if settings.exists() else ""
        parameters = _server_parameters(database)
        if config.CODE_ROOT != config.PROJECT_ROOT:
            parameters["command"] = str(config.PROJECT_ROOT / "runtime/python.exe")
        updated = _updated_settings(current, parameters)
        if updated != current:
            with NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=settings.parent) as file:
                file.write(updated)
            try:
                os.replace(file.name, settings)
            finally:
                Path(file.name).unlink(missing_ok=True)
    return {**result, "project_folder": str(config.PROJECT_ROOT), "codex_config": str(settings),
            "next": "Open this folder in local Codex in the ChatGPT desktop app, trust it, and type /mcp."}
