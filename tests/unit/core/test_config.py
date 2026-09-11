"""Configuration, and the absences it is responsible for keeping absent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from persona_api.core.config import (
    ENV_PREFIX,
    LogFormat,
    Settings,
    UnknownSettingError,
    check_for_unknown_env_vars,
    known_env_names,
    load_settings,
)


def build(**overrides: Any) -> Settings:
    """Build settings without reading a .env from the working tree.

    ``_env_file`` is a pydantic-settings init argument rather than a model field, so
    strict mypy cannot see it on the generated ``__init__``. Splatting it through a dict
    keeps the call honest without an ignore comment on every line -- the same trick
    tests/conftest.py uses for the same reason.
    """
    return Settings(**{"_env_file": None, **overrides})


class TestUnknownVariables:
    def test_a_misspelled_setting_is_a_startup_error_not_a_silent_default(self) -> None:
        # pydantic-settings ignores prefixed variables it does not recognise, which for
        # most services is a harmless convenience. PERSONA_AUDEINCE=persona would
        # otherwise leave the audience on its default with nothing in the logs to say
        # so -- and the audience is the claim that stops another service's token
        # working here.
        with pytest.raises(UnknownSettingError):
            check_for_unknown_env_vars({"PERSONA_AUDEINCE": "persona"})

    def test_it_names_every_offender_at_once(self) -> None:
        # So a deployment is fixed in one pass rather than one restart per typo.
        with pytest.raises(UnknownSettingError) as failure:
            check_for_unknown_env_vars(
                {"PERSONA_AUDEINCE": "x", "PERSONA_KEYRING_ISUER": "y", "PERSONA_PROT": "z"}
            )

        message = str(failure.value)
        assert "PERSONA_AUDEINCE" in message
        assert "PERSONA_KEYRING_ISUER" in message
        assert "PERSONA_PROT" in message

    def test_a_recognised_variable_passes(self) -> None:
        check_for_unknown_env_vars({"PERSONA_AUDIENCE": "persona", "PERSONA_PORT": "8002"})

    def test_variables_outside_the_prefix_are_none_of_our_business(self) -> None:
        check_for_unknown_env_vars({"PATH": "/usr/bin", "KEYRING_ISSUER": "x"})

    def test_it_reads_the_real_environment_when_given_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERSONA_NOT_A_SETTING", "1")

        with pytest.raises(UnknownSettingError, match="PERSONA_NOT_A_SETTING"):
            check_for_unknown_env_vars()


class TestKnownNames:
    def test_every_flat_setting_is_recognised(self) -> None:
        names = known_env_names()

        assert f"{ENV_PREFIX}AUDIENCE" in names
        assert f"{ENV_PREFIX}DATABASE_PATH" in names
        assert f"{ENV_PREFIX}MAX_PINNED_NOTES" in names

    def test_it_walks_into_a_nested_settings_model(self) -> None:
        # There are no nested settings yet. This covers the branch that makes one
        # recognised the day somebody adds it, rather than leaving the walk to be
        # discovered broken by whoever does.
        class Inner(BaseSettings):
            time_cost: int = 3

        class Outer(BaseModel):
            inner: Inner = Inner()
            flat: str = "x"

        assert known_env_names(Outer, "PERSONA_") == {
            "PERSONA_INNER__TIME_COST",
            "PERSONA_FLAT",
        }


class TestPaths:
    def test_the_database_path_is_resolved_eagerly(self) -> None:
        # Resolved at load rather than at open, so a relative path cannot mean two
        # different places either side of a chdir.
        settings = build(database_path=Path("var/persona.db"))

        assert settings.database_path.is_absolute()

    def test_a_user_path_is_expanded(self) -> None:
        settings = build(database_path=Path("~/persona.db"))

        assert "~" not in str(settings.database_path)


class TestDefaults:
    def test_it_serves_on_loopback_because_it_belongs_behind_a_proxy(self) -> None:
        assert build().host == "127.0.0.1"

    def test_the_audience_defaults_to_this_service(self) -> None:
        assert build().audience == "persona"

    def test_logs_are_json_unless_a_human_is_reading_them(self) -> None:
        assert build().log_format is LogFormat.JSON

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("max_personas_per_account", 20),
            ("max_fields_per_persona", 500),
            ("max_notes_per_persona", 5_000),
            ("max_pinned_fields", 20),
            ("max_pinned_notes", 20),
            ("max_field_value_bytes", 4_096),
            ("max_value_depth", 3),
            ("max_note_body_chars", 4_000),
            ("max_events", 10_000),
            ("recall_default_limit", 20),
            ("recall_max_limit", 100),
        ],
    )
    def test_every_cap_has_the_documented_default(self, field: str, expected: int) -> None:
        # Pinned here as well as in .env.example, because a default that drifts from its
        # documentation is worse than one that was never written down.
        assert getattr(build(), field) == expected

    def test_a_cap_of_zero_is_refused(self) -> None:
        # Every limit is a PositiveInt. A cap of zero would make the service refuse
        # every write while looking correctly configured.
        with pytest.raises(ValueError, match="greater than 0"):
            build(max_notes_per_persona=0)

    def test_an_invented_setting_is_refused_by_the_model_too(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            build(enable_admin_api=True)


class TestDeliberateAbsences:
    """The settings that must never exist.

    A knob that would disable a security property is worse than the property being
    absent, because somebody in a hurry will find the knob. These are written down in
    the module docstring, in .env.example and in docs/operations.md -- and asserted
    here, so the absence survives somebody adding a field in good faith.
    """

    @pytest.mark.parametrize(
        "forbidden",
        [
            "verify",  # no setting disables token verification
            "insecure",
            "allow_any",
            "algorithm",  # no setting selects an algorithm; RS256 is fixed in code
            "admin",  # there is no administrative surface to enable
            "rbac",
            "role",
            "cross_account",
            "skip_credential",
            "allow_secret",
        ],
    )
    def test_no_setting_could_turn_a_security_property_off(self, forbidden: str) -> None:
        names = [name.lower() for name in Settings.model_fields]

        assert not [name for name in names if forbidden in name]


class TestLoading:
    def test_it_checks_the_environment_before_building(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERSONA_AUDEINCE", "persona")

        with pytest.raises(UnknownSettingError):
            load_settings()

    def test_it_builds_from_a_clean_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for name in [key for key in os.environ if key.startswith(ENV_PREFIX)]:
            monkeypatch.delenv(name, raising=False)
        # chdir so a .env in the working tree cannot change the answer.
        monkeypatch.chdir(tmp_path)

        assert load_settings().app_name == "persona"
