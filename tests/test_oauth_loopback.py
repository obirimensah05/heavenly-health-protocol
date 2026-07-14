from __future__ import annotations

import socket
import threading
import urllib.request

import pytest

from heavenly_health.providers.oauth_loopback import (
    OAuthCallbackError,
    receive_oauth_callback,
)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_loopback_receiver_accepts_one_matching_state_without_logging_query() -> None:
    port = _free_port()
    callback = f"http://127.0.0.1:{port}/providers/google-health/oauth/callback"
    expected_state = "s" * 32

    def open_browser(_url: str) -> bool:
        def callback_request() -> None:
            with urllib.request.urlopen(
                f"{callback}?code=one-time-code&state={expected_state}", timeout=5
            ) as response:
                assert response.status == 200

        threading.Thread(target=callback_request, daemon=True).start()
        return True

    result = receive_oauth_callback(
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth?redacted=yes",
        callback_url=callback,
        expected_state=expected_state,
        open_browser=open_browser,
        timeout_seconds=5,
    )

    assert result.code == "one-time-code"


def test_loopback_receiver_rejects_wrong_state() -> None:
    port = _free_port()
    callback = f"http://127.0.0.1:{port}/providers/google-health/oauth/callback"

    def open_browser(_url: str) -> bool:
        def callback_request() -> None:
            try:
                urllib.request.urlopen(f"{callback}?code=code&state=wrong", timeout=5)
            except Exception:
                pass

        threading.Thread(target=callback_request, daemon=True).start()
        return True

    with pytest.raises(OAuthCallbackError, match="state validation failed"):
        receive_oauth_callback(
            authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
            callback_url=callback,
            expected_state="s" * 32,
            open_browser=open_browser,
            timeout_seconds=5,
        )
