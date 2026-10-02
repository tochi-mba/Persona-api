"""The composition root wires preferences, and closes them with the rest."""

from __future__ import annotations

import asyncio
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


class _SweeperThatFails:
    def __init__(self) -> None:
        self.attempts = 0

    async def sweep_once(self) -> int:
        self.attempts += 1
        msg = "the disk is having a bad day"
        raise RuntimeError(msg)


class _SweeperThatCounts:
    def __init__(self) -> None:
        self.attempts = 0

    async def sweep_once(self) -> int:
        self.attempts += 1
        return 0


class TestTheSweeper:
    async def test_a_sweep_that_raises_does_not_end_the_sweeping(self, tmp_path: Path) -> None:
        """The bug, named: one failed sweep stopped every later erasure, with nothing saying so."""
        container = Container.build(settings_for(tmp_path), clock=FakeClock())
        failing = _SweeperThatFails()
        container.sweeper = failing  # type: ignore[assignment]
        try:
            await container.sweep_guarded()
            await container.sweep_guarded()
        finally:
            await container.aclose()

        assert failing.attempts == 2

    async def test_it_sweeps_before_it_first_waits(self, tmp_path: Path) -> None:
        # The other order leaves rows whose grace ran out while the service was stopped
        # waiting a whole further interval after it came back.
        container = Container.build(settings_for(tmp_path), clock=FakeClock())
        counting = _SweeperThatCounts()
        container.sweeper = counting  # type: ignore[assignment]
        try:
            container.start_sweeper()
            for _ in range(20):
                await asyncio.sleep(0)
                if counting.attempts:
                    break
        finally:
            await container.aclose()

        assert counting.attempts == 1

    async def test_closing_stops_it_before_the_database_goes(self, tmp_path: Path) -> None:
        container = Container.build(settings_for(tmp_path), clock=FakeClock())
        container.start_sweeper()
        sweeping = container._sweeping

        await container.aclose()

        assert sweeping is not None
        assert sweeping.cancelled()
        assert container._sweeping is None


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
