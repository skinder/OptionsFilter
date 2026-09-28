"""Minimal read-only async client for the Robinhood MCP server."""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from optionsfilter.auth import FileTokenStorage, build_oauth

SERVER_URL = os.environ.get("ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading")

# Anything that isn't a read is refused before it reaches Robinhood.
READ_ONLY_EXTRA = {"search", "preview_scan", "run_scan"}


def is_read_only(tool: str) -> bool:
    return tool.startswith("get_") or tool in READ_ONLY_EXTRA


class RobinhoodMCP:
    def __init__(self, server_url: str = SERVER_URL) -> None:
        self.server_url = server_url
        self.storage = FileTokenStorage()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> RobinhoodMCP:
        self._stack = AsyncExitStack()
        http = await self._stack.enter_async_context(create_mcp_http_client(auth=build_oauth(self.server_url, self.storage)))
        read, write = await self._stack.enter_async_context(streamable_http_client(self.server_url, http_client=http, terminate_on_close=False))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack:
            await self._stack.aclose()

    async def call(self, tool: str, **args: Any) -> dict:
        if not is_read_only(tool):
            raise PermissionError(f"read-only client: refusing '{tool}'")
        result = await self._session.call_tool(tool, {k: v for k, v in args.items() if v is not None})
        payload = result.structured_content or _json_text(result)
        if result.is_error:
            raise RuntimeError(f"{tool} failed: {payload}")
        return payload.get("data", payload)

    # --- the handful of calls the screener needs -----------------------------------

    async def scan(self, filters: list[dict], columns: list[dict]) -> list[dict]:
        data = await self.call("preview_scan", filters=filters, columns=columns)
        return data["result"]["results"]

    async def option_chains(self, symbol: str) -> list[dict]:
        return (await self.call("get_option_chains", underlying_symbol=symbol))["chains"]

    async def option_instruments(self, chain_id: str, expiration: str, option_type: str) -> list[dict]:
        out, cursor = [], None
        while True:
            data = await self.call(
                "get_option_instruments", chain_id=chain_id, expiration_dates=expiration,
                type=option_type, tradability="tradable", cursor=cursor,
            )
            out += data.get("instruments", [])
            cursor = data.get("next")
            if not cursor:
                return out

    async def option_quotes(self, instrument_ids: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(instrument_ids), 20):
            data = await self.call("get_option_quotes", instrument_ids=instrument_ids[i : i + 20])
            for r in data["results"]:
                out[r["quote"]["instrument_id"]] = r["quote"]
        return out


def _json_text(result) -> dict:
    text = "".join(c.text for c in result.content if c.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
