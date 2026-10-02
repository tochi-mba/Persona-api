"""One person's choices, and what persona-api does with them -- and without them.

Every test that reads settings-api here uses its shared fake, including with it switched
off, because the outage is the case most services forget and the one their users notice.
"""

from __future__ import annotations

from typing import Any

import pytest
from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient

from persona_api.core.config import LogFormat, Settings
from persona_api.core.logging import configure_logging
from persona_api.core.preferences import (
    NOT_GUESSED,
    REFUSED,
    DeploymentPreferences,
    Preferences,
    SettingsApiPreferences,
    build_preference_source,
    deployment_preferences,
)
from persona_api.domain.erasure import (
    DEFAULT_GRACE_DAYS,
    MAX_GRACE_DAYS,
    TOMBSTONE,
    ErasureMode,
    ErasurePolicy,
)
from persona_api.domain.errors import PreferencesUnavailableError

USER_TOKEN = "a-user-token-from-keyring"
SETTINGS_API_TOKEN = "settings-api-token-for-persona-api-01"

FALLBACKS = {
    "recall_default_limit": Fallback(default=20, on_unavailable=OnUnavailable.USE_DEFAULT),
    "max_pinned_fields": Fallback(default=20, on_unavailable=OnUnavailable.USE_DEFAULT),
    "max_pinned_notes": Fallback(default=20, on_unavailable=OnUnavailable.USE_DEFAULT),
}


@pytest.fixture(autouse=True)
def _logs() -> None:
    configure_logging(level="INFO", log_format=LogFormat.CONSOLE)


def settings_with(**overrides: Any) -> Settings:
    return Settings(**{"_env_file": None, **overrides})


def reading(client: FakeSettingsClient, **overrides: Any) -> SettingsApiPreferences:
    return SettingsApiPreferences(client=client, settings=settings_with(**overrides))


class TestWithoutSettingsApi:
    def test_the_configuration_is_what_everybody_gets(self) -> None:
        settings = settings_with(recall_default_limit=7, max_pinned_fields=4, max_pinned_notes=5)

        assert deployment_preferences(settings) == Preferences(
            recall_default_limit=7, max_pinned_fields=4, max_pinned_notes=5
        )

    async def test_nobody_is_asked_when_settings_api_is_not_configured(self) -> None:
        settings = settings_with()
        source = build_preference_source(settings)

        assert isinstance(source, DeploymentPreferences)
        assert await source.for_token(USER_TOKEN) == deployment_preferences(settings)
        await source.aclose()

    async def test_a_configured_settings_api_is_asked_per_person(self) -> None:
        source = build_preference_source(
            settings_with(
                settings_api_base_url="https://settings.test",
                settings_api_token=SETTINGS_API_TOKEN,
            )
        )

        assert isinstance(source, SettingsApiPreferences)
        await source.aclose()

    async def test_a_substituted_client_is_the_one_asked(self) -> None:
        client = FakeSettingsClient()

        await build_preference_source(settings_with(), client=client).for_token(USER_TOKEN)

        assert client.resolves == 1


class TestAPersonsChoices:
    async def test_they_become_the_caps_their_writes_are_held_to(self) -> None:
        client = FakeSettingsClient()
        client.seed(
            "persona",
            {"recall_default_limit": 5, "max_pinned_fields": 3, "max_pinned_notes": 2},
        )

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences == Preferences(
            recall_default_limit=5, max_pinned_fields=3, max_pinned_notes=2
        )

    async def test_a_ceiling_can_be_narrowed_and_never_raised(self) -> None:
        client = FakeSettingsClient()
        client.seed(
            "persona",
            {"recall_default_limit": 80, "max_pinned_fields": 20, "max_pinned_notes": 20},
        )

        preferences = await reading(
            client, recall_default_limit=10, max_pinned_fields=4, max_pinned_notes=6
        ).for_token(USER_TOKEN)

        assert preferences == Preferences(
            recall_default_limit=10, max_pinned_fields=4, max_pinned_notes=6
        )

    async def test_the_recall_default_cannot_exceed_the_hard_cap(self) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {"recall_default_limit": 80})

        preferences = await reading(client, recall_default_limit=50, recall_max_limit=50).for_token(
            USER_TOKEN
        )

        assert preferences.recall_default_limit == 50

    async def test_a_request_with_no_caller_asks_nobody(self) -> None:
        client = FakeSettingsClient()
        settings = settings_with()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(None)

        assert preferences == deployment_preferences(settings)
        assert client.resolves == 0


