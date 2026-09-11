"""Redaction, and the proof that it is installed rather than merely written."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from persona_api.core.config import LogFormat
from persona_api.core.context import bind_account_id, bind_request_id
from persona_api.core.logging import (
    MAX_REDACTION_DEPTH,
    REDACTED,
    add_account_id,
    add_request_id,
    configure_logging,
    get_logger,
    is_sensitive,
    redact_secrets,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Put structlog back afterwards, so a configuring test cannot leak into another."""
    yield
    configure_logging(level="INFO", log_format=LogFormat.CONSOLE)


class TestWhatCountsAsSensitive:
    @pytest.mark.parametrize(
        "field",
        [
            "password",
            "new_password",
            "api_key",
            "refresh_token",
            "client_secret",
            "Authorization",
            "cookie",
            "private_key",
            "totp_seed",
            "master_key",
        ],
    )
    def test_credential_field_names_are_sensitive(self, field: str) -> None:
        # Matched as substrings rather than exact names, so compounds are covered
        # without maintaining a list of every one. The failure mode of an over-broad
        # rule is a redacted field that did not need it; the failure mode of a narrow
        # one is a credential in a log file.
        assert is_sensitive(field)

    @pytest.mark.parametrize("field", ["value_json", "body"])
    def test_a_personas_own_text_is_sensitive_too(self, field: str) -> None:
        # Neither is a secret. Both are a person's own words about themselves, and a
        # log aggregator is not a place to accumulate what an assistant has noticed
        # about somebody.
        assert is_sensitive(field)

    @pytest.mark.parametrize("field", ["account_id", "profile", "key", "kind", "revision"])
    def test_the_fields_that_make_a_log_useful_are_not_redacted(self, field: str) -> None:
        assert not is_sensitive(field)


class TestRedaction:
    def test_a_sensitive_value_is_replaced_wholesale(self) -> None:
        # Replaced rather than masked: the length and the type of a secret are
        # themselves information, and `password=None` would reveal that none was sent.
        record = redact_secrets(None, "info", {"password": "hunter2"})

        assert record["password"] == REDACTED

    def test_it_reaches_into_nested_structures(self) -> None:
        record = redact_secrets(
            None, "info", {"request": {"headers": {"authorization": "Bearer x"}}}
        )

        assert record["request"]["headers"]["authorization"] == REDACTED

    def test_it_reaches_into_lists(self) -> None:
        record = redact_secrets(None, "info", {"items": [{"api_key": "k"}, {"safe": "s"}]})

        assert record["items"][0]["api_key"] == REDACTED
        assert record["items"][1]["safe"] == "s"

    def test_a_structure_deeper_than_the_walker_fails_closed(self) -> None:
        # A structure this deep is either a bug or an attempt to bury a secret past the
        # walker, and neither deserves to be rendered.
        deep: object = "the secret"
        for _ in range(MAX_REDACTION_DEPTH + 2):
            deep = {"nested": deep}

        record = redact_secrets(None, "info", {"outer": deep})

        assert REDACTED in str(record)
        assert "the secret" not in str(record)

    def test_an_ordinary_value_survives(self) -> None:
        record = redact_secrets(None, "info", {"event": "field_set", "key": "voice"})

        assert record == {"event": "field_set", "key": "voice"}

    def test_a_non_string_key_is_still_checked(self) -> None:
        record = redact_secrets(None, "info", {"outer": {1: "one", "token": "t"}})

        assert record["outer"]["token"] == REDACTED
        assert record["outer"]["1"] == "one"


class TestRequestContextOnRecords:
    def test_the_request_id_is_attached_when_one_is_bound(self) -> None:
        with bind_request_id("abc123"):
            assert add_request_id(None, "info", {})["request_id"] == "abc123"

    def test_it_is_absent_rather_than_null_outside_a_request(self) -> None:
        # Absent rather than present-and-null, so a query can filter on existence.
        assert "request_id" not in add_request_id(None, "info", {})

    def test_the_account_is_attached_when_one_is_bound(self) -> None:
        with bind_account_id("acct_one"):
            assert add_account_id(None, "info", {})["account_id"] == "acct_one"

    def test_it_is_absent_for_an_anonymous_request(self) -> None:
        assert "account_id" not in add_account_id(None, "info", {})


class TestTheConfiguredPipeline:
    """That the processors are installed, not merely written.

    This is the test that matters. Every assertion above operates on the redactor
    directly, which proves the function works and proves nothing about whether the
    logger anybody actually calls goes through it.
    """

    @pytest.mark.parametrize("log_format", [LogFormat.JSON, LogFormat.CONSOLE])
    def test_a_sensitive_value_never_reaches_the_output(
        self, log_format: LogFormat, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", log_format=log_format)

        get_logger(__name__).info("write_refused", value_json="sk-live-abcdef123456")

        written = capsys.readouterr().out
        assert "sk-live-abcdef123456" not in written
        assert REDACTED in written

    def test_the_request_id_reaches_the_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", log_format=LogFormat.JSON)

        with bind_request_id("req-42"):
            get_logger(__name__).info("field_set")

        assert "req-42" in capsys.readouterr().out

    def test_the_level_is_honoured(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="WARNING", log_format=LogFormat.JSON)

        get_logger(__name__).info("not_important")

        assert capsys.readouterr().out == ""

    def test_reconfiguring_replaces_the_configuration(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="WARNING", log_format=LogFormat.JSON)
        configure_logging(level="INFO", log_format=LogFormat.JSON)

        get_logger(__name__).info("now_important")

        assert "now_important" in capsys.readouterr().out


class TestGetLogger:
    def test_a_logger_carries_its_module_name(self) -> None:
        configure_logging(level="INFO", log_format=LogFormat.JSON)

        bound = get_logger("persona_api.memory.fields")

        # Bound into the event dict rather than read off the underlying logger, so it
        # survives regardless of which logger factory is configured.
        assert structlog.get_context(bound)["logger"] == "persona_api.memory.fields"
