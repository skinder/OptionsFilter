"""OAuth for the Robinhood MCP server.

The server supports OAuth discovery + dynamic client registration, so the first run
opens a browser to log in on robinhood.com; the redirect lands on a one-shot local
HTTP listener. Tokens and the registered client are cached on disk and refreshed
silently by the MCP SDK on later runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

DEFAULT_CONFIG_DIR = Path(os.environ.get("OPTIONSFILTER_HOME", Path.home() / ".config" / "optionsfilter"))
CALLBACK_PORT = int(os.environ.get("OPTIONSFILTER_CALLBACK_PORT", "8765"))
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"


class FileTokenStorage:
    """Persists OAuth tokens and the dynamically registered client in a 0600 JSON file."""

    def __init__(self, path: Path = DEFAULT_CONFIG_DIR / "tokens.json") -> None:
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._load().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(data)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        type(self).result = params
        ok = "code" in params
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Signed in to Robinhood MCP. You can close this tab." if ok else f"Authorization failed: {params}"
        self.wfile.write(f"<html><body><p>{msg}</p></body></html>".encode())

    def log_message(self, *args) -> None:  # silence default stderr logging
        pass


async def _open_browser(auth_url: str) -> None:
    print(f"\nOpening browser to authorize OptionsFilter with Robinhood:\n  {auth_url}\n")
    webbrowser.open(auth_url)


async def _wait_for_callback() -> AuthorizationCodeResult:
    _CallbackHandler.result = {}

    def serve() -> None:
        with HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler) as server:
            server.timeout = 300
            while not _CallbackHandler.result:
                server.handle_request()

    await asyncio.to_thread(serve)
    params = _CallbackHandler.result
    if "code" not in params:
        raise RuntimeError(f"OAuth callback did not include a code: {params}")
    return AuthorizationCodeResult(code=params["code"], state=params.get("state"), iss=params.get("iss"))


def build_oauth(server_url: str, storage: FileTokenStorage | None = None) -> OAuthClientProvider:
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name="OptionsFilter",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage or FileTokenStorage(),
        redirect_handler=_open_browser,
        callback_handler=_wait_for_callback,
    )
