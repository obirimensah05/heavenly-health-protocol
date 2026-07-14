"""One-shot loopback OAuth callback receiver with state and path validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import time
from typing import Callable
from urllib.parse import parse_qs, urlparse
import webbrowser


class OAuthCallbackError(RuntimeError):
    """The local OAuth callback was missing, denied, expired, or invalid."""


@dataclass(frozen=True)
class OAuthCallbackResult:
    code: str = field(repr=False)


def receive_oauth_callback(
    *,
    authorization_url: str,
    callback_url: str,
    expected_state: str,
    open_browser: Callable[[str], bool] = webbrowser.open,
    timeout_seconds: int = 180,
) -> OAuthCallbackResult:
    """Open authorization and receive exactly one validated loopback callback."""
    callback = urlparse(callback_url)
    if (
        callback.scheme != "http"
        or callback.hostname != "127.0.0.1"
        or callback.port is None
        or not callback.path.startswith("/")
        or callback.query
        or callback.fragment
    ):
        raise OAuthCallbackError("OAuth callback must be an exact 127.0.0.1 HTTP URL")
    authorization = urlparse(authorization_url)
    if authorization.scheme != "https" or not authorization.hostname:
        raise OAuthCallbackError("OAuth authorization URL must use HTTPS")
    if not expected_state or len(expected_state) < 32:
        raise OAuthCallbackError("OAuth state is invalid")
    bounded_timeout = max(5, min(int(timeout_seconds), 600))
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - standard library callback
            requested = urlparse(self.path)
            if requested.path != callback.path:
                self._reply(404, "Not found")
                return
            query = parse_qs(requested.query, keep_blank_values=True)
            state = query.get("state", [""])
            code = query.get("code", [""])
            error = query.get("error", [""])
            if len(state) != 1 or not secrets.compare_digest(state[0], expected_state):
                result["error"] = "OAuth state validation failed"
                self._reply(400, "Authorization failed. Return to the terminal.")
                return
            if error[0]:
                result["error"] = "Provider authorization was denied"
                self._reply(400, "Authorization was denied. Return to the terminal.")
                return
            if len(code) != 1 or not code[0]:
                result["error"] = "OAuth callback omitted the authorization code"
                self._reply(400, "Authorization failed. Return to the terminal.")
                return
            result["code"] = code[0]
            self._reply(200, "Authorization complete. You may close this tab.")

        def _reply(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        server = HTTPServer(("127.0.0.1", callback.port), Handler)
    except OSError as error:
        raise OAuthCallbackError("OAuth loopback port is unavailable; stop the local MCP first") from error
    try:
        server.timeout = 0.25
        if open_browser(authorization_url) is False:
            raise OAuthCallbackError("Could not open the provider authorization page")
        deadline = time.monotonic() + bounded_timeout
        while time.monotonic() < deadline and not result:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise OAuthCallbackError(result["error"])
    code = result.get("code")
    if not code:
        raise OAuthCallbackError("Provider authorization timed out")
    return OAuthCallbackResult(code=code)
