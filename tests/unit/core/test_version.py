"""Resolving the running version."""

from __future__ import annotations

from importlib import metadata

import pytest

from persona_api import __version__
from persona_api.core.version import DISTRIBUTION_NAME, service_version


class TestServiceVersion:
    def test_it_prefers_what_the_deployment_actually_installed(self) -> None:
        assert service_version() == metadata.version(DISTRIBUTION_NAME)

    def test_it_falls_back_to_the_source_constant_in_an_uninstalled_checkout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Running from a checkout that was never installed is an ordinary thing to do,
        # and reporting no version at all would make /healthy useless in exactly the
        # situation where somebody is debugging by hand.
        def missing(_name: str) -> str:
            raise metadata.PackageNotFoundError(_name)

        monkeypatch.setattr(metadata, "version", missing)

        assert service_version() == __version__
