"""What forgetting does, as a value: when a row may go, and what the log says about it."""

from __future__ import annotations

from datetime import timedelta

import pytest

from persona_api.domain.erasure import (
    DEFAULT_GRACE_DAYS,
    TOMBSTONE,
    ErasureMode,
    ErasurePolicy,
)
from persona_api.domain.errors import InvalidProfileError
from persona_api.domain.personas import DEFAULT_PERSONA_SEGMENT, normalize_profile
from tests.fakes.clock import EPOCH


class TestTheDefault:
    def test_nobody_who_has_not_chosen_has_anything_destroyed(self) -> None:
        """The bug, named: a person who never chose an erasure mode had a forgotten row destroyed.

        persona-api kept every forgotten row for ever before forgetting was a choice. The
        policy nobody chose has to be exactly that, or adopting the setting changes what
        "forget" means for everybody who never opened settings-api.
        """
        assert TOMBSTONE.mode is ErasureMode.TOMBSTONE
        assert TOMBSTONE.purge_after(EPOCH) is None
        assert not TOMBSTONE.destroys_now

    def test_the_spelling_is_user_apis(self) -> None:
        # One vocabulary across the family: a person answers the question once.
        assert {mode.value for mode in ErasureMode} == {"grace", "immediate", "tombstone"}
        assert DEFAULT_GRACE_DAYS == 30


class TestWhenARowMayGo:
    def test_a_grace_period_is_counted_from_the_moment_of_forgetting(self) -> None:
        policy = ErasurePolicy(mode=ErasureMode.GRACE, grace_days=7)

        assert policy.purge_after(EPOCH) == EPOCH + timedelta(days=7)

    def test_a_grace_of_zero_days_is_due_at_once_but_still_waits_for_a_sweep(self) -> None:
        policy = ErasurePolicy(mode=ErasureMode.GRACE, grace_days=0)

        assert policy.purge_after(EPOCH) == EPOCH
        assert not policy.destroys_now

    def test_immediate_schedules_nothing_because_nothing_is_left_to_schedule(self) -> None:
        policy = ErasurePolicy(mode=ErasureMode.IMMEDIATE)

        assert policy.destroys_now
        assert policy.purge_after(EPOCH) is None


class TestWhatTheLogSays:
    def test_a_tombstone_says_nothing_exactly_as_before(self) -> None:
        assert TOMBSTONE.describe(EPOCH) == ""

    def test_a_grace_period_says_when(self) -> None:
        policy = ErasurePolicy(mode=ErasureMode.GRACE, grace_days=1)

        assert policy.describe(EPOCH) == f"erased after {(EPOCH + timedelta(days=1)).isoformat()}"

    def test_immediate_says_it_is_gone(self) -> None:
        assert ErasurePolicy(mode=ErasureMode.IMMEDIATE).describe(EPOCH) == "erased"


class TestTheReservedSegment:
    @pytest.mark.parametrize("spelling", [DEFAULT_PERSONA_SEGMENT, "@Default", " @default "])
    def test_no_persona_can_ever_be_called_it(self, spelling: str) -> None:
        # Reserved by the profile pattern itself, so no existing path changes meaning.
        with pytest.raises(InvalidProfileError):
            normalize_profile(spelling)
