"""The two attributions, and why only one of them is worth trusting."""

from __future__ import annotations

import pytest

from persona_api.domain.provenance import Source


class TestSource:
    def test_the_set_is_closed(self) -> None:
        # A typo in a free-string source is a memory that can never be filtered for
        # again, and nobody notices until they go looking for everything the owner
        # said.
        assert {source.value for source in Source} == {"owner", "assistant", "service"}

    def test_an_invented_source_is_not_a_source(self) -> None:
        with pytest.raises(ValueError, match="inferred"):
            Source("inferred")

    def test_it_serializes_as_its_value(self) -> None:
        # A StrEnum so it round-trips through JSON and through a TEXT column without a
        # converter on either side.
        assert f"{Source.ASSISTANT}" == "assistant"
