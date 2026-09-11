"""The identity card, and the profile name that addresses it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from persona_api.domain.errors import InvalidProfileError
from persona_api.domain.personas import (
    MAX_PROFILE_LENGTH,
    MAX_SUMMARY_LENGTH,
    PERSONA_ID_PREFIX,
    Persona,
    new_persona_id,
    normalize_card_text,
    normalize_profile,
)


class TestProfileNames:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("work", "work"),
            ("Work", "work"),
            ("  work  ", "work"),
            ("WORK", "work"),
            ("home-assistant", "home-assistant"),
            ("work.v2", "work.v2"),
            ("a_b", "a_b"),
            ("x", "x"),
            ("2026", "2026"),
        ],
    )
    def test_it_folds_a_name_onto_the_one_form_that_addresses_a_persona(
        self, written: str, expected: str
    ) -> None:
        # "Work", "work " and "work" must reach one persona rather than making three,
        # because a person who made three would have no way to tell them apart in a
        # list.
        assert normalize_profile(written) == expected

    @pytest.mark.parametrize("written", ["", "   ", "-work", "work-", ".", "..", "_work"])
    def test_a_name_that_cannot_address_a_persona_is_refused(self, written: str) -> None:
        with pytest.raises(InvalidProfileError):
            normalize_profile(written)

    @pytest.mark.parametrize("written", ["wo rk", "work/other", "work@home", "wörk"])
    def test_a_name_outside_the_character_class_is_refused(self, written: str) -> None:
        # The same expression keyring validates its profile names with: a name one
        # service accepted and the other refused would be discovered by a person
        # halfway through setting up the second.
        with pytest.raises(InvalidProfileError):
            normalize_profile(written)

    def test_a_name_at_the_length_limit_is_kept(self) -> None:
        assert normalize_profile("p" * MAX_PROFILE_LENGTH) == "p" * MAX_PROFILE_LENGTH

    def test_a_name_over_the_length_limit_says_the_limit(self) -> None:
        with pytest.raises(InvalidProfileError, match=str(MAX_PROFILE_LENGTH)):
            normalize_profile("p" * (MAX_PROFILE_LENGTH + 1))

    def test_a_typo_is_a_new_persona_rather_than_an_error(self) -> None:
        # persona-api cannot check that a profile exists in keyring -- there is no
        # endpoint that would answer, and a signed token carries no profile list. So
        # "wrok" validates, and list_personas is how anybody notices.
        assert normalize_profile("wrok") == "wrok"


class TestCardText:
    def test_it_trims(self) -> None:
        assert normalize_card_text("  Ada  ", what="display name", limit=120) == "Ada"

    @pytest.mark.parametrize("written", ["", "   ", "\t\n"])
    def test_blank_becomes_absent_rather_than_empty(self, written: str) -> None:
        # "Clear my pronouns" and "I never set pronouns" are one state in the database
        # instead of three that a reader would have to know are the same.
        assert normalize_card_text(written, what="pronouns", limit=40) is None

    def test_text_over_the_limit_names_the_field_that_was_too_long(self) -> None:
        # A caller sending a name, a pronoun set and a summary in one request needs to
        # be told which of the three to shorten.
        with pytest.raises(InvalidProfileError, match="summary"):
            normalize_card_text(
                "s" * (MAX_SUMMARY_LENGTH + 1), what="summary", limit=MAX_SUMMARY_LENGTH
            )

    def test_text_exactly_at_the_limit_is_kept(self) -> None:
        text = "s" * MAX_SUMMARY_LENGTH
        assert normalize_card_text(text, what="summary", limit=MAX_SUMMARY_LENGTH) == text


class TestPersonaIds:
    def test_an_id_is_opaque_and_prefixed(self) -> None:
        # Opaque because it is public and stable: a readable id built out of the
        # account and the profile would put both into every log line that mentions it.
        assert new_persona_id().startswith(PERSONA_ID_PREFIX)

    def test_two_ids_differ(self) -> None:
        assert new_persona_id() != new_persona_id()


class TestThePersonaType:
    def test_it_is_frozen(self) -> None:
        persona = Persona(
            persona_id=new_persona_id(),
            account_id="acct",
            profile="work",
            display_name="Ada",
            pronouns="they/them",
            summary="dry, concise",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        with pytest.raises(AttributeError):
            persona.profile = "home"  # type: ignore[misc]
