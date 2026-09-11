"""The server entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import uvicorn

from persona_api import __main__


class TestMain:
    def test_it_serves_on_the_configured_host_and_port(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        recorded: dict[str, Any] = {}

        def record(app: str, **kwargs: Any) -> None:
            recorded["app"] = app
            recorded.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", record)
        monkeypatch.setenv("PERSONA_PORT", "9119")
        monkeypatch.setenv("PERSONA_HOST", "127.0.0.2")
        monkeypatch.chdir(tmp_path)

        __main__.main()

        assert recorded["host"] == "127.0.0.2"
        assert recorded["port"] == 9119

    def test_it_names_the_app_factory_by_string_so_reload_can_reimport_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # By string rather than by reference: uvicorn's reloader re-imports the target
        # in a fresh process, and a factory passed by reference cannot survive that.
        recorded: dict[str, Any] = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **kwargs: recorded.update({"app": app, **kwargs})
        )
        monkeypatch.chdir(tmp_path)

        __main__.main()

        assert recorded["app"] == "persona_api.api.app:create_app"
        assert recorded["factory"] is True

    def test_it_leaves_logging_alone_because_structlog_owns_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # log_config=None: uvicorn would otherwise install its own handlers and the
        # redaction processor would not be on the path any of them take.
        recorded: dict[str, Any] = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **kwargs: recorded.update({"app": app, **kwargs})
        )
        monkeypatch.chdir(tmp_path)

        __main__.main()

        assert recorded["log_config"] is None
