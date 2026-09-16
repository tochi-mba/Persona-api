"""The composition root wires preferences, and closes them with the rest."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    def test_without_settings_api_everybody_gets_the_configuration(self, tmp_path: Path) -> None:
        container = Container.build(settings_for(tmp_path), clock=FakeClock())

        assert isinstance(container.preferences, DeploymentPreferences)

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

    def test_preferences_can_be_substituted(self, tmp_path: Path) -> None:
        settings = settings_for(tmp_path)
        source = build_preference_source(settings, client=FakeSettingsClient())

        container = Container.build(settings, clock=FakeClock(), preferences=source)

        assert container.preferences is source
