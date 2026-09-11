"""Time as an injectable dependency."""

from __future__ import annotations

from datetime import UTC, datetime

from persona_api.core.clock import Clock, SystemClock
from tests.fakes.clock import EPOCH, FakeClock


class TestSystemClock:
    def test_it_satisfies_the_port(self) -> None:
        checked: Clock = SystemClock()

        assert isinstance(checked, Clock)

    def test_now_is_timezone_aware_utc(self) -> None:
        # A naive datetime reaching storage.times is refused there, and would be
        # refused far away from whatever read the wall clock. Catching it here makes
        # the failure local.
        now = SystemClock().now()

        assert now.tzinfo is not None
        assert now.utcoffset() == datetime.now(UTC).utcoffset()

    def test_monotonic_does_not_go_backwards(self) -> None:
        clock = SystemClock()

        assert clock.monotonic() <= clock.monotonic()


class TestFakeClock:
    def test_it_satisfies_the_same_port_as_the_real_one(self) -> None:
        # Which is how a change to the port is discovered: the fake stops type-checking
        # rather than silently diverging from the thing it stands in for.
        checked: Clock = FakeClock()

        assert isinstance(checked, Clock)

    def test_it_does_not_move_on_its_own(self) -> None:
        clock = FakeClock()

        assert clock.now() == clock.now() == EPOCH

    def test_it_moves_exactly_as_far_as_it_is_told(self) -> None:
        clock = FakeClock()

        clock.advance(90)

        assert (clock.now() - EPOCH).total_seconds() == 90
        assert clock.monotonic() == 90
