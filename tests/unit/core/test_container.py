"""The composition root wires preferences, and closes them with the rest."""

from __future__ import annotations

import gc
import threading
import warnings
from typing import TYPE_CHECKING, Any

import pytest
from settings_client.testing import FakeSettingsClient

from persona_api.core.container import Container
from persona_api.core.preferences import (
    DeploymentPreferences,
    SettingsApiPreferences,
    build_preference_source,
)
from tests.conftest import build_settings
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from pathlib import Path

SETTINGS_API_TOKEN = "settings-api-token-for-persona-api-01"


def settings_for(tmp_path: Path, **overrides: Any) -> Any:
    return build_settings(tmp_path, **overrides)


class TestWiring:
    async def test_without_settings_api_everybody_gets_the_configuration(
        self, tmp_path: Path
    ) -> None:
        container = Container.build(settings_for(tmp_path), clock=FakeClock())
        try:
            assert isinstance(container.preferences, DeploymentPreferences)
        finally:
            await container.aclose()

    async def test_a_configured_settings_api_is_read_per_person_and_closed_with_the_rest(
        self, tmp_path: Path
    ) -> None:
        settings = settings_for(
            tmp_path,
            settings_api_base_url="https://settings.test",
            settings_api_token=SETTINGS_API_TOKEN,
        )

        container = Container.build(settings, clock=FakeClock())

        assert isinstance(container.preferences, SettingsApiPreferences)
        await container.aclose()

    async def test_preferences_can_be_substituted(self, tmp_path: Path) -> None:
        settings = settings_for(tmp_path)
        source = build_preference_source(settings, client=FakeSettingsClient())

        container = Container.build(settings, clock=FakeClock(), preferences=source)
        try:
            assert container.preferences is source
        finally:
            await container.aclose()


class TestARefusedBuildLeaksNothing:
    """A build that fails after opening the database must close it.

    Migration and policy loading both run after the open, and a policy file that says
    something the catalogue refuses is *meant* to raise. That refusal used to drop the
    open database -- file handle, WAL sidecars and worker thread -- which Python 3.13
    reports as a ResourceWarning at collection and this suite treats as a failure.
    """

    def test_a_migration_that_fails_closes_the_database_it_was_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_: object, **__: object) -> None:
            msg = "the schema is not what this build expects"
            raise RuntimeError(msg)

        monkeypatch.setattr("persona_api.core.container.migrate", refuse)
        threads_before = threading.active_count()

        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            with pytest.raises(RuntimeError, match="schema"):
                Container.build(settings_for(tmp_path), clock=FakeClock())
            gc.collect()
        assert threading.active_count() == threads_before, "the worker thread was given back"
