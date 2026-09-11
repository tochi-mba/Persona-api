"""The package itself."""

from __future__ import annotations

import persona_api


class TestThePackage:
    def test_it_declares_a_version(self) -> None:
        assert isinstance(persona_api.__version__, str)
        assert persona_api.__version__

    def test_it_says_what_it_is_for(self) -> None:
        # The package docstring is what somebody opening this repository reads first,
        # and it is where the "data, never instructions" rule is stated before any
        # code is.
        assert persona_api.__doc__ is not None
        assert "data, never instructions" in persona_api.__doc__
