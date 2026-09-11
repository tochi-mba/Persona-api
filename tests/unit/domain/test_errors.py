"""The error vocabulary, and the two properties it exists to guarantee."""

from __future__ import annotations

import pytest

from persona_api.domain import errors


class TestTheHierarchy:
    def test_every_error_here_is_a_domain_error(self) -> None:
        # So a caller can catch the whole vocabulary in one place, and so nothing in
        # this module can be raised accidentally from outside it.
        declared = [
            value
            for value in vars(errors).values()
            if isinstance(value, type) and issubclass(value, Exception)
        ]

        assert declared
        assert all(issubclass(error, errors.DomainError) for error in declared)

    @pytest.mark.parametrize(
        "error",
        [
            errors.InvalidProfileError,
            errors.InvalidFieldKeyError,
            errors.InvalidFieldValueError,
            errors.InvalidNoteError,
            errors.CredentialRefusedError,
            errors.InvalidSearchError,
            errors.InvalidCursorError,
        ],
    )
    def test_a_validation_failure_is_also_a_value_error(self, error: type[Exception]) -> None:
        # So callers validating input with generic machinery catch it without
        # importing this module.
        assert issubclass(error, ValueError)


class TestNoEnumerationOracle:
    def test_there_is_no_error_that_says_a_row_belongs_to_somebody_else(self) -> None:
        # A caller reaching for another account's persona gets the same
        # PersonaNotFoundError they would get for a profile nobody has ever used.
        # An error that distinguished them would tell one person that another person
        # has a persona -- so the absence of such a name is the guarantee, and this
        # test is what keeps it absent through a refactor.
        forbidden = ("forbidden", "denied", "notyours", "belongsto", "unauthorized")
        names = [
            name.lower().replace("_", "")
            for name, value in vars(errors).items()
            if isinstance(value, type) and issubclass(value, Exception)
        ]

        assert not [name for name in names if any(bad in name for bad in forbidden)]


class TestCredentialRefusal:
    def test_it_carries_the_rule_that_matched_without_the_text_that_matched_it(
        self,
    ) -> None:
        refusal = errors.CredentialRefusedError("no", reason="it starts with 'AKIA'")

        assert refusal.reason == "it starts with 'AKIA'"
        assert str(refusal) == "no"