class TestTheSettingsThatChangeWhatHappens:
    """``default_persona``, ``erasure_mode``, ``grace_days`` and ``log_values``."""

    async def test_nobody_who_chose_nothing_gets_anything_new(self) -> None:
        """The bug, named: a person who chose nothing had their forgetting or logging changed."""
        client = FakeSettingsClient()
        client.seed("persona", {})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.default_persona is None
        assert preferences.erasure == TOMBSTONE
        assert preferences.log_values is False

    async def test_without_settings_api_forgetting_is_a_tombstone_and_nothing_is_logged(
        self,
    ) -> None:
        preferences = deployment_preferences(settings_with())

        assert preferences.default_persona is None
        assert preferences.erasure == TOMBSTONE
        assert preferences.log_values is False

    async def test_a_persons_choices_arrive_as_they_made_them(self) -> None:
        client = FakeSettingsClient()
        client.seed(
            "persona",
            {
                "default_persona": "Home",
                "erasure_mode": "grace",
                "grace_days": 7,
                "log_values": True,
            },
        )

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.default_persona == "home", "in the form a path is stored in"
        assert preferences.erasure == ErasurePolicy(ErasureMode.GRACE, grace_days=7)
        assert preferences.log_values is True

    @pytest.mark.parametrize("days", [0, MAX_GRACE_DAYS])
    async def test_the_grace_bounds_are_the_catalogues(self, days: int) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {"erasure_mode": "grace", "grace_days": days})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.erasure.grace_days == days

    @pytest.mark.parametrize("mode", [ErasureMode.IMMEDIATE, ErasureMode.TOMBSTONE])
    async def test_a_mode_with_no_schedule_ignores_the_grace_period(
        self, mode: ErasureMode
    ) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {"erasure_mode": mode.value, "grace_days": "nonsense"})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.erasure == ErasurePolicy(mode=mode)

    async def test_an_outage_with_fallbacks_lands_on_what_the_catalogue_calls_safe(
        self,
    ) -> None:
        fallbacks = {
            "default_persona": Fallback(default=None, on_unavailable=OnUnavailable.USE_DEFAULT),
            "erasure_mode": Fallback(default="tombstone", on_unavailable=OnUnavailable.USE_DEFAULT),
            "log_values": Fallback(default=False, on_unavailable=OnUnavailable.USE_DEFAULT),
        }
        client = FakeSettingsClient(fallbacks={"persona": fallbacks})
        client.unavailable = True

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.default_persona is None, "an outage must not load *a* persona"
        assert preferences.erasure == TOMBSTONE
        assert preferences.log_values is False


class TestWhenSettingsApiCannotBeReached:
    async def test_never_having_answered_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.unavailable = True
        settings = settings_with(max_pinned_fields=4)

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences == deployment_preferences(settings)

    async def test_known_fallbacks_are_used_inside_the_deployment_ceilings(self) -> None:
        client = FakeSettingsClient(fallbacks={"persona": FALLBACKS})
        client.unavailable = True

        preferences = await reading(client, max_pinned_fields=4).for_token(USER_TOKEN)

        assert preferences.max_pinned_fields == 4
        assert preferences.recall_default_limit == 20
        assert preferences.max_pinned_notes == 20

    async def test_a_persona_setting_that_refuses_fails_rather_than_being_guessed(self) -> None:
        refusing = {"max_pinned_fields": Fallback(default=20, on_unavailable=OnUnavailable.REFUSE)}
        client = FakeSettingsClient(fallbacks={"persona": refusing})
        client.unavailable = True

        with pytest.raises(PreferencesUnavailableError, match=NOT_GUESSED):
            await reading(client).for_token(USER_TOKEN)


class TestWhenSettingsApiRefusesThisService:
    async def test_the_refusal_is_not_hidden_behind_defaults(self) -> None:
        client = FakeSettingsClient()
        client.rejects["persona"] = (403, "persona-api was not granted persona")

        with pytest.raises(PreferencesUnavailableError) as caught:
            await reading(client).for_token(USER_TOKEN)

        assert str(caught.value) == REFUSED
        assert "granted" not in str(caught.value)

    async def test_the_refusal_logs_the_status_and_never_the_detail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeSettingsClient()
        client.rejects["persona"] = (403, "persona-api was not granted persona")

        with pytest.raises(PreferencesUnavailableError):
            await reading(client).for_token(USER_TOKEN)

        output = capsys.readouterr().out
        assert "403" in output
        assert "granted" not in output
        assert USER_TOKEN not in output


class TestValuesThatCannotBeUsed:
    @pytest.mark.parametrize("value", [True, "3", 0, -1, None])
    async def test_an_unusable_pin_ceiling_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {"max_pinned_fields": value})

        preferences = await reading(client, max_pinned_fields=4).for_token(USER_TOKEN)

        assert preferences.max_pinned_fields == 4

    async def test_a_missing_setting_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {})
        settings = settings_with()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences == deployment_preferences(settings)

    @pytest.mark.parametrize("value", ["-bad-", "@default", "", 7, True])
    async def test_an_unusable_default_persona_names_none(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {"default_persona": value})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.default_persona is None

    @pytest.mark.parametrize("value", ["shred", 1, True, ["grace"]])
    async def test_an_unusable_erasure_mode_is_a_tombstone(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {"erasure_mode": value, "grace_days": 1})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.erasure == TOMBSTONE

    @pytest.mark.parametrize("value", [None, -1, MAX_GRACE_DAYS + 1, True, "7"])
    async def test_an_unusable_grace_period_is_the_catalogue_default(self, value: Any) -> None:
        # The person did choose destruction; it is the schedule settings-api got wrong.
        client = FakeSettingsClient()
        client.seed("persona", {"erasure_mode": "grace", "grace_days": value})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.erasure == ErasurePolicy(ErasureMode.GRACE, DEFAULT_GRACE_DAYS)

    @pytest.mark.parametrize("value", ["true", 1, None])
    async def test_an_unusable_log_setting_keeps_no_values(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {"log_values": value})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.log_values is False

    async def test_the_key_is_logged_and_the_value_never_is(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeSettingsClient()
        client.seed("persona", {"max_pinned_fields": "a-value-nobody-should-read"})

        await reading(client).for_token(USER_TOKEN)

        output = capsys.readouterr().out
        assert "max_pinned_fields" in output
        assert "a-value-nobody-should-read" not in output
        assert USER_TOKEN not in output
